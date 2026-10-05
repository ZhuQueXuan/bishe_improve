<template>
  <div>
    <template v-for="(block, index) in blocks" :key="index">
      <div v-if="block.type === 'table'" class="table-scroll">
        <table>
          <thead><tr><th v-for="(cell, column) in block.header" :key="column">{{ cell }}</th></tr></thead>
          <tbody><tr v-for="(row, rowIndex) in block.rows" :key="rowIndex"><td v-for="(cell, column) in row" :key="column">{{ cell }}</td></tr></tbody>
        </table>
      </div>
      <div v-else>{{ block.text }}</div>
    </template>
  </div>
</template>

<script setup>
import { computed, defineProps } from 'vue'
import { messageBlocks } from '../utils/messageBlocks'
const props = defineProps({ content: { type: String, default: '' } })
const blocks = computed(() => messageBlocks(props.content))
</script>

<style scoped>
.table-scroll { overflow-x: auto; margin: 12px 0; }
table { border-collapse: collapse; width: 100%; white-space: normal; font-size: 13px; }
th, td { border: 1px solid #dbe4de; padding: 9px; text-align: left; vertical-align: top; min-width: 90px; }
th { background: #edf5f0; }
</style>
