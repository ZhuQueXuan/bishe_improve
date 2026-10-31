const configured = process.env.VUE_APP_API_BASE_URL || 'http://127.0.0.1:8000/api'
// Resolve a configured same-origin path once. Axios also has a /api baseURL;
// leaving this relative would otherwise request /api/api/... .
export const API_BASE_URL = (configured.startsWith('/') && typeof window !== 'undefined'
  ? new URL(configured, window.location.origin).href
  : configured).replace(/\/$/, '')
