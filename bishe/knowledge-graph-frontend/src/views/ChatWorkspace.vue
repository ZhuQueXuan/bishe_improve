<template>
  <div class="chat-shell">
    <div v-if="sidebarOpen" class="mobile-mask" @click="sidebarOpen = false" />

    <aside :class="['sidebar', { open: sidebarOpen }]">
      <div class="brand-row">
        <div class="brand-mark">杏</div>
        <div>
          <div class="brand-name">知医 Agent</div>
          <div class="brand-subtitle">Medical Graph Assistant</div>
        </div>
      </div>

      <button class="new-chat-button" :disabled="chatStore.syncing" @click="startNewChat">
        <el-icon><Plus /></el-icon>
        <span>新建对话</span>
      </button>

        <div class="history-heading">
          <span>{{ userStore.isLoggedIn ? '历史会话' : '本次对话' }}</span>
          <span>{{ chatStore.sessions.length }}</span>
        </div>
        <div class="history-search">
          <el-icon><Search /></el-icon>
          <input v-model="searchText" placeholder="搜索对话" />
        </div>
        <div class="session-list">
          <button
            v-for="session in filteredSessions"
            :key="session.id"
            :class="['session-item', { active: session.id === chatStore.activeSessionId }]"
            @click="selectSession(session.id)"
          >
            <el-icon><ChatDotRound /></el-icon>
            <span class="session-title">{{ session.title }}</span>
            <span class="session-delete" title="删除会话" @click.stop="removeSession(session.id)">
              <el-icon><Delete /></el-icon>
            </span>
          </button>
          <div v-if="!filteredSessions.length" class="history-empty">没有匹配的会话</div>
        </div>
      <div v-if="!userStore.isLoggedIn" class="guest-card">
        <div class="guest-icon"><el-icon><Lock /></el-icon></div>
        <div class="guest-title">当前为游客模式</div>
        <p>本次对话可随时切换，刷新页面后清空。登录后可长期保存历史。</p>
        <button class="guest-login" @click="router.push('/login')">登录并保存历史</button>
      </div>

      <div class="sidebar-footer">
        <template v-if="userStore.isLoggedIn">
          <div class="account-avatar">{{ userInitial }}</div>
          <div class="account-copy">
            <strong>{{ userStore.user?.username }}</strong>
            <span>已登录</span>
          </div>
          <el-dropdown trigger="click" @command="handleAccountCommand">
            <button class="more-button"><el-icon><MoreFilled /></el-icon></button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
        <template v-else>
          <div class="account-avatar guest"><el-icon><User /></el-icon></div>
          <div class="account-copy">
            <strong>游客</strong>
            <span>刷新后清空对话</span>
          </div>
          <button class="text-login" @click="router.push('/register')">注册</button>
        </template>
      </div>
    </aside>

    <main class="main-panel">
      <header class="topbar">
        <button class="mobile-menu" @click="sidebarOpen = true">
          <el-icon><Fold /></el-icon>
        </button>
        <div class="model-title">
          <span>医疗知识助手</span>
          <span class="model-badge"><i /> 图谱增强</span>
        </div>
        <div v-if="!userStore.isLoggedIn" class="auth-actions">
          <button class="login-link" @click="router.push('/login')">登录</button>
          <button class="register-link" @click="router.push('/register')">注册</button>
        </div>
        <div v-else class="saved-indicator">
          <el-icon><CircleCheck /></el-icon>
          {{ chatStore.syncing ? '正在加载历史会话…' : '历史已同步到后端' }}
        </div>
      </header>

      <section ref="messageArea" class="message-area">
        <div v-if="!activeMessages.length" class="welcome-state">
          <div class="welcome-orbit">
            <span class="orbit-dot one" />
            <span class="orbit-dot two" />
            <div class="welcome-logo">杏</div>
          </div>
          <p class="eyebrow">KNOWLEDGE GRAPH + LLM</p>
          <h1>今天想了解什么？</h1>
          <p class="welcome-copy">我会先从医疗知识图谱中检索相关信息，再结合大模型组织回答。</p>
          <div class="suggestion-grid">
            <button v-for="item in suggestions" :key="item.title" @click="useSuggestion(item.prompt)">
              <span class="suggestion-icon"><el-icon><component :is="item.icon" /></el-icon></span>
              <span>
                <strong>{{ item.title }}</strong>
                <small>{{ item.description }}</small>
              </span>
              <el-icon class="arrow"><Right /></el-icon>
            </button>
          </div>
        </div>

        <div v-else class="conversation">
          <article
            v-for="message in activeMessages"
            :key="message.id"
            :class="['message-row', message.role, { error: message.isError }]"
          >
            <div v-if="message.role === 'assistant'" class="message-avatar">杏</div>
            <div class="message-body">
              <div class="message-meta">
                <strong>{{ message.role === 'user' ? '你' : '知医 Agent' }}</strong>
                <span>{{ formatTime(message.timestamp) }}</span>
              </div>
              <MessageContent class="message-text" :content="visibleMessageContent(message)" />
              <button v-if="message.resumeToken" class="guest-login" :disabled="chatStore.loading" @click="resumeTask(message)">继续整理</button>
              <details v-if="message.retrieval?.task" class="graph-evidence">
                <summary>查看本次整理依据</summary>
                <div>本次查询 {{ message.retrieval.task.counts.reads }} 次；{{ message.retrieval.task.status === 'partial' ? '部分资料缺失' : message.retrieval.task.status === 'paused' ? '等待继续' : message.retrieval.task.status === 'complete' ? '所选资料已整理' : '整理已停止' }}。</div>
                <div v-for="item in message.retrieval.diseases" :key="item">{{ item }}</div>
              </details>
            </div>
          </article>

          <article v-if="chatStore.loading" class="message-row assistant">
            <div class="message-avatar">杏</div>
            <div class="message-body thinking-body">
              <div class="message-meta"><strong>知医 Agent</strong></div>
              <div class="thinking"><span /><span /><span /></div>
              <small>正在检索知识图谱并组织回答…</small>
            </div>
          </article>
        </div>
      </section>

      <footer class="composer-wrap">
        <div v-if="!userStore.isLoggedIn" class="guest-notice">
          <el-icon><InfoFilled /></el-icon>
          游客对话仅在本次使用期间保留，刷新后清空，<span @click="router.push('/register')">注册后长期保存</span>
        </div>
        <div class="composer">
          <el-input
            v-model="draft"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 5 }"
            maxlength="1000"
            resize="none"
            placeholder="输入你的医疗知识问题…"
            :disabled="chatStore.loading || chatStore.syncing"
            @keydown.enter.exact.prevent="send"
          />
          <div class="composer-bottom">
            <div class="mode-pill"><el-icon><Connection /></el-icon> 图谱检索</div>
            <label class="composer-hint"><input v-model="stepwise" type="checkbox" /> 先看关联结果</label>
            <div class="composer-hint">Shift + Enter 换行</div>
            <button class="send-button" :disabled="!draft.trim() || chatStore.loading || chatStore.syncing" @click="send">
              <el-icon><Promotion /></el-icon>
            </button>
          </div>
        </div>
        <p class="medical-note">当前问题、有效会话要求与检索资料会交给 DeepSeek 整理；讨论对象不是个人病历，回答不替代专业诊断。</p>
      </footer>
    </main>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import { useChatStore } from '../stores/chat'
