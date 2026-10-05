import { defineStore } from 'pinia'
import axios from 'axios'
import { API_BASE_URL } from '../config/api'

const readStoredUser = () => {
  try {
    return JSON.parse(localStorage.getItem('user') || 'null')
  } catch (error) {
    localStorage.removeItem('user')
    return null
  }
}

export const useUserStore = defineStore('user', {
  state: () => ({
    token: localStorage.getItem('token') || null,
    user: readStoredUser(),
    error: null
  }),
  
  getters: {
    isLoggedIn: (state) => !!state.token
  },
  
  actions: {
    async login(username, password) {
      this.error = null
      try {
        const { data } = await axios.post(`${API_BASE_URL}/auth/login/`, {
          username,
          password
        })
        
        const { token, user } = data
        this.token = token
        this.user = user
        localStorage.setItem('token', token)
        localStorage.setItem('user', JSON.stringify(user))
        return true
      } catch (error) {
        this.error = error.response?.data?.error || '登录失败，请稍后重试'
        return false
      }
    },
    
    async register(username, password, email) {
      this.error = null
      try {
        await axios.post(`${API_BASE_URL}/auth/register/`, {
          username,
          password,
          email
        })
        
        return true
      } catch (error) {
        this.error = error.response?.data?.error || '注册失败，请稍后重试'
        return false
      }
    },
    
    logout() {
      this.token = null
      this.user = null
      this.error = null
      localStorage.removeItem('token')
      localStorage.removeItem('user')
    }
  }
}) 
