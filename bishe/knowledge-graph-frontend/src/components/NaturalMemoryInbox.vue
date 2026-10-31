<template>
  <section v-if="view.available" class="natural-inbox" aria-label="聊天中的长期记忆">
    <h3>聊天中的长期记忆</h3>
    <p>开启后会在后台整理新消息。有待核对内容时会在这里提示，你可以照常聊天，稍后再处理。</p>
    <p v-if="view.error" role="alert">{{ view.error }}</p>
    <p v-if="view.notice" role="status">{{ view.notice }}</p>
    <template v-if="view.state">
      <fieldset :disabled="view.busy">
        <legend>收集与使用许可</legend>
        <label v-for="flag in flags" :key="flag.key"><input v-model="settings[flag.key]" type="checkbox" :disabled="flag.key === 'preference_auto_enabled' && !view.state.preference_auto_available" /> {{ flag.label }}</label>
        <p>健康自述先核对确认后保存；确认保存不等于医学确诊。关闭收集或使用不会删除已保存内容。</p>
        <button type="button" @click="controller.settings(settings)">保存设置</button>
      </fieldset>
      <div class="heading"><h4>待核对 {{ view.state.pending_count }} 条</h4><button type="button" :disabled="view.busy" @click="refresh">刷新</button></div>
      <p v-if="!view.state.items.length">当前没有需要核对的内容。</p>
      <article v-for="item in view.state.items" :key="item.key" class="review-card">
        <strong>{{ item.status === 'needs_confirmation' ? '请核对这份自述' : '需要再核对一下' }}</strong>
        <p v-for="(fact, i) in item.facts" :key="i">{{ describeNaturalFact(fact) }}</p>
        <blockquote v-if="item.source_text">{{ item.source_text }}</blockquote>
        <div v-if="item.later_messages?.length">
          <strong>之后你还说过：</strong><p v-for="(message, i) in item.later_messages" :key="i">{{ message.content }}</p>
          <p>请结合后续消息核对，再决定是否保存上面的内容。</p>
        </div>
        <p v-for="(question, i) in item.questions" :key="i">{{ question }}</p>
        <p v-if="item.needs_fresh_statement">后续消息较多，请补充你现在希望保留的说法。</p>
        <div class="actions">
          <button v-if="item.status === 'needs_confirmation'" type="button" :disabled="view.busy" @click="controller.decide(item, 'accept')">已核对，确认保存</button>
          <button type="button" :disabled="view.busy" @click="controller.decide(item, 'reject')">不保存</button>
        </div>
        <template v-if="item.status !== 'unavailable'">
          <div v-if="item.relation_required && item.facts.some(fact => fact.category === 'self_disease_report')" class="actions">
            <button type="button" :disabled="view.busy" @click="controller.relation(item, { operation: 'new_event', target: null, target_version: null })">作为另一次经历</button>
            <button v-for="entry in item.matching_entries.filter(entry => entry.fact.category === 'self_disease_report')" :key="entry.key" type="button" :disabled="view.busy" @click="controller.relation(item, { operation: 'correction', target: entry.key, target_version: entry.version })">更正：{{ describeNaturalFact(entry.fact) }}</button>
          </div>
          <div v-if="item.relation_required && item.facts.some(fact => fact.category === 'presentation_preference')" class="actions">
            <button v-for="entry in item.matching_entries.filter(entry => entry.fact.category === 'presentation_preference' && item.facts.some(fact => fact.category === 'presentation_preference' && fact.content.slot === entry.fact.content.slot))" :key="entry.key" type="button" :disabled="view.busy" @click="controller.relation(item, { operation: 'new_default', target: entry.key, target_version: entry.version })">更新这条偏好：{{ describeNaturalFact(entry.fact) }}</button>
          </div>
          <label>补充说明<input v-model="replies[item.key]" :disabled="view.busy" maxlength="4000" /></label>
          <button type="button" :disabled="view.busy || !replies[item.key]?.trim()" @click="controller.reply(item, replies[item.key].trim())">提交补充，继续聊天</button>
        </template>
      </article>
      <h4>已保存的记忆</h4>
      <p v-if="!view.state.entries.length">尚未保存聊天中的长期记忆。</p>
      <article v-for="entry in view.state.entries" :key="entry.key" class="review-card">
        <strong>{{ describeNaturalFact(entry.fact) }}</strong><p>{{ statusLabel(entry.status) }}</p>
        <details v-if="entry.sources?.length"><summary>原始表达</summary><p v-for="(source, i) in entry.sources" :key="i">{{ source.text }}</p></details>
        <div v-if="entry.status !== 'forgotten'" class="actions">
          <button v-if="entry.status !== 'disputed' && entry.status !== 'retracted'" type="button" :disabled="view.busy" @click="controller.manage(entry, entry.status === 'active' ? 'pause' : 'resume')">{{ entry.status === 'active' ? '停用' : '启用' }}</button>
          <button v-if="entry.status === 'disputed'" type="button" :disabled="view.busy" @click="controller.manage(entry, 'keep_original')">核对后保留原记忆</button>
          <button v-if="entry.can_undo" type="button" :disabled="view.busy" @click="controller.manage(entry, 'undo')">撤销上次偏好更改</button>
          <button v-if="entry.fact?.category === 'self_disease_report' && entry.status !== 'retracted'" type="button" :disabled="view.busy" @click="controller.manage(entry, 'retraction')">撤回本人自述</button>
          <button type="button" :disabled="view.busy" @click="controller.manage(entry, 'forget')">忘记这条</button>
        </div>
      </article>
    </template>
  </section>
