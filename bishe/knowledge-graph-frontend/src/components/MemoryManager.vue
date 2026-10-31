<template>
  <el-drawer :model-value="open" title="记忆设置" size="min(480px, 100vw)" @update:model-value="$emit('update:open', $event)">
    <p v-if="!naturalAvailable" class="intro">日常聊天会衔接近期对话，并可记住解释偏好。本人疾病自述需先打开保存开关，再明确说“记住……”，在这里核对确认。普通健康聊天不会自动存为长期记忆。</p>
    <NaturalMemoryInbox v-if="chat.usesServerHistory && open" @available="naturalAvailable = $event" />
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div v-if="loading" aria-live="polite">正在加载记忆…</div>
    <template v-else>
      <section>
        <h3>当前会话记忆</h3>
        <p>重置后保留聊天原文，从下一问重新理解对象和要求；旧任务不能继续。</p>
        <dl v-if="short?.state">
          <dt>讨论对象</dt><dd>{{ short.state.objects?.join('、') || '尚未确定' }}</dd>
          <dt>资料范围</dt><dd>{{ short.state.field === 'prevent' ? '只查预防' : '未限定' }}</dd>
          <dt>回答形式</dt><dd>{{ short.state.format === 'table' ? '表格' : '未限定' }}</dd>
        </dl>
        <details v-if="short?.entries && Object.keys(short.entries).length">
          <summary>查看状态来源</summary>
          <p v-for="(entry, key) in short.entries" :key="key">{{ fieldLabel(key) }}：{{ entry.source_message_id }} · {{ entry.active ? '有效' : '已取消' }}</p>
        </details>
        <el-button :disabled="blocked" @click="reset">重置当前记忆</el-button>
      </section>
      <section>
        <h3>{{ naturalAvailable ? '此前保存的其他内容' : '长期记忆' }}</h3>
        <p v-if="!chat.usesServerHistory">登录后可以保存并跨会话使用。游客记忆仅在本次页面使用期间保留。</p>
        <template v-else>
          <p v-if="naturalAvailable">这些条目仍保留。新聊天中的收集和使用，请以上方“聊天中的长期记忆”设置为准。</p>
          <template v-if="!naturalAvailable">
          <label class="toggle"><input type="checkbox" :checked="long.auto_enabled" :disabled="blocked" @change="toggleAuto($event.target.checked)" /> 从明确表达中自动记住偏好</label>
          <label class="toggle"><input type="checkbox" :checked="long.enabled" :disabled="blocked" @change="toggle($event.target.checked)" /> 在回答中使用长期记忆</label>
          <label class="toggle"><input type="checkbox" :checked="long.health_collect_enabled" :disabled="blocked" @change="toggleHealth($event.target.checked)" /> 允许确认保存本人疾病自述</label>
          </template>
          <p>关闭后仍保留已保存条目；忘记会清空该条偏好，不删除聊天原文。已发出的模型请求无法撤回，设置变更后旧结果会停止返回。</p>
          <div v-for="item in long.items" :key="item.id" class="memory-card">
            <strong>{{ item.label }}</strong><span>{{ item.status === 'paused' ? '已停用' : '已保存' }}</span>
            <p>{{ item.description }}</p>
            <small>{{ item.source }} · {{ formatTime(item.updated_at) }}</small>
            <details v-if="item.source_text"><summary>原始表达</summary><p>{{ item.source_text }}</p></details>
            <div class="actions">
              <el-button size="small" :disabled="blocked" @click="edit(item)">修改</el-button>
              <el-button size="small" :disabled="blocked" @click="saveItem(item, item.status === 'paused' ? 'active' : 'paused')">{{ item.status === 'paused' ? '启用' : '停用' }}</el-button>
              <el-button size="small" :disabled="blocked" @click="forget(item)">忘记这条</el-button>
            </div>
          </div>
          <p v-if="!long.items.length">尚未保存长期偏好。</p>
          <h4>本人疾病自述</h4>
          <el-button size="small" :disabled="blocked" @click="maintain">整理记忆（不删除聊天）</el-button>
          <p>整理只核对失效候选、重复来源和缓存；不会因时间过去自动判断康复或忘记。归档保留内容并停止使用，需明确启用后再用。</p>
          <p v-for="task in long.maintenance_tasks || []" :key="`maintenance-${task.id}`">整理状态：{{ taskLabel(task.status) }}{{ task.result?.requires_narrowing ? '，数量超过本版范围，未执行部分整理' : '' }}</p>
          <p>仅限已知疾病名称及明确状态，不是诊断。不保存家属、药物或过敏原。更正时重新说“记住，我……”，再选择更新原条目。停用和忘记不删除聊天原文。</p>
          <div v-for="item in long.health_items || []" :key="`health-${item.id}`" class="memory-card">
            <strong>{{ item.description }}</strong><span>{{ healthLabel(item.status) }}</span>
            <p>事件时间原话：{{ item.report.event_time?.raw || '未说明' }}；记录时间不代表发病时间。</p>
            <small>记录于 {{ formatTime(item.report.recorded_at) }}；状态表示当时自述。</small>
            <details><summary>原始表达</summary><p>{{ item.report.source?.text }}</p></details>
            <div class="actions">
              <el-button size="small" :disabled="blocked || item.status === 'invalid'" @click="healthAction(item, item.status === 'active' ? 'pause' : 'activate')">{{ item.status === 'active' ? '停用' : '启用' }}</el-button>
              <el-button size="small" :disabled="blocked || item.status === 'archived' || item.status === 'invalid'" @click="healthAction(item, 'archive')">归档</el-button>
              <el-button size="small" :disabled="blocked" @click="healthAction(item, 'forget')">忘记这条自述</el-button>
            </div>
          </div>
          <div v-for="p in long.health_proposals || []" :key="`proposal-${p.id}`" class="memory-card">
            <strong>待确认，尚未长期保存</strong>
            <p>{{ p.report.source.text }}</p>
            <p>以下确认将允许跨会话使用；候选十分钟内有效。更新会替换当前条目，原聊天保留。</p>
            <div class="actions">
              <el-button size="small" :disabled="blocked" @click="dismissHealth(p)">不保存</el-button>
              <el-button v-if="!p.matching_ids.length" size="small" type="primary" :disabled="blocked" @click="confirmHealth(p)">确认保存</el-button>
              <template v-else>
                <el-button v-for="item in (long.health_items || []).filter(i => p.matching_ids.includes(i.id))" :key="item.id" size="small" :disabled="blocked || item.status !== 'active'" @click="confirmHealth(p, item)">更新：{{ item.description }}（{{ item.report.event_time?.raw || '未说明时间' }} · #{{ item.id }}）</el-button>
                <el-button size="small" :disabled="blocked" @click="confirmHealth(p, null, true)">保存为另一次事件</el-button>
              </template>
            </div>
          </div>
          <details><summary>手动补充或纠正</summary><form class="memory-form" @submit.prevent="save">
            <h4>{{ long.items.some(i => i.kind === kind) ? '修改偏好' : '保存一条偏好' }}</h4>
            <label>记忆类型<select v-model="kind" :disabled="blocked" @change="value = ''"><option v-for="(opt, key) in long.options" :key="key" :value="key">{{ opt.label }}</option></select></label>
            <label>内容<select v-model="value" :disabled="blocked"><option value="" disabled>请选择</option><option v-for="(label, key) in long.options[kind]?.values || {}" :key="key" :value="key">{{ label }}</option></select></label>
            <el-button native-type="submit" type="primary" :disabled="blocked || !value">保存长期偏好</el-button>
          </form></details>
        </template>
      </section>
    </template>
  </el-drawer>
