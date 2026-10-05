<template>
  <div class="auth-page">
    <button class="back-home" @click="router.push('/')"><el-icon><ArrowLeft /></el-icon> 返回对话</button>
    <section class="auth-visual">
      <div class="visual-content">
        <div class="visual-mark">杏</div>
        <p class="visual-eyebrow">MEDICAL GRAPH AGENT</p>
        <h1>让每次追问<br />都有上下文。</h1>
        <span>登录后，你的历史会话会保存在账号下，刷新页面或重新登录后都能继续。</span>
      </div>
      <div class="visual-grid" />
    </section>
    <section class="auth-form-side">
      <div class="auth-card">
        <div class="mobile-brand"><span>杏</span> 知医 Agent</div>
        <p class="eyebrow">欢迎回来</p>
        <h2>登录账号</h2>
        <p class="subtitle">继续你之前的医疗知识探索。</p>
        <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" size="large" placeholder="输入用户名" prefix-icon="User" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" size="large" type="password" placeholder="输入密码" prefix-icon="Lock" show-password @keyup.enter="submit" />
          </el-form-item>
          <el-alert v-if="userStore.error" :title="userStore.error" type="error" :closable="false" show-icon />
          <el-button class="submit-button" :loading="loading" @click="submit">登录并继续</el-button>
        </el-form>
        <div class="auth-switch">还没有账号？<button @click="router.push('/register')">免费注册</button></div>
        <button class="guest-entry" @click="router.push('/')">暂不登录，以游客身份使用</button>
      </div>
    </section>
  </div>
</template>

<script>
export default { name: 'LoginView' }
</script>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'

const router = useRouter()
const userStore = useUserStore()
const formRef = ref(null)
const loading = ref(false)
const form = reactive({ username: '', password: '' })
const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

const submit = async () => {
  if (!formRef.value || loading.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  const success = await userStore.login(form.username, form.password)
  loading.value = false
  if (success) {
    ElMessage.success('登录成功')
    router.push('/')
  }
}
</script>

<style scoped src="../assets/auth.css"></style>