</template>

<script setup>
/* global defineEmits */
import { ref, reactive, watch, onBeforeUnmount } from 'vue'
import axios from 'axios'
import { API_BASE_URL } from '../config/api'
import { useChatStore } from '../stores/chat'
import { createNaturalMemoryInbox, describeNaturalFact } from '../utils/naturalMemoryInbox.js'
const emit = defineEmits(['available'])
const chat = useChatStore(), view = ref({ available: false, state: null }), settings = reactive({}), replies = reactive({})
const flags = [
  { key: 'collect_health', label: '收集本人疾病自述' }, { key: 'collect_preferences', label: '收集回答风格和篇幅偏好' },
  { key: 'use_enabled', label: '在回答中使用已保存的记忆' }, { key: 'cloud_extraction', label: '允许将必要的新消息交给模型整理' },
  { key: 'cloud_verification', label: '允许模型复核健康自述' }, { key: 'cloud_routing', label: '允许模型判断问题是否需要个人背景' },
  { key: 'cloud_answer_personal_context', label: '允许回答模型使用选中的个人背景' },
  { key: 'preference_auto_enabled', label: '自动保存清楚的表达偏好' }
]
const statusLabel = status => ({ active: '已保存', paused: '已停用', disputed: '来源需要核对', retracted: '已撤回', forgotten: '已遗忘' }[status] || '待核对')
const controller = createNaturalMemoryInbox({
  async request(path, body) { const result = await axios({ url: `${API_BASE_URL}/natural-memory/${path}`, method: body === undefined ? 'GET' : 'POST', data: body }); return result.data },
  changed(value) {
    view.value = value; emit('available', value.available)
    if (value.state && !value.busy) { Object.assign(settings, value.state.settings); chat.setNaturalMemoryState(value.state) }
  }
})
async function refresh() { try { await controller.refresh() } catch { /* The controller renders operation errors; chat stays usable. */ } }
watch(() => chat.currentUserKey, owner => {
  for (const key of Object.keys(replies)) delete replies[key]
  for (const key of Object.keys(settings)) delete settings[key]
  controller.connect(chat.usesServerHistory ? owner : null)
}, { immediate: true })
onBeforeUnmount(() => controller.clear())
</script>

<style scoped>
.natural-inbox { border-bottom: 1px solid #dce7e5; padding-bottom: 18px; margin-bottom: 18px; color: #294b47; }
fieldset { border: 1px solid #dce7e5; border-radius: 10px; padding: 12px; min-width: 0; }
label { display: block; margin: 9px 0; font-size: 14px; }
.review-card { border: 1px solid #dce7e5; border-radius: 10px; padding: 12px; margin: 12px 0; overflow-wrap: anywhere; }
.heading, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.heading { justify-content: space-between; } p, blockquote { font-size: 14px; line-height: 1.6; }
blockquote { margin: 10px 0; padding: 8px 12px; background: #f2f7f6; border-left: 3px solid #59867c; }
input:not([type=checkbox]) { box-sizing: border-box; width: 100%; padding: 8px; border: 1px solid #b9cfca; border-radius: 6px; }
button { cursor: pointer; border: 1px solid #b5cec6; border-radius: 6px; background: white; padding: 7px 10px; color: #245247; }
button:disabled { opacity: .5; cursor: default; }
</style>