</template>

<script setup>
/* global defineProps, defineEmits */
import { ref, computed, watch } from 'vue'
import axios from 'axios'
import { API_BASE_URL } from '../config/api'
import { useChatStore } from '../stores/chat'
import NaturalMemoryInbox from './NaturalMemoryInbox.vue'
const props = defineProps({ open: Boolean })
defineEmits(['update:open'])
const chat = useChatStore()
const naturalAvailable = ref(false)
const short = ref(null), long = ref({ enabled: true, items: [], options: {}, versions: {}, version: 0 })
const loading = ref(false), busy = ref(false), error = ref(''), kind = ref('style'), value = ref('')
let generation = 0
const blocked = computed(() => loading.value || busy.value || chat.loading || chat.syncing)
const fieldLabel = key => ({ objects: '讨论对象', field: '资料范围', format: '回答形式' }[key] || key)
const formatTime = text => new Date(text).toLocaleString('zh-CN')
const healthLabel = status => ({ active: '已保存', paused: '已停用', archived: '已归档', invalid: '来源待核对' }[status] || '状态待核对')
const taskLabel = status => ({ queued: '等待整理', running: '正在整理', completed: '已完成', stale: '来源已变化，请刷新后重新整理', failed: '整理未完成，可重试', exhausted: '重试已停止，请重新核对' }[status] || '待核对')
async function load() {
  const token = ++generation
  short.value = null; long.value = { enabled: true, items: [], options: {}, versions: {}, version: 0 }; error.value = ''; value.value = ''
  loading.value = false
  if (!props.open || !chat.activeSession) return
  loading.value = true
  const session = chat.activeSession
  try {
    const [s, l] = await Promise.all([
      chat.usesServerHistory ? axios.get(`${API_BASE_URL}/conversations/${session.id}/memory/`) : axios.post(`${API_BASE_URL}/memory/preview/`, {
        history: session.messages.slice(session.memoryStart || 0).map(m => ({ role: m.role, content: m.content.slice(0, 4000) }))
      }),
      chat.usesServerHistory ? axios.get(`${API_BASE_URL}/memory/`) : Promise.resolve({ data: long.value })
    ])
    if (token !== generation) return
    short.value = s.data; long.value = l.data
  } catch (e) {
    if (token === generation) error.value = e.response?.data?.error || '记忆加载失败，请关闭后重试。'
  } finally { if (token === generation) loading.value = false }
}
async function mutate(fn) {
  if (blocked.value) return
  busy.value = true
  const token = generation
  try {
    await fn()
    if (token === generation) {
      // Memory mutations invalidate the selection bound to paused tasks.
      // Keep historical messages, but do not offer a known-stale continuation.
      for (const session of chat.sessions || []) {
        for (const message of session.messages) message.resumeToken = null
      }
      await load()
    }
  } catch (e) {
    if (token === generation) error.value = e.response?.data?.error || '操作未完成，可能已在其他页面更新，请关闭后重新打开。'
  } finally { busy.value = false }
}
const reset = () => mutate(() => chat.resetMemory())
const toggle = enabled => mutate(() => axios.patch(`${API_BASE_URL}/memory/`, { enabled, version: long.value.version }))
const toggleAuto = auto_enabled => mutate(() => axios.patch(`${API_BASE_URL}/memory/`, { auto_enabled, version: long.value.version }))
const toggleHealth = health_collect_enabled => mutate(() => axios.patch(`${API_BASE_URL}/memory/`, { health_collect_enabled, version: long.value.version }))
const healthAction = (item, action) => mutate(() => axios.post(`${API_BASE_URL}/memory/health/${item.id}/`, { action, version: item.version }))
const maintain = () => mutate(() => axios.post(`${API_BASE_URL}/memory/maintenance/`, { request_key: `maintenance-${Date.now()}-${Math.random().toString(16).slice(2)}`, version: long.value.version }))
const dismissHealth = p => mutate(() => axios.post(`${API_BASE_URL}/memory/health/proposals/${p.id}/confirm/`, { profile_version: p.profile_version, dismiss: true }))
const confirmHealth = (p, item = null, newEvent = false) => mutate(() => axios.post(`${API_BASE_URL}/memory/health/proposals/${p.id}/confirm/`, {
  profile_version: p.profile_version,
  ...(item ? { replace_id: item.id, target_version: item.version } : {}),
  ...(newEvent ? { new_event: true } : {})
}))
const edit = item => { kind.value = item.kind; value.value = item.value }
const saveItem = (item, status) => mutate(() => axios.post(`${API_BASE_URL}/memory/`, { kind: item.kind, value: item.value, status, version: item.version }))
const save = () => mutate(() => axios.post(`${API_BASE_URL}/memory/`, { kind: kind.value, value: value.value, version: long.value.versions[kind.value] || 0 }))
const forget = item => mutate(() => axios.post(`${API_BASE_URL}/memory/${item.id}/forget/`, { version: item.version }))
watch(() => [props.open, chat.activeSessionId, chat.currentUserKey], load, { immediate: true })
watch(() => chat.loading, (now, before) => { if (before && !now && props.open) load() })
</script>

<style scoped>
.intro, section > p { color: #65766b; line-height: 1.7; font-size: 13px; }
section { padding: 12px 0 22px; border-bottom: 1px solid #dce7df; }
h3 { color: #235f45; margin: 10px 0; } h4 { margin: 0; }
dl { display: grid; grid-template-columns: 90px 1fr; gap: 10px; font-size: 14px; } dd { margin: 0; } dt { color: #65766b; }
details { margin: 14px 0; font-size: 12px; overflow-wrap: anywhere; }
.memory-card { padding: 14px; margin: 12px 0; background: #f1f7f3; border-radius: 10px; }
.memory-card span { float: right; color: #65766b; font-size: 12px; } .memory-card p { margin: 9px 0; }
small { color: #65766b; } .actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.actions :deep(.el-button) { margin: 0; } .toggle { display: flex; align-items: center; gap: 6px; }
.memory-form { display: grid; gap: 14px; margin-top: 20px; }
.memory-form label { display: grid; gap: 7px; font-size: 13px; } select { padding: 9px; border: 1px solid #bacfc0; border-radius: 6px; width: 100%; background: white; }
.error { color: #a43939; } button { cursor: pointer; }
</style>
