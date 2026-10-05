import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { ArrowLeft, User, Lock, Message, Plus, Search, ChatDotRound, Delete, MoreFilled, Fold, CircleCheck, Right, Connection, InfoFilled, Promotion, FirstAidKit, Memo, Food } from '@element-plus/icons-vue'
const ElementPlusIconsVue = { ArrowLeft, User, Lock, Message, Plus, Search, ChatDotRound, Delete, MoreFilled, Fold, CircleCheck, Right, Connection, InfoFilled, Promotion, FirstAidKit, Memo, Food }
import App from './App.vue'
import router from './router'
import axios from 'axios'
import { API_BASE_URL } from './config/api'

// 配置 axios 默认值
axios.defaults.baseURL = API_BASE_URL

// 添加请求拦截器
axios.interceptors.request.use(config => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Token ${token}`
  }
  return config
})

const app = createApp(App)

// 注册 Element Plus 图标
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus)

app.mount('#app')
