<template>
  <details v-if="sources.length" class="source-references">
    <summary>查看资料出处（{{ sources.length }}）</summary>
    <p class="source-note">资料尚未经医学专业审核；文档仅收录选定页面。</p>
    <div v-for="(source, index) in sources" :key="`${source.evidence_id}-${index}`" class="source-item">
      <strong>[{{ source.evidence_id }}] {{ source.title }}</strong>
      <template v-if="source.kind === 'document'">
        <div>{{ source.edition }} · PDF 第 {{ source.page }} 页</div>
        <div class="source-note">{{ source.publisher }}</div>
        <p class="source-excerpt">{{ source.excerpt }}</p>
        <p class="source-note">收录范围：{{ displayScope(source.scope_note) }}</p>
        <div class="source-links">
          <a v-if="documentLink(source)" :href="documentLink(source)" target="_blank" rel="noopener noreferrer">打开原文此页</a>
          <a v-if="officialLink(source)" :href="officialLink(source)" target="_blank" rel="noopener noreferrer">官方发布页</a>
        </div>
      </template>
      <div v-else>{{ source.disease }} · {{ source.label }} · medical.json 第 {{ source.line }} 行</div>
    </div>
  </details>
</template>

<script setup>
import { defineProps } from 'vue'
import { documentLink, officialLink } from '../utils/sourceLinks'
defineProps({ sources: { type: Array, default: () => [] } })
const displayScope = (text) => (text || '').replace(/，?not_verified[。.]?/g, '。')
</script>

<style scoped>
.source-references { margin-top: 16px; padding: 12px 16px; border: 1px solid #dce5e1; border-radius: 12px; font-size: 13px; color: #3d5950; overflow-wrap: anywhere; }
summary { cursor: pointer; font-weight: 600; }
.source-item { padding: 14px 0; border-top: 1px solid #e5ece8; line-height: 1.7; }
.source-note { color: #71837b; font-size: 12px; }
.source-excerpt { white-space: pre-wrap; max-height: 150px; overflow-y: auto; padding: 10px; background: #f5f8f6; border-radius: 8px; }
.source-links { display: flex; gap: 18px; flex-wrap: wrap; }
a { color: #13765e; text-decoration: underline; }
</style>
