<template>
  <main class="memory-page">
    <header><p class="eyebrow">个人记忆 · 隔离预览</p><h1>让记忆由你掌握</h1>
      <p>本页只连接虚构账号和离线回答替身，不提供诊疗意见。确认自述不代表医学确诊。</p></header>
    <section v-if="!state.connected" class="card">
      <h2>进入验收账号</h2><p>使用本轮夹具提供的临时凭据；页面不会保存凭据。</p>
      <label>临时凭据 <input v-model="credential" type="password" autocomplete="off" /></label>
      <button @click="connect">连接</button>
      <div class="buttons"><button v-for="account in fixture.accounts || []" :key="account.owner" @click="controller.connect(account.token)">连接{{ account.label }}</button></div>
    </section>
    <template v-if="state.connected">
      <nav class="card toolbar"><span>{{ state.panel?.owner || '正在核对账号' }}</span>
        <label>会话 <select :value="state.conversation?.id" :disabled="state.busy" @change="controller.selectConversation($event.target.value)">
          <option v-for="c in state.panel?.conversations || []" :key="c.id" :value="c.id">{{ c.title || '隔离会话' }} · {{ c.id }}</option>
        </select></label>
        <button :disabled="blocked" @click="controller.newConversation()">新会话</button>
        <button :disabled="state.busy" @click="refresh">刷新核对</button><button @click="controller.logout()">退出 / 换账号</button>
      </nav>
      <div v-if="state.panel" class="columns">
        <aside class="card"><h2>收集与使用</h2><p>每个选择分别生效。关闭收集不会删除已保存的记忆。</p>
          <label v-for="[flag, label] in flags" :key="flag" class="toggle"><input v-model="settings[flag]" type="checkbox" :disabled="state.busy" />{{ label }}</label>
          <p v-if="!state.panel.preference_auto_gate_passed" class="hint">自动偏好保存尚未开放，开启意愿后仍需确认。健康自述本阶段始终先确认。</p>
          <button :disabled="blocked" @click="saveSettings">保存选择</button>
        </aside>
        <section class="card"><h2>聊一聊</h2><p>可以正常输入。本轮离线夹具仅覆盖约定的虚构样例，其余内容会显示未处理。</p>
          <label>想说的话 <textarea v-model="draft" rows="4" maxlength="4096" :disabled="state.busy" /></label>
          <p v-if="tooLong" class="error">这条表达超出本轮单条长度上限，请分成较短的消息。</p>
          <label v-if="fixture.samples?.length">填入验收样例（不会自动发送）<select @change="draft = $event.target.value"><option value="">选择一个虚构样例</option><option v-for="sample in fixture.samples" :key="sample" :value="sample">{{ sample }}</option></select></label>
          <label>这次表达与旧记忆的关系 <select v-model="relationMode" :disabled="state.busy">
            <option value="">由提取流程判断，关系不清时核对</option><option value="correction">更正指定旧记忆</option><option value="new_event">这是另一件事</option></select></label>
          <label v-if="relationMode === 'correction'">更正哪条 <select v-model="target" :disabled="state.busy"><option value="">请选择</option>
            <option v-for="e in correctable" :key="e.key" :value="e.key">{{ factLabel(e.fact) }}</option></select></label>
          <div class="buttons"><button :disabled="blocked || tooLong || !draft.trim() || (relationMode === 'correction' && !target)" @click="send">发送并检查可记内容</button>
            <button :disabled="blocked || tooLong || !draft.trim()" @click="ask(false)">作为问题回答</button><button :disabled="blocked || tooLong || !draft.trim()" @click="ask(true)">准备回答，稍后继续</button></div>
          <div v-if="state.checkpoint" class="notice"><p>回答已暂停，原期限 {{ state.checkpoint.deadline }}。只能继续一次。</p><button :disabled="blocked || expired" @click="controller.resume()">{{ expired ? '已过期' : '继续回答' }}</button></div>
          <article v-if="state.answer" class="answer"><h3>回答</h3><p>{{ state.answer.answer }}</p><small>本轮为离线回答替身。</small>
            <h4>本次引用的个人自述</h4><p v-if="!state.answer.basis?.personal_reports?.length">本次未引用本人疾病自述，不能据此推断没有疾病。</p>
            <p v-for="r in state.answer.basis?.personal_reports || []" :key="r.ref">{{ r.ref }} · {{ factLabel({ category: 'self_disease_report', content: { value_raw: r.name_raw, polarity: r.polarity, certainty: r.certainty }, time: r.event_time }) }}</p>
            <h4>专业资料依据</h4><blockquote v-for="r in state.answer.basis?.professional_evidence || []" :key="r.ref">{{ r.text }}<small>{{ r.ref }} · {{ r.title }} · 第 {{ r.source.page }} 页 · 版本 {{ r.source.version_id }}</small></blockquote>
            <p v-if="state.answer.saved_preferences_used">本次使用了已保存的回答偏好。</p>
          </article>
        </section>
        <section class="card wide"><h2>待你核对</h2>
          <template v-if="state.panel.pending">
            <p>尚未保存，不能作为有效个人背景使用。</p>
            <p v-if="!pendingHere">候选来自另一会话，请先切换至对应会话。</p>
            <article v-for="(action, i) in state.panel.pending.actions" :key="i" class="entry">
              <p>{{ factLabel(action.fact) }}</p><p v-if="action.previous_fact">原记录：{{ factLabel(action.previous_fact) }}</p>
              <blockquote v-for="(q, n) in action.sources" :key="n">{{ q.text }}<small>原话 · {{ q.recorded_at }}</small></blockquote>
              <small>{{ action.semantic_assurance === 'extraction_only_not_supported' ? '提取结果尚未语义复核' : '离线复核替身结果，未证明模型理解能力' }}</small>
            </article>
            <p v-for="(q, i) in state.panel.pending.questions" :key="i">{{ q.question || q }}</p>
            <div v-if="state.panel.pending.unresolved"><p>关系或含义未明确，请在原会话补充。不会先替你保存。</p>
              <button :disabled="blocked || !pendingHere || !draft.trim() || !state.panel.clarification_available" @click="send(true)">把输入作为本次补充</button></div>
            <button v-else :disabled="blocked || !pendingHere" @click="confirm('accept')">确认这份自述 / 偏好</button>
            <button :disabled="blocked || !pendingHere" @click="confirm('reject')">不保存这次候选</button>
          </template><p v-else>当前没有有效的待确认候选。</p>
        </section>
        <section class="card wide"><h2>已保存与停用的记忆</h2><p>“已确认”表示你认可记录的表达；怀疑、否定和原时间不会因此改变。</p>
          <p v-if="!state.panel.entries.length">暂无记忆。</p>
          <article v-for="e in state.panel.entries" :key="e.key" class="entry">
            <h3>{{ factLabel(e.fact) }}</h3><p>{{ entryStatus(e) }}<template v-if="e.fact"> · {{ e.user_confirmed ? '用户确认来源' : '自动来源 / 未逐条确认' }}</template></p>
            <blockquote v-for="(q, i) in e.sources" :key="i">{{ q.text }}<small>原话 · {{ q.recorded_at }}</small></blockquote>
            <div v-if="e.fact" class="buttons">
              <button v-if="e.status === 'active'" :disabled="blocked" @click="manage(e, 'pause')">暂停使用</button>
              <button v-if="e.status === 'paused'" :disabled="blocked" @click="manage(e, 'resume')">恢复使用</button>
              <button v-if="['active','disputed'].includes(e.status)" :disabled="blocked" @click="correct(e)">更正这条</button>
              <button :disabled="blocked" @click="manage(e, 'retraction')">撤销使用</button>
              <button v-if="e.status === 'disputed'" :disabled="blocked" @click="manage(e, 'keep_original')">核对后保留原记录</button>
              <button class="danger" :disabled="blocked" @click="forgetTarget = e">遗忘</button>
            </div>
          </article>
          <div v-if="forgetTarget" role="dialog" aria-label="遗忘确认" class="notice"><p>遗忘“{{ factLabel(forgetTarget.fact) }}”会清除该记忆及派生内容。原聊天仍保留，但不会从排除的旧来源自动重建。</p>
            <button :disabled="blocked" @click="forget">确认遗忘</button><button @click="forgetTarget = null">取消</button></div>
          <button :disabled="blocked" @click="resetOpen = true">重置当前会话的记忆上下文</button>
          <div v-if="resetOpen" class="notice"><p>旧回答与续做将失效，原聊天保留。</p><button :disabled="blocked" @click="reset">确认重置上下文</button><button @click="resetOpen = false">取消</button></div>
        </section>
      </div>
      <p v-else class="card">暂时无法核对当前状态，已收起个人内容。请刷新，或退出后重新连接。</p>
    </template>
    <p v-if="state.notice" role="status" class="notice">{{ statusText(state.notice) }}</p>
    <p v-if="state.error" role="alert" class="error">操作未能确认完成。{{ errorText(state.error) }}</p>
    <button v-if="state.retryAvailable" :disabled="blocked" @click="controller.retry()">核对并重试同一次操作</button>
  </main>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { createNaturalMemoryPanel, createPanelTransport } from '../utils/naturalMemoryPanel.js'
