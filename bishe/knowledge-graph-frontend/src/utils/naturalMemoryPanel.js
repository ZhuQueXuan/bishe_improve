import { createNaturalMemoryFence } from './naturalMemoryFence.js'

export function createNaturalMemoryPanel({ transport, changed = () => {}, key = () => crypto.randomUUID().replaceAll('-', '') }) {
  const fence = createNaturalMemoryFence()
  let generation = 0, readNumber = 0, token = '', retry = null
  const view = { connected: false, busy: false, panel: null, conversation: null, answer: null,
    checkpoint: null, notice: '', error: '', retryAvailable: false }
  const emit = () => changed({ ...view })
  const clearDerived = () => { view.answer = null; view.checkpoint = null; fence.clear() }
  const versions = () => ({ expected_revision: view.panel.revision, expected_epoch: view.panel.epoch,
    expected_policy_version: view.panel.policy_version })
  const sessionFence = () => view.panel && view.conversation ? { owner: view.panel.owner,
    session: `conversation:${view.conversation.id}`, revision: view.panel.revision, epoch: view.panel.epoch,
    policy_version: view.panel.policy_version, answer_grant_version: view.panel.answer_grant_version } : null
  const request = (path, body, credential = token) => transport(path, body, credential)
  function logout() {
    generation++; readNumber++; token = ''; retry = null; clearDerived()
    Object.assign(view, { connected: false, busy: false, panel: null, conversation: null,
      notice: '', error: '', retryAvailable: false }); emit()
  }
  async function refresh() {
    if (!token) return
    const mine = generation, number = ++readNumber, credential = token
    try {
      const panel = await request('panel/', undefined, credential)
      if (mine !== generation || number !== readNumber) return
      const previous = view.conversation?.id
      const conversation = panel.conversations.find(c => c.id === previous) || panel.conversations[0] || null
      const next = conversation ? { owner: panel.owner, session: `conversation:${conversation.id}`,
        revision: panel.revision, epoch: panel.epoch, policy_version: panel.policy_version,
        answer_grant_version: panel.answer_grant_version } : null
      if (next && !fence.set(next)) { clearDerived(); throw new Error('stale_panel') }
      view.panel = panel; view.conversation = conversation; view.connected = true
      if (view.answer && !fence.accepts(view.answer)) view.answer = null
      if (view.checkpoint && JSON.stringify(view.checkpoint.fence) !== JSON.stringify(next)) view.checkpoint = null
      emit(); return panel
    } catch (error) {
      if (mine !== generation || number !== readNumber) return
      clearDerived(); view.panel = null; view.error = error.message || 'read_failed'; emit(); throw error
    }
  }
  async function connect(credential) {
    logout(); token = credential.trim()
    if (!token) return
    try { await refresh() } catch { /* rendered as unavailable; no inherited state */ }
  }
  function selectConversation(id) {
    if (view.busy || !view.panel) return
    generation++; readNumber++; retry = null; view.retryAvailable = false; clearDerived()
    view.conversation = view.panel.conversations.find(c => c.id === Number(id)) || null
    view.notice = ''; view.error = ''; const next = sessionFence(); if (next) fence.set(next); emit()
  }
  async function execute(path, body, sameRequest = false) {
    if (view.busy || !token || !view.panel) return
    const mine = generation, credential = token
    const operation = { path, body: JSON.parse(JSON.stringify(body)) }
    view.busy = true; view.error = ''; view.notice = ''; clearDerived(); emit()
    if (!sameRequest) { retry = null; view.retryAvailable = false }
    try {
      const result = await request(path, operation.body, credential)
      if (mine !== generation) return
      retry = null; view.retryAvailable = false
      await refresh()
      if (mine !== generation || !view.panel) return
      view.notice = result.processing_status || result.answer_status || result.status
      const answer = result.answer_result || result
      if (answer.status === 'answered') {
        if (fence.accepts(answer)) view.answer = answer
        else view.notice = 'stale_answer'
      }
      if (answer.status === 'answer_paused' && answer.checkpoint_key &&
          answer.revision === view.panel.revision && answer.epoch === view.panel.epoch) {
        view.checkpoint = { key: answer.checkpoint_key, deadline: answer.deadline_at, fence: sessionFence() }
      }
      if (result.conversation_id && path === 'panel/conversations/') selectCreated(result.conversation_id)
      return result
    } catch (error) {
      if (mine !== generation) return
      clearDerived(); view.error = error.message || 'request_failed'
      // A network ambiguity keeps the exact body/key, never invents a new write.
      if (error.uncertain) { retry = operation; view.retryAvailable = true }
      try { await refresh() } catch { /* already closed */ }
    } finally {
      if (mine === generation) { view.busy = false; emit() }
    }
  }
  function selectCreated(id) {
    clearDerived(); view.conversation = view.panel.conversations.find(c => c.id === id) || null
    const next = sessionFence(); if (next) fence.set(next)
  }
  function base() { return { request_key: key(), ...versions(), conversation_id: view.conversation.id } }
  function message(content, clarify = false) {
    return { ...base(), content, context_ids: clarify ? view.panel.clarification_context_ids : [],
      mode: clarify ? 'clarification' : 'new_message', display_digest: clarify ? view.panel.pending.display_digest : null,
      expected_conversation_version: view.conversation.memory_version }
  }
  return { view, connect, logout, refresh, selectConversation,
    async saveSettings(flags) { return execute('panel/settings/', { request_key: key(), ...versions(),
      expected_answer_grant_version: view.panel.answer_grant_version, ...flags }) },
    async newConversation() { return execute('panel/conversations/', { request_key: key(), ...versions() }) },
    async send(content, relation = null, clarify = false) {
      return execute('panel/messages/', { message: message(content, clarify), relation })
    },
    async confirm(decision, reply = null) { return execute('confirm/', { ...base(),
      display_digest: view.panel.pending.display_digest, decision, reply_content: reply }) },
    async manage(entry, action) { return execute('manage/', { ...base(), target: entry.key,
      target_version: entry.version, display_digest: entry.display_digest, action }) },
    async ask(content, pause = false) { return execute(pause ? 'questions/pause/' : 'questions/', message(content)) },
    async resume() {
      const checkpoint = view.checkpoint
      if (!checkpoint || Date.parse(checkpoint.deadline) <= Date.now()) { clearDerived(); emit(); return }
      return execute('resume/', { checkpoint_key: checkpoint.key, expected_revision: view.panel.revision, expected_epoch: view.panel.epoch })
    },
    async reset() { return execute('reset/', { ...base(), expected_conversation_version: view.conversation.memory_version }) },
    async retry() { if (retry) return execute(retry.path, retry.body, true) },
    invalidate() { readNumber++; clearDerived(); emit() }
  }
}

export function createPanelTransport(base = '/natural-memory/') {
  return async (path, body, token) => {
    let response
    try {
      response = await fetch(base + path, { method: body === undefined ? 'GET' : 'POST',
        headers: { Authorization: `Token ${token}`, 'Content-Type': 'application/json' },
        credentials: 'omit', body: body === undefined ? undefined : JSON.stringify(body) })
    } catch {
      const error = new Error('network_result_unknown'); error.uncertain = body !== undefined; throw error
    }
    let result
    try { result = await response.json() } catch {
      const error = new Error('response_unreadable'); error.uncertain = body !== undefined; throw error
    }
    if (!response.ok) throw new Error(result.reason || (response.status === 401 ? 'login_required' : 'request_rejected'))
    return result
  }
}