import { visibleMessageContent } from '../utils/messageDisplay'
import MessageContent from '../components/MessageContent.vue'

const router = useRouter()
const userStore = useUserStore()
const chatStore = useChatStore()
const draft = ref('')
const stepwise = ref(false)
const searchText = ref('')
const sidebarOpen = ref(false)
const messageArea = ref(null)

const suggestions = [
  { title: '疾病知识', description: '了解症状、检查与治疗知识', prompt: '糖尿病常见的症状和检查项目有哪些？', icon: 'FirstAidKit' },
  { title: '用药查询', description: '查找疾病与药品的图谱关联', prompt: '高血压相关的常见药物有哪些？', icon: 'Memo' },
  { title: '饮食建议', description: '查看相关宜食与饮食禁忌', prompt: '胃炎患者在饮食上通常需要注意什么？', icon: 'Food' }
]

const activeMessages = computed(() => chatStore.activeSession?.messages || [])
const userInitial = computed(() => userStore.user?.username?.slice(0, 1).toUpperCase() || '用')
const filteredSessions = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  if (!keyword) return chatStore.sessions
  return chatStore.sessions.filter(session => session.title.toLowerCase().includes(keyword))
})

const scrollToBottom = async () => {
  await nextTick()
  if (messageArea.value) messageArea.value.scrollTop = messageArea.value.scrollHeight
}

const initializeChat = async () => {
  try {
    await chatStore.initialize(userStore.isLoggedIn ? userStore.user : null)
  } catch (error) {
    ElMessage.error('后端历史会话加载失败')
  }
}

const startNewChat = async () => {
  if (chatStore.syncing) return
  try {
    await chatStore.createSession()
    sidebarOpen.value = false
  } catch (error) {
    ElMessage.error('新建会话失败')
  }
}

