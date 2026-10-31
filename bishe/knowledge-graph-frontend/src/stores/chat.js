import { defineStore } from 'pinia'
import axios from 'axios'
import { API_BASE_URL } from '../config/api'
import { olderNaturalFence } from '../utils/naturalMemoryInbox'

const makeId = () => `guest-${Date.now()}-${Math.random().toString(16).slice(2)}`

const makeGuestSession = () => ({
  id: makeId(),
  title: '新对话',
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  messages: [], memoryStart: 0, memoryEpoch: 0, memoryVersion: 0
})

const normalizeSession = (item) => ({
  id: item.id,
  title: item.title,
  createdAt: item.created_at,
  updatedAt: item.updated_at,
  memoryStart: 0, memoryEpoch: 0, memoryVersion: item.memory_version || 0,
  messages: (item.messages || []).map(message => ({
    id: message.id,
    role: message.role,
    content: message.content,
    hasContext: Boolean(message.has_context),
    sources: message.sources || [],
    timestamp: message.timestamp
  }))
})

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessions: [],
    activeSessionId: null,
    loading: false,
    syncing: false,
    error: null,
    currentUserKey: null,
    naturalPendingCount: 0,
    naturalMemoryAvailable: false,
    naturalFence: null,
    naturalReadNumber: 0
  }),

  getters: {
    activeSession: (state) => (
      state.sessions.find(session => session.id === state.activeSessionId) || null
    ),
    usesServerHistory: (state) => state.currentUserKey?.startsWith('server:') || false
  },

  actions: {
    async initialize(user) {
      const nextUserKey = user ? `server:${user.id || user.username}` : 'guest'
      if (this.currentUserKey === nextUserKey && this.sessions.length) return

      this.currentUserKey = nextUserKey
      this.naturalPendingCount = 0
      this.naturalMemoryAvailable = false
      this.naturalFence = null
      this.naturalReadNumber++
      this.sessions = []
      this.activeSessionId = null
      this.error = null

      if (!user) {
        const session = makeGuestSession()
        this.sessions = [session]
        this.activeSessionId = session.id
        return
      }

      this.syncing = true
      try {
        const { data } = await axios.get(`${API_BASE_URL}/conversations/`)
        if (this.currentUserKey !== nextUserKey) return
        this.sessions = data.map(normalizeSession)
        if (!this.sessions.length) await this.createSession()
        else this.activeSessionId = this.sessions[0].id
      } catch (error) {
        if (this.currentUserKey !== nextUserKey) return
        this.error = error.response?.data?.error || '历史会话加载失败'
        throw error
      } finally {
        if (this.currentUserKey === nextUserKey) this.syncing = false
      }
      void this.refreshNaturalMemory()
    },

    async refreshNaturalMemory() {
      if (!this.usesServerHistory) return
      const owner = this.currentUserKey, number = ++this.naturalReadNumber
      try {
        const availability = await axios.get(`${API_BASE_URL}/natural-memory/availability/`)
        if (owner !== this.currentUserKey || number !== this.naturalReadNumber) return
        this.naturalMemoryAvailable = availability.data.available === true
        if (!this.naturalMemoryAvailable) { this.naturalPendingCount = 0; return }
        const result = await axios.get(`${API_BASE_URL}/natural-memory/inbox/`)
        if (owner === this.currentUserKey && number === this.naturalReadNumber) this.setNaturalMemoryState(result.data)
      } catch { /* Optional memory notices never fail a chat or replace its answer. */ }
    },

    setNaturalMemoryState(state) {
      if (this.naturalFence && olderNaturalFence(state, this.naturalFence)) return
      if (this.naturalFence && ['revision', 'epoch', 'policy_version', 'answer_grant_version'].some(key => state[key] !== this.naturalFence[key])) {
        for (const session of this.sessions) {
          for (const message of session.messages) if (message.resumeToken?.startsWith('nm:')) message.resumeToken = null
        }
      }
      this.naturalPendingCount = state.pending_count || 0
      this.naturalFence = { revision: state.revision, epoch: state.epoch, policy_version: state.policy_version,
        answer_grant_version: state.answer_grant_version }
    },

    async createSession() {
      const owner = this.currentUserKey
      let session
      if (this.usesServerHistory) {
        const { data } = await axios.post(`${API_BASE_URL}/conversations/`, { title: '新对话' })
        if (owner !== this.currentUserKey) return null
        session = normalizeSession(data)
        this.sessions.unshift(session)
      } else {
        session = makeGuestSession()
        this.sessions.unshift(session)
      }
      this.activeSessionId = session.id
      return session
    },

    selectSession(sessionId) {
      if (this.sessions.some(session => session.id === sessionId)) {
        this.activeSessionId = sessionId
      }
    },

    async removeSession(sessionId) {
      const owner = this.currentUserKey
      if (this.usesServerHistory) {
        await axios.delete(`${API_BASE_URL}/conversations/${sessionId}/`)
        if (owner !== this.currentUserKey) return
      }
      const index = this.sessions.findIndex(session => session.id === sessionId)
      if (index !== -1) this.sessions.splice(index, 1)
      if (!this.sessions.length) await this.createSession()
      if (this.activeSessionId === sessionId) this.activeSessionId = this.sessions[0].id
    },

    async resetMemory() {
      if (this.loading || this.syncing || !this.activeSession) return
      const session = this.activeSession
      const owner = this.currentUserKey
      this.syncing = true
      try {
        if (this.usesServerHistory) {
          const { data } = await axios.post(`${API_BASE_URL}/conversations/${session.id}/memory/`, { action: 'reset' })
          if (owner !== this.currentUserKey) return
          session.memoryVersion = data.version
        } else {
          session.memoryStart = session.messages.length
          session.memoryEpoch = (session.memoryEpoch || 0) + 1
        }
        for (const message of session.messages) message.resumeToken = null
      } finally {
        if (owner === this.currentUserKey) this.syncing = false
      }
    },

    async sendMessage(content, options = {}) {
      const text = content.trim()
      if (!text || this.loading || this.syncing) return
      if (!this.activeSession) await this.createSession()

      const session = this.activeSession
      const owner = this.currentUserKey
      const optimisticId = makeId()
      const now = new Date().toISOString()
      const naturalResume = options.resumeToken?.startsWith('nm:')
      if (!naturalResume) session.messages.push({ id: optimisticId, role: 'user', content: text, timestamp: now })
      if (session.title === '新对话') {
        session.title = text.length > 18 ? `${text.slice(0, 18)}…` : text
      }
      session.updatedAt = now
      this.loading = true
      this.error = null

      try {
        const payload = { query: text, client_message_id: optimisticId, session_id: `${session.id}:memory-${session.memoryEpoch || 0}` }
        if (options.resumeToken) payload.resume_token = options.resumeToken
        if (options.stepwise) payload.pause_after_steps = 1
        // A new request changes the task; do not keep an older continuation button.
        for (const message of session.messages) message.resumeToken = null
        if (this.usesServerHistory) {
          payload.conversation_id = session.id
        } else {
          payload.history = session.messages.slice(session.memoryStart || 0)
            .filter(message => message.id !== optimisticId && ['user', 'assistant'].includes(message.role))
            .map(message => ({ id: String(message.id), role: message.role, content: message.content.slice(0, 4000) }))
        }
        const { data } = await axios.post(`${API_BASE_URL}/ai/knowledge-qa/`, payload)
        if (owner !== this.currentUserKey) return
        if (data.natural_fence && olderNaturalFence(data.natural_fence, this.naturalFence)) {
          this.error = '记忆已更新，本次旧状态的回答未显示，请重新提问。'
          return
        }
        if (data.natural_fence) this.naturalFence = data.natural_fence

        if (this.usesServerHistory && data.user_message) {
          const index = session.messages.findIndex(message => message.id === optimisticId)
          if (index !== -1) {
            session.messages[index] = {
              id: data.user_message.id,
              role: 'user',
              content: data.user_message.content,
              timestamp: data.user_message.timestamp
            }
          }
          session.title = data.conversation_title || session.title
        }

        const assistant = data.assistant_message || {
          id: makeId(),
          role: 'assistant',
          content: data.answer,
          has_context: data.has_context,
          timestamp: new Date().toISOString()
        }
        session.messages.push({
          id: assistant.id,
          role: 'assistant',
          content: assistant.content || '暂时没有获取到回答。',
          hasContext: Boolean(assistant.has_context),
          timestamp: assistant.timestamp,
          resumeToken: data.retrieval?.resume_token || null,
          retrieval: data.retrieval || null,
          sources: assistant.sources || data.sources || [],
          memoryUsed: data.memory?.used_long_term || []
        })
      } catch (error) {
        if (owner !== this.currentUserKey) return
        const detail = error.response?.data?.error || error.message || '未知错误'
        this.error = detail
        session.messages.push({
          id: makeId(),
          role: 'assistant',
          content: `抱歉，这次回答失败了。${detail}`,
          isError: true,
          timestamp: new Date().toISOString()
        })
        throw error
      } finally {
        session.updatedAt = new Date().toISOString()
        this.loading = false
        void this.refreshNaturalMemory()
      }
    }
  }
})
