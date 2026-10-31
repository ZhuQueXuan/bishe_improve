// R6 delivery guard. R7 will connect this to the actual page/store lifecycle.
const fields = ['owner', 'session', 'revision', 'epoch', 'policy_version', 'answer_grant_version']

function validFence(value) {
  return value && typeof value.owner === 'string' && /^user:[1-9][0-9]*$/.test(value.owner) &&
    typeof value.session === 'string' && /^conversation:[1-9][0-9]*$/.test(value.session) &&
    fields.slice(2).every(key => Number.isSafeInteger(value[key]) && value[key] >= 0)
}

export function createNaturalMemoryFence() {
  let current = null
  return {
    clear() { current = null },
    set(next) {
      if (!validFence(next)) return false
      if (current && current.owner === next.owner &&
          (next.epoch < current.epoch || next.revision < current.revision ||
           next.policy_version < current.policy_version || next.answer_grant_version < current.answer_grant_version)) return false
      current = Object.fromEntries(fields.map(key => [key, next[key]]))
      return true
    },
    accepts(envelope) {
      return !!(current && envelope && envelope.status === 'answered' &&
        typeof envelope.answer === 'string' && typeof envelope.delivery_key === 'string' &&
        /^[0-9a-f]{32}$/.test(envelope.delivery_key) && validFence(envelope.fence) &&
        fields.every(key => current[key] === envelope.fence[key]))
    },
    snapshot() { return current ? { ...current } : null }
  }
}