const fixture = window.__NATURAL_MEMORY_FIXTURE__ || {}
const state = reactive({ connected: false }), settings = reactive({})
const draft = ref(''), credential = ref(''), relationMode = ref(''), target = ref(''), forgetTarget = ref(null), resetOpen = ref(false), now = ref(Date.now())
const controller = createNaturalMemoryPanel({ transport: createPanelTransport(), changed: value => {
  const old = state.panel
  const previousConversation = state.conversation?.id
  Object.assign(state, value)
  if (previousConversation !== value.conversation?.id) { draft.value = ''; relationMode.value = ''; target.value = ''; forgetTarget.value = null; resetOpen.value = false }
  if (value.panel && (!old || old.owner !== value.panel.owner || old.policy_version !== value.panel.policy_version || old.answer_grant_version !== value.panel.answer_grant_version)) Object.assign(settings, value.panel.settings)
  if (!value.connected) { credential.value = ''; draft.value = ''; relationMode.value = ''; target.value = ''; forgetTarget.value = null; resetOpen.value = false; Object.keys(settings).forEach(k => delete settings[k]) }
} })
const flags = [['collect_health','收集本人疾病自述'],['collect_preferences','收集回答风格与篇幅偏好'],['use_enabled','在相关回答中使用已保存记忆'],
  ['cloud_extraction','允许当前表达用于云端提取'],['cloud_verification','允许相关语境用于云端复核'],['cloud_routing','允许当前问题用于云端用途判断'],
  ['cloud_answer_personal_context','允许选中的个人背景用于云端回答'],['preference_auto_enabled','申请自动保存明确长期偏好']]