const selectSession = (sessionId) => {
  chatStore.selectSession(sessionId)
  sidebarOpen.value = false
  scrollToBottom()
}

const removeSession = async (sessionId) => {
  try {
    await chatStore.removeSession(sessionId)
  } catch (error) {
    ElMessage.error('删除会话失败')
  }
}

const useSuggestion = (prompt) => {
  draft.value = prompt
  send()
}

const send = async () => {
  const content = draft.value.trim()
  if (!content || chatStore.loading || chatStore.syncing) return
  draft.value = ''
  scrollToBottom()
  try {
    await chatStore.sendMessage(content, { stepwise: stepwise.value })
  } catch (error) {
    ElMessage.error('问答服务暂时不可用，请稍后重试')
  }
  scrollToBottom()
}

const resumeTask = async (message) => {
  try {
    await chatStore.sendMessage('继续整理', { resumeToken: message.resumeToken })
  } catch (error) {
    ElMessage.error('进度已失效或服务暂时不可用，请重新提交问题')
  }
  scrollToBottom()
}

const handleAccountCommand = async (command) => {
  if (command === 'logout') {
    userStore.logout()
    await chatStore.initialize(null)
    ElMessage.success('已退出登录，现在为游客模式')
  }
}

const formatTime = (timestamp) => new Date(timestamp).toLocaleTimeString('zh-CN', {
  hour: '2-digit',
  minute: '2-digit'
})

watch(
  () => [userStore.token, userStore.user?.id],
  initializeChat
)

watch(
  () => activeMessages.value.length,
  scrollToBottom
)

onMounted(() => {
  initializeChat()
  scrollToBottom()
})
</script>

