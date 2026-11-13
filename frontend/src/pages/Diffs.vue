<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getJSON, postJSON } from '../api'

const route = useRoute()
const router = useRouter()
const rows = ref([])
const err = ref('')
const busyId = ref(null)
const kindClass = { left_only: 'warn', right_only: 'warn', field_mismatch: 'danger' }
const kindLabel = { left_only: '仅左有', right_only: '仅右有', field_mismatch: '字段不一致' }
const stateClass = { undisposed: 'muted-tag', open: 'warn', closed: 'ok' }
const stateLabel = { undisposed: '未处置', open: '处置中', closed: '已关闭' }

async function refresh() {
  const q = route.query.batch_id ? `?batch_id=${route.query.batch_id}` : ''
  rows.value = await getJSON(`/api/diffs${q}`)
}

async function openDisposition(diff) {
  err.value = ''
  busyId.value = diff.id
  try {
    const t = await postJSON('/api/dispositions', { diff_id: diff.id })
    await refresh()
    router.push({ path: '/dispositions', query: { ticket: t.id } })
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busyId.value = null
  }
}

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
watch(() => route.query.batch_id, async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>差异清单</h1>
    <p class="muted">
      对齐后的差异行。差异处置闭环：开处置单 → 认领 → 回灌侧数据 → 复核 → 关闭。
      关单后差异历史保留，状态与处置结果在此互证。
      <span v-if="route.query.batch_id">当前过滤批次 #{{ route.query.batch_id }}</span>
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr><th>ID</th><th>批次</th><th>类型</th><th>对齐键</th><th>不一致字段</th><th>左</th><th>右</th><th>处置状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in rows" :key="d.id" :class="d.disposition_state === 'closed' ? 'row-ok' : (d.disposition_state === 'open' ? 'row-open' : '')">
            <td>{{ d.id }}</td>
            <td>{{ d.batch_id }}</td>
            <td><span class="tag" :class="kindClass[d.kind]">{{ kindLabel[d.kind] || d.kind }}</span></td>
            <td class="mono">{{ JSON.stringify(d.align_key) }}</td>
            <td class="mono">{{ (d.mismatched_fields || []).join(', ') || '—' }}</td>
            <td class="mono">{{ d.left ? JSON.stringify(d.left) : '—' }}</td>
            <td class="mono">{{ d.right ? JSON.stringify(d.right) : '—' }}</td>
            <td>
              <span class="tag" :class="stateClass[d.disposition_state]">
                {{ d.disposition ? d.disposition.status_label : stateLabel[d.disposition_state] }}
              </span>
              <div v-if="d.disposition" class="muted" style="font-size:0.78rem">
                #{{ d.disposition.ticket_id }}<span v-if="d.disposition.assignee"> · {{ d.disposition.assignee }}</span>
              </div>
            </td>
            <td>
              <button v-if="!d.disposition" :disabled="busyId === d.id" @click="openDisposition(d)">
                {{ busyId === d.id ? '开单中…' : '开处置单' }}
              </button>
              <router-link v-else :to="{ path: '/dispositions', query: { ticket: d.disposition.ticket_id } }">
                查看 #{{ d.disposition.ticket_id }}
              </router-link>
            </td>
          </tr>
          <tr v-if="!rows.length"><td colspan="9" class="muted">暂无差异。去对账台跑一趟后回到这里。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