const blocked = computed(() => state.busy || !state.panel || !state.conversation)
const tooLong = computed(() => new TextEncoder().encode(draft.value).length > 4096)
const correctable = computed(() => (state.panel?.entries || []).filter(e => e.fact && ['active','disputed'].includes(e.status)))
const pendingHere = computed(() => state.panel?.pending_session === `conversation:${state.conversation?.id}`)
const expired = computed(() => now.value >= Date.parse(state.checkpoint?.deadline))
function factLabel(fact) {
  if (!fact) return '已遗忘，内容已清除'
  const c = fact.content
  if (fact.category === 'presentation_preference') return ({ brief:'回答简短', detailed:'回答详细', plain:'用通俗语言', technical:'用专业语言' })[c.value] || '回答偏好'
  return `${c.polarity === 'negated' ? '否定自述：' : c.polarity === 'unknown' ? '方向不明：' : '本人自述：'}${c.certainty === 'possible' ? '可能' : c.certainty === 'unknown' ? '尚不确定' : ''}${c.value_raw}（${fact.time.raw || '未说明事件时间'}）`
}
function entryStatus(e) { return ({ active: e.eligible ? '可供相关问题参考' : '已保存，目前不可参考', paused:'已暂停', disputed:'有争议，已停止使用', retracted:'已撤销使用', forgotten:'已遗忘' })[e.status] || '不可使用' }
function statusText(s) { return ({ needs_confirmation:'收到表达，候选待确认，尚未记住。', projected_confirmed:'已保存你确认的表达。', settings_updated:'选择已保存。', conversation_created:'已建立新会话。', stored_without_collection:'消息已收到；当前收集或提取许可未开，未建立记忆。', processing_failed:'消息已收到，但记忆处理失败，没有保存新候选。', worker_not_configured:'消息已收到，提取服务尚未接入，不能说已记住。', no_memory:'本次没有可保存内容。', replayed:'已核对原操作，没有重复提交。', answered:'回答完成。', stale_answer:'状态已变化，旧回答已失效，请重新发起问题。', answer_paused:'回答已暂停。', projected_forget:'已遗忘并清除派生内容。', projected_pause:'已暂停使用。', projected_resume:'已恢复使用。', projected_retraction:'已撤销使用。', projected_keep_original:'已保留原记录。', projected_auto_preference:'已自动保存偏好，可随时撤销。', needs_clarification:'含义或关系需补充，尚未保存。', answer_unavailable:'当前回答不可用。', personal_answer_permission_required:'尚未允许个人背景用于回答。', reset:'上下文已重置。' })[s] || '本次处理已返回，请核对候选、已保存列表和当前许可。' }
function errorText(s) { return s === 'network_result_unknown' ? '网络中断，提交结果未知；请重试同一次操作。' : s.startsWith('stale') ? '状态已变化，旧操作已停止。请核对刷新后的内容。' : s === 'login_required' ? '临时凭据无效或已退出。' : '当前内容未通过检查或服务暂不可用，请核对最新状态。' }
async function connect() { const value = credential.value; credential.value = ''; await controller.connect(value) }
async function refresh() { try { await controller.refresh() } catch { /* controller renders error */ } }
async function saveSettings() { await controller.saveSettings({ ...settings }); broadcast() }
async function send(clarify = false) {
  const entry = relationMode.value === 'correction' ? correctable.value.find(e => e.key === target.value) : null
  const relation = relationMode.value ? { operation: relationMode.value, target: entry?.key || null, target_version: entry?.version || null } : null
  await controller.send(draft.value, relation, clarify === true); broadcast()
}
async function ask(pause) { await controller.ask(draft.value, pause) }
async function confirm(decision) { await controller.confirm(decision); broadcast() }
async function manage(entry, action) { await controller.manage(entry, action); broadcast() }
function correct(e) { relationMode.value = 'correction'; target.value = e.key; draft.value = ''; window.scrollTo({ top: 0, behavior: 'smooth' }) }
async function forget() { const entry = forgetTarget.value; forgetTarget.value = null; await controller.manage(entry, 'forget'); broadcast() }
async function reset() { resetOpen.value = false; await controller.reset(); broadcast() }
let channel, timer
function broadcast() { channel?.postMessage('changed') }
function visible() { controller.invalidate(); if (!document.hidden && !state.busy) refresh() }
onMounted(() => {
  if (typeof BroadcastChannel !== 'undefined') { channel = new BroadcastChannel('natural-memory-isolated-panel'); channel.onmessage = visible }
  document.addEventListener('visibilitychange', visible); window.addEventListener('focus', visible)
  timer = setInterval(() => { now.value = Date.now(); if (state.connected && !state.busy && !document.hidden) refresh() }, 3000)
})
onUnmounted(() => { clearInterval(timer); channel?.close(); document.removeEventListener('visibilitychange', visible); window.removeEventListener('focus', visible); controller.logout() })
</script>

