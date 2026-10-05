import { defineStore } from 'pinia'
import axios from 'axios'
import { API_BASE_URL } from '../config/api'

const makeId = () => `guest-${Date.now()}-${Math.random().toString(16).slice(2)}`

const makeGuestSession = () => ({
  id: makeId(),
  title: '新对话',
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  messages: []
})

const normalizeSession = (item) => ({
  id: item.id,
  title: item.title,
  createdAt: item.created_at,
  updatedAt: item.updated_at,
  messages: (item.messages || []).map(message => ({
    id: message.id,
    role: message.role,
    content: message.content,
    hasContext: Boolean(message.has_context),
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
    currentUserKey: null
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
        this.sessions = data.map(normalizeSession)
        if (!this.sessions.length) await this.createSession()
        else this.activeSessionId = this.sessions[0].id
      } catch (error) {
        this.error = error.response?.data?.error || '历史会话加载失败'
        throw error
      } finally {
        this.syncing = false
      }
    },

    async createSession() {
      let session
      if (this.usesServerHistory) {
        const { data } = await axios.post(`${API_BASE_URL}/conversations/`, { title: '新对话' })
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
      if (this.usesServerHistory) {
        await axios.delete(`${API_BASE_URL}/conversations/${sessionId}/`)
      }
      const index = this.sessions.findIndex(session => session.id === sessionId)
      if (index !== -1) this.sessions.splice(index, 1)
      if (!this.sessions.length) await this.createSession()
      if (this.activeSessionId === sessionId) this.activeSessionId = this.sessions[0].id
    },

    async sendMessage(content, options = {}) {
      const text = content.trim()
      if (!text || this.loading || this.syncing) return
      if (!this.activeSession) await this.createSession()

      const session = this.activeSession
      const optimisticId = makeId()
      const now = new Date().toISOString()
      session.messages.push({ id: optimisticId, role: 'user', content: text, timestamp: now })
      if (session.title === '新对话') {
        session.title = text.length > 18 ? `${text.slice(0, 18)}…` : text
      }
      session.updatedAt = now
      this.loading = true
      this.error = null

      try {
        const payload = { query: text, session_id: String(session.id) }
        if (options.resumeToken) payload.resume_token = options.resumeToken
        if (options.stepwise) payload.pause_after_steps = 1
        // A new request changes the task; do not keep an older continuation button.
        for (const message of session.messages) message.resumeToken = null
        if (this.usesServerHistory) {
          payload.conversation_id = session.id
        } else {
          payload.history = session.messages
            .filter(message => message.id !== optimisticId && ['user', 'assistant'].includes(message.role))
            .map(message => ({ id: String(message.id), role: message.role, content: message.content.slice(0, 4000) }))
        }
        const { data } = await axios.post(`${API_BASE_URL}/ai/knowledge-qa/`, payload)

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
          retrieval: data.retrieval || null
        })
      } catch (error) {
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
      }
    }
  }
})
