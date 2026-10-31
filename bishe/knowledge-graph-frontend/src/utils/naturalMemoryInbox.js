export function olderNaturalFence(incoming, current) {
  if (!current) return false
  return incoming.epoch < current.epoch || incoming.revision < current.revision ||
    incoming.policy_version < current.policy_version || incoming.answer_grant_version < current.answer_grant_version
}

export function describeNaturalFact(fact) {
  if (!fact?.content) return '内容已清除'
  const c = fact.content
  if (fact.category === 'presentation_preference') {
    return ({ plain: '喜欢通俗解释', technical: '喜欢专业解释', brief: '喜欢简短回答', detailed: '喜欢详细回答' })[c.value] || '表达偏好'
  }
  const when = fact.time?.raw ? `${fact.time.raw}，` : ''
  if (c.polarity === 'unknown') return `${when}是否有${c.value_raw}仍不确定`
  const uncertain = c.certainty !== 'asserted'
  return `${when}${c.polarity === 'negated' ? (uncertain ? '可能没有' : '已明确排除') : (uncertain ? '可能有' : '曾明确自述有')}${c.value_raw}`
}

export function createNaturalMemoryInbox({ request, changed = () => {}, key = () => crypto.randomUUID().replaceAll('-', '') }) {
  let generation = 0, reads = 0, owner = null
  const view = { available: false, busy: false, state: null, error: '', notice: '' }
  const emit = () => changed({ ...view })
  const versions = () => ({ expected_revision: view.state.revision, expected_epoch: view.state.epoch, expected_policy_version: view.state.policy_version })
  function clear() { generation++; reads++; owner = null; Object.assign(view, { available: false, busy: false, state: null, error: '', notice: '' }); emit() }
  async function refresh() {
    if (owner === null || !view.available) return
    const mine = generation, number = ++reads
    const state = await request('inbox/')
    if (mine === generation && number === reads) { view.state = state; emit() }
  }
  async function connect(identity) {
    clear(); owner = identity
    if (!identity) return
    const mine = generation
    try {
      const result = await request('availability/')
      if (mine !== generation) return
      view.available = result.available === true; emit(); await refresh()
    } catch { if (mine === generation) { view.error = '记忆区暂时无法读取，聊天不受影响。'; emit() } }
  }
  async function mutate(path, body, notice) {
    if (view.busy || !view.state || owner === null) return
    const mine = generation
    view.busy = true; view.error = ''; view.notice = ''; emit()
    try {
      await request(path, { request_key: key(), ...versions(), ...body })
      if (mine !== generation) return
      await refresh()
      if (mine === generation) view.notice = notice
    } catch {
      if (mine !== generation) return
      view.error = '内容或设置已变化，操作尚未确认，请刷新后重新核对。'
      try { await refresh() } catch { /* Keep chat independent of this panel. */ }
    } finally { if (mine === generation) { view.busy = false; emit() } }
  }
  return { view, connect, clear, refresh,
    settings(flags) { return mutate('settings/', { expected_answer_grant_version: view.state.answer_grant_version, ...flags }, '记忆设置已保存。') },
    decide(item, decision) { return mutate('decision/', { key: item.key, display_digest: item.display_digest, decision }, decision === 'accept' ? '这条记忆已确认保存。' : '这条内容不保存。') },
    reply(item, content) { return mutate('revision/', { key: item.key, display_digest: item.display_digest, content, relation: null }, '补充已收到，会在后台核对，你可以继续聊天。') },
    relation(item, relation) { return mutate('revision/', { key: item.key, display_digest: item.display_digest, content: null, relation }, '保存方式已更新，请再核对一次内容。') },
    manage(entry, action) { return mutate('entry/', { conversation_id: entry.source_conversation_id, target: entry.key, target_version: entry.version,
      display_digest: entry.display_digest, action }, action === 'forget' ? '已忘记这条内容。' : action === 'undo' ? '已恢复上一次的偏好。' : '记忆使用状态已更新。') }
  }
}