<style scoped>
.memory-page{max-width:1120px;margin:auto;padding:28px 20px 64px;color:#21374a;font:16px/1.65 system-ui,sans-serif;background:#f5f8fa;min-height:100vh;box-sizing:border-box}.eyebrow{color:#28746e;letter-spacing:.12em}h1{font-size:32px;margin:8px 0}h2{font-size:21px;margin:0 0 12px}h3{font-size:17px}p{margin:8px 0}.card{background:white;border:1px solid #dce6e9;border-radius:14px;padding:22px;margin-top:18px}.columns{display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}.columns .card{margin:0}.columns{margin-top:18px}.wide{grid-column:1/-1}.toolbar,.buttons{display:flex;flex-wrap:wrap;align-items:center;gap:10px}label{display:block;margin:10px 0}.toggle{display:flex;gap:10px;align-items:flex-start}input,select,textarea,button{font:inherit}input:not([type=checkbox]),select,textarea{box-sizing:border-box;width:100%;border:1px solid #b9ccd0;border-radius:7px;padding:9px;background:#fff;color:inherit}textarea{resize:vertical}button{border:1px solid #28746e;border-radius:7px;padding:8px 12px;color:#1f625e;background:#f0f8f6;cursor:pointer}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:3px solid #68b7b0;outline-offset:2px}.danger{color:#a13c34;border-color:#ba655c}.entry{padding:16px 0;border-top:1px solid #e3e9eb}.entry p,.answer p{white-space:pre-wrap;overflow-wrap:anywhere}blockquote{margin:10px 0;padding:10px 14px;background:#f3f7f8;border-left:3px solid #8cacb6;white-space:pre-wrap;overflow-wrap:anywhere}small{display:block;color:#657887}.hint{font-size:14px;color:#657887}.notice{padding:14px;background:#edf6f1;border-radius:8px;margin-top:12px;overflow-wrap:anywhere}.error{padding:14px;background:#fff1ed;color:#8c3d2e}.answer{margin-top:20px;border-top:2px solid #67a9a3}.toolbar label{min-width:180px;flex:1}@media(max-width:640px){.memory-page{padding:18px 12px 44px}.columns{grid-template-columns:minmax(0,1fr)}.wide{grid-column:auto}.card{padding:16px}h1{font-size:27px}.buttons button{flex:1 1 140px}}
</style>
