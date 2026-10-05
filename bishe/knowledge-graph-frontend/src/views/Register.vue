<template>
  <div class="register-page">
    <button class="back-home light" @click="router.push('/')"><el-icon><ArrowLeft /></el-icon> 返回对话</button>
    <div class="register-card">
      <div class="visual-mark compact">杏</div>
      <p class="eyebrow">建立你的知识记录</p>
      <h2>创建账号</h2>
      <p class="subtitle">注册后可保存和继续历史会话。你也可以随时返回游客模式。</p>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <div class="two-columns">
          <el-form-item label="用户名" prop="username"><el-input v-model="form.username" size="large" placeholder="3–20 个字符" prefix-icon="User" /></el-form-item>
          <el-form-item label="邮箱" prop="email"><el-input v-model="form.email" size="large" placeholder="name@example.com" prefix-icon="Message" /></el-form-item>
        </div>
        <el-form-item label="密码" prop="password"><el-input v-model="form.password" size="large" type="password" placeholder="至少 6 位" prefix-icon="Lock" show-password /></el-form-item>
        <el-form-item label="确认密码" prop="confirmPassword"><el-input v-model="form.confirmPassword" size="large" type="password" placeholder="再次输入密码" prefix-icon="Lock" show-password @keyup.enter="submit" /></el-form-item>
        <el-alert v-if="userStore.error" :title="userStore.error" type="error" :closable="false" show-icon />
        <el-button class="submit-button" :loading="loading" @click="submit">注册并开始使用</el-button>
      </el-form>
      <div class="auth-switch">已经有账号？<button @click="router.push('/login')">直接登录</button></div>
    </div>
  </div>
</template>

<script>
export default { name: 'RegisterView' }
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
const form = reactive({ username: '', email: '', password: '', confirmPassword: '' })
const confirmPassword = (rule, value, callback) => {
  if (!value) callback(new Error('请再次输入密码'))
  else if (value !== form.password) callback(new Error('两次输入的密码不一致'))
  else callback()
}
const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }, { min: 3, max: 20, message: '长度需为 3–20 个字符', trigger: 'blur' }],
  email: [{ required: true, message: '请输入邮箱', trigger: 'blur' }, { type: 'email', message: '请输入有效邮箱', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }, { min: 6, message: '密码不少于 6 位', trigger: 'blur' }],
  confirmPassword: [{ validator: confirmPassword, trigger: 'blur' }]
}

const submit = async () => {
  if (!formRef.value || loading.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  const registered = await userStore.register(form.username, form.password, form.email)
  const loggedIn = registered ? await userStore.login(form.username, form.password) : false
  loading.value = false
  if (loggedIn) {
    ElMessage.success('注册成功，已为你登录')
    router.push('/')
  }
}
</script>

<style scoped src="../assets/auth.css"></style>
