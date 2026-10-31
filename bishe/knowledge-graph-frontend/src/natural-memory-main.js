// Separate CLI entry. The production main/router/store do not import this page.
import { createApp } from 'vue'
import NaturalMemoryPanel from './views/NaturalMemoryPanel.vue'
document.title = '个人记忆 · 隔离验收'
createApp(NaturalMemoryPanel).mount('#app')