<style scoped>
.chat-shell { height: 100vh; display: flex; overflow: hidden; background: #f8f9f7; }
.sidebar { width: 286px; flex: 0 0 286px; display: flex; flex-direction: column; padding: 22px 16px 16px; color: #edf5f0; background: #17231e; border-right: 1px solid rgba(255,255,255,.05); }
.brand-row { display: flex; align-items: center; gap: 11px; padding: 0 8px 22px; }
.brand-mark, .welcome-logo { display: grid; place-items: center; color: #fff; background: linear-gradient(145deg, #3e8d6b, #246047); box-shadow: 0 8px 28px rgba(37,104,76,.28); }
.brand-mark { width: 38px; height: 38px; border-radius: 11px; font-family: serif; font-weight: 700; }
.brand-name { font-size: 16px; font-weight: 700; letter-spacing: .03em; }
.brand-subtitle { margin-top: 2px; color: #91a59a; font-size: 10px; letter-spacing: .08em; }
button { border: 0; cursor: pointer; }
.new-chat-button { width: 100%; height: 45px; display: flex; align-items: center; justify-content: center; gap: 8px; border-radius: 11px; color: #f4fbf7; background: #2d7658; font-weight: 650; transition: .2s ease; }
.new-chat-button:hover { background: #378467; transform: translateY(-1px); }
.history-heading { display: flex; justify-content: space-between; padding: 25px 7px 9px; color: #8fa298; font-size: 12px; }
.history-search { height: 38px; display: flex; align-items: center; gap: 8px; padding: 0 11px; margin-bottom: 10px; border: 1px solid #34433c; border-radius: 9px; color: #809189; background: #1d2a24; }
.history-search input { width: 100%; border: 0; outline: 0; color: #dce7e1; background: transparent; font-size: 12px; }
.history-search input::placeholder { color: #72827a; }
.session-list { flex: 1; min-height: 0; overflow-y: auto; }
.session-item { width: 100%; display: flex; align-items: center; gap: 9px; padding: 11px 10px; margin: 2px 0; border-radius: 9px; color: #aebdb5; background: transparent; text-align: left; }
.session-item:hover, .session-item.active { color: #f6fbf8; background: #26372f; }
.session-title { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 13px; }
.session-delete { display: none; padding: 3px; color: #8fa298; }
.session-item:hover .session-delete { display: inline-flex; }
.history-empty { padding: 20px 8px; color: #73867b; font-size: 12px; text-align: center; }
.guest-card { margin-top: 25px; padding: 20px 15px; border: 1px solid #33463c; border-radius: 14px; background: #1e2d26; text-align: center; }
.guest-icon { width: 38px; height: 38px; display: grid; place-items: center; margin: 0 auto 11px; border-radius: 50%; color: #96c5ad; background: #2a4035; }
.guest-title { font-size: 14px; font-weight: 650; }
.guest-card p { margin: 8px 0 15px; color: #8fa298; font-size: 12px; line-height: 1.65; }
.guest-login { width: 100%; padding: 9px; border-radius: 8px; color: #cce9da; background: #2a4035; font-size: 12px; }
.sidebar-footer { min-height: 58px; display: flex; align-items: center; gap: 10px; margin-top: auto; padding: 12px 5px 0; border-top: 1px solid #2a3932; }
.account-avatar { width: 35px; height: 35px; display: grid; place-items: center; flex: 0 0 35px; border-radius: 10px; color: #173125; background: #91d0b0; font-weight: 700; }
.account-avatar.guest { color: #9fb0a7; background: #293830; }
.account-copy { min-width: 0; flex: 1; display: flex; flex-direction: column; }
.account-copy strong { overflow: hidden; font-size: 13px; text-overflow: ellipsis; }
.account-copy span { margin-top: 2px; color: #788a80; font-size: 10px; }
.more-button, .text-login { padding: 6px; color: #a8bab0; background: transparent; }
.text-login { color: #9fd4b8; font-size: 12px; }
.main-panel { min-width: 0; flex: 1; display: flex; flex-direction: column; }
.topbar { height: 64px; flex: 0 0 64px; display: flex; align-items: center; padding: 0 28px; border-bottom: 1px solid #e5e9e5; background: rgba(255,255,255,.86); backdrop-filter: blur(10px); }
.model-title { display: flex; align-items: center; gap: 10px; font-size: 14px; font-weight: 650; }
.model-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 9px; border-radius: 20px; color: #39745a; background: #edf7f1; font-size: 11px; font-weight: 500; }
.model-badge i { width: 6px; height: 6px; border-radius: 50%; background: #42a876; box-shadow: 0 0 0 3px #d7eee1; }
.auth-actions { display: flex; gap: 8px; margin-left: auto; }
.auth-actions button { padding: 8px 15px; border-radius: 8px; font-size: 13px; }
.login-link { color: #31453b; background: transparent; }
.register-link { color: #fff; background: #276c50; }
.saved-indicator { display: flex; align-items: center; gap: 6px; margin-left: auto; color: #6c7d74; font-size: 12px; }
.mobile-menu { display: none; margin-right: 10px; padding: 7px; color: #52645b; background: transparent; }
.message-area { flex: 1; min-height: 0; overflow-y: auto; scroll-behavior: smooth; }
.welcome-state { width: min(760px, calc(100% - 40px)); min-height: 100%; display: flex; flex-direction: column; justify-content: center; align-items: center; padding: 45px 0 32px; margin: 0 auto; text-align: center; }
.welcome-orbit { position: relative; width: 84px; height: 84px; display: grid; place-items: center; margin-bottom: 16px; border: 1px solid #dbe8df; border-radius: 50%; }
.welcome-logo { width: 54px; height: 54px; border-radius: 18px; font-family: serif; font-size: 22px; }
.orbit-dot { position: absolute; width: 7px; height: 7px; border-radius: 50%; background: #64a888; }
.orbit-dot.one { top: 8px; right: 11px; }.orbit-dot.two { bottom: 10px; left: 8px; background: #b7d5c5; }
.eyebrow { margin: 0 0 8px; color: #5b8c73; font-size: 10px; font-weight: 700; letter-spacing: .16em; }
.welcome-state h1 { margin: 0; color: #17251e; font-family: Georgia, 'Noto Serif SC', serif; font-size: clamp(30px, 4vw, 42px); font-weight: 600; letter-spacing: -.03em; }
.welcome-copy { max-width: 540px; margin: 14px 0 28px; color: #78867f; font-size: 14px; line-height: 1.8; }
.suggestion-grid { width: 100%; display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.suggestion-grid button { min-height: 112px; display: flex; align-items: flex-start; gap: 11px; padding: 17px 15px; border: 1px solid #e1e7e2; border-radius: 14px; color: #304139; background: #fff; text-align: left; box-shadow: 0 6px 22px rgba(34,63,49,.04); transition: .2s ease; }
.suggestion-grid button:hover { border-color: #a7cbb8; transform: translateY(-2px); box-shadow: 0 10px 26px rgba(34,83,59,.08); }
.suggestion-icon { width: 31px; height: 31px; display: grid; place-items: center; flex: 0 0 31px; border-radius: 9px; color: #3b7c5e; background: #edf6f1; }
.suggestion-grid strong, .suggestion-grid small { display: block; }.suggestion-grid strong { margin: 2px 0 6px; font-size: 13px; }.suggestion-grid small { color: #849089; font-size: 11px; line-height: 1.5; }
.suggestion-grid .arrow { align-self: center; margin-left: auto; color: #aab6af; }
.conversation { width: min(820px, calc(100% - 48px)); padding: 42px 0 24px; margin: 0 auto; }
.message-row { display: flex; gap: 13px; margin-bottom: 30px; }
.message-row.user { justify-content: flex-end; }
.message-avatar { width: 34px; height: 34px; display: grid; place-items: center; flex: 0 0 34px; border-radius: 11px; color: #fff; background: #2e7558; font-family: serif; font-size: 13px; }
.message-body { max-width: 76%; }
.message-row.user .message-body { padding: 12px 16px; border-radius: 15px 15px 3px 15px; background: #e5f0e9; }
.message-row.assistant .message-body { flex: 1; max-width: calc(100% - 48px); padding-top: 2px; }
.message-meta { display: flex; align-items: center; gap: 9px; margin-bottom: 7px; font-size: 12px; }.message-meta span { color: #9aa59f; font-size: 10px; }
.message-text { color: #25342d; font-size: 14px; line-height: 1.85; white-space: pre-wrap; word-break: break-word; }
.message-row.error .message-text { color: #a84e4e; }
.graph-evidence { width: fit-content; display: flex; align-items: center; gap: 5px; margin-top: 10px; padding: 5px 9px; border: 1px solid #dbeadf; border-radius: 7px; color: #4c7d64; background: #f3f8f5; font-size: 10px; }
.thinking-body small { color: #8a9790; font-size: 11px; }.thinking { display: flex; gap: 4px; margin: 9px 0; }.thinking span { width: 6px; height: 6px; border-radius: 50%; background: #5c9478; animation: pulse 1.2s infinite ease-in-out; }.thinking span:nth-child(2) { animation-delay: .16s; }.thinking span:nth-child(3) { animation-delay: .32s; }
@keyframes pulse { 0%, 60%, 100% { opacity: .25; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-3px); } }
.composer-wrap { flex: 0 0 auto; padding: 0 24px 16px; background: linear-gradient(to top, #f8f9f7 80%, rgba(248,249,247,0)); }
.guest-notice { width: min(760px, 100%); display: flex; justify-content: center; align-items: center; gap: 5px; margin: 0 auto 7px; color: #89948e; font-size: 11px; }.guest-notice span { color: #39785a; cursor: pointer; }
.composer { width: min(820px, 100%); padding: 10px 12px 9px 16px; margin: 0 auto; border: 1px solid #d9dfda; border-radius: 17px; background: #fff; box-shadow: 0 8px 30px rgba(35,61,48,.09); }
.composer :deep(.el-textarea__inner) { min-height: 30px !important; padding: 4px 0; border: 0; box-shadow: none; color: #26372e; background: transparent; font-size: 14px; line-height: 1.6; }
.composer-bottom { display: flex; align-items: center; margin-top: 7px; }
.mode-pill { display: flex; align-items: center; gap: 5px; padding: 5px 8px; border-radius: 7px; color: #4a735e; background: #eef6f1; font-size: 10px; }
.composer-hint { margin-left: auto; margin-right: 10px; color: #a0aaa4; font-size: 10px; }
.send-button { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 10px; color: #fff; background: #2b7054; transition: .2s ease; }.send-button:hover:not(:disabled) { background: #205d44; transform: translateY(-1px); }.send-button:disabled { cursor: not-allowed; opacity: .35; }
.medical-note { margin: 8px 0 0; color: #9aa49f; font-size: 9px; text-align: center; }
.mobile-mask { display: none; }

@media (max-width: 800px) {
  .sidebar { position: fixed; z-index: 30; left: 0; top: 0; bottom: 0; transform: translateX(-100%); transition: transform .24s ease; }
  .sidebar.open { transform: translateX(0); }
  .mobile-mask { position: fixed; z-index: 20; inset: 0; display: block; background: rgba(13,24,19,.4); }
  .mobile-menu { display: inline-grid; place-items: center; }
  .topbar { padding: 0 14px; }
  .model-badge, .saved-indicator { display: none; }
  .suggestion-grid { grid-template-columns: 1fr; }
  .suggestion-grid button { min-height: 80px; }
  .welcome-state { justify-content: flex-start; padding-top: 46px; }
  .conversation { width: calc(100% - 28px); padding-top: 28px; }
  .message-body { max-width: 86%; }
  .composer-wrap { padding: 0 12px 10px; }
  .composer-hint { display: none; }
  .auth-actions button { padding: 7px 10px; }
}
</style>
