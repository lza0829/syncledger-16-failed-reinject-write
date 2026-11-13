<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getJSON, postJSON } from '../api'

const route = useRoute()
const tickets = ref([])
const evidence = ref(null)
const snapshots = ref([])
const selectedId = ref(null)
const detail = ref(null)
const err = ref('')
const busy = ref(false)

// action forms
const assignee = ref('')
const side = ref('right')
const fieldName = ref('')
const fieldValue = ref('')
const simulateFailure = ref(false)
const rejectReason = ref('')

const statusClass = {
  pending_claim: 'info',
  in_progress: 'warn',
  pending_review: 'danger',
  closed: 'ok',
}

async function refreshAll(keepSelection = true) {
  const [list, ev, snaps] = await Promise.all([
    getJSON('/api/dispositions'),
    getJSON('/api/dispositions/evidence'),
    getJSON('/api/side-snapshots'),
  ])
  tickets.value = list
  evidence.value = ev
  snapshots.value = snaps
  if (keepSelection && selectedId.value) await selectTicket(selectedId.value)
}

async function selectTicket(id) {
  err.value = ''
  selectedId.value = id
  try {
    detail.value = await getJSON(`/api/dispositions/${id}`)
    if (!fieldName.value && detail.value.diff?.mismatched_fields?.length) {
      fieldName.value = detail.value.diff.mismatched_fields[0]
    }
  } catch (e) {
    err.value = String(e.message || e)
    detail.value = null
  }
}

async function act(fn) {
  err.value = ''
  busy.value = true
  try {
    await fn()
    await refreshAll()
  } catch (e) {
    err.value = String(e.message || e)
    await refreshAll()
  } finally {
    busy.value = false
  }
}

function doClaim() {
  return act(() => postJSON(`/api/dispositions/${selectedId.value}/claim`, { assignee: assignee.value }))
}

function parsedValue() {
  const name = fieldName.value
  const raw = fieldValue.value
  if (name === 'qty' || name === 'amount') {
    const n = Number(raw)
    return Number.isFinite(n) ? n : raw
  }
  return raw
}

function doReinject() {
  if (!fieldName.value) {
    err.value = '请选择回灌字段'
    return
  }
  const fields = { [fieldName.value]: parsedValue() }
  return act(() =>
    postJSON(`/api/dispositions/${selectedId.value}/reinject`, {
      side: side.value,
      fields,
      simulate_failure: simulateFailure.value,
    })
  )
}

function doSubmit() {
  return act(() => postJSON(`/api/dispositions/${selectedId.value}/submit-review`))
}

function doApprove() {
  return act(() => postJSON(`/api/dispositions/${selectedId.value}/review`, { approved: true }))
}

function doReject() {
  if (!rejectReason.value.trim()) {
    err.value = '驳回必须填写原因'
    return
  }
  return act(() =>
    postJSON(`/api/dispositions/${selectedId.value}/review`, {
      approved: false,
      reason: rejectReason.value,
    })
  ).then(() => { rejectReason.value = '' })
}

onMounted(async () => {
  try {
    await refreshAll(false)
    const q = Number(route.query.ticket)
    if (q && tickets.value.some((t) => t.id === q)) await selectTicket(q)
  } catch (e) {
    err.value = String(e.message || e)
  }
})
watch(() => route.query.ticket, async (q) => {
  const id = Number(q)
  if (id && id !== selectedId.value) await selectTicket(id)
})
</script>
<template>
  <div class="page">
    <h1>差异处置</h1>
    <p class="muted">
      处置单状态机：待认领 → 处理中 → 待复核 → 已关闭；复核驳回回处理中。非法跳转服务端拒绝，
      回灌失败留在处理中并留痕，复核以侧上实际值与回灌记录逐条比对。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <div class="panel" v-if="evidence">
      <h3 style="margin-top:0">互证总览（状态数量 · 回灌台账 · 侧数据 · 差异列表）</h3>
      <div class="stat-grid">
        <div class="stat" v-for="(label, code) in evidence.status_labels" :key="code">
          <div class="stat-num">{{ evidence.status_counts[code] || 0 }}</div>
          <div class="stat-label">{{ label }}</div>
        </div>
        <div class="stat">
          <div class="stat-num">{{ evidence.undisposed_diffs }}</div>
          <div class="stat-label">未处置差异</div>
        </div>
        <div class="stat">
          <div class="stat-num">{{ evidence.reinject_counts.success }}</div>
          <div class="stat-label">回灌成功</div>
        </div>
        <div class="stat">
          <div class="stat-num">{{ evidence.reinject_counts.failed }}</div>
          <div class="stat-label">回灌失败</div>
        </div>
      </div>
      <p style="margin-bottom:0">
        互证检查：
        <span class="tag" :class="evidence.all_pass ? 'ok' : 'danger'">
          {{ evidence.all_pass ? '全部通过' : '存在不符' }}
        </span>
        <span v-for="c in evidence.checks" :key="c.name" style="margin-left:0.6rem">
          <span class="tag" :class="c.pass ? 'ok' : 'danger'">{{ c.pass ? '✓' : '✗' }}</span>
          <span class="muted" style="font-size:0.82rem">{{ c.name }}</span>
        </span>
      </p>
    </div>

    <div class="detail-grid">
      <div class="panel">
        <h3 style="margin-top:0">处置单列表</h3>
        <table>
          <thead><tr><th>ID</th><th>标题</th><th>状态</th><th>处理人</th></tr></thead>
          <tbody>
            <tr v-for="t in tickets" :key="t.id"
                :style="t.id === selectedId ? 'background:rgba(31,95,139,0.1);cursor:pointer' : 'cursor:pointer'"
                @click="selectTicket(t.id)">
              <td>{{ t.id }}</td>
              <td style="max-width:16rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ t.title }}</td>
              <td><span class="tag" :class="statusClass[t.status]">{{ t.status_label }}</span></td>
              <td>{{ t.assignee || '—' }}</td>
            </tr>
            <tr v-if="!tickets.length"><td colspan="4" class="muted">暂无处置单，去差异清单开单。</td></tr>
          </tbody>
        </table>
      </div>

      <div class="panel" v-if="detail">
        <h3 style="margin-top:0">处置单 #{{ detail.id }}
          <span class="tag" :class="statusClass[detail.status]">{{ detail.status_label }}</span>
        </h3>
        <p class="kv" style="margin:0.2rem 0">
          <b>差异</b>#{{ detail.diff_id }} · {{ detail.diff?.kind }} ·
          <span class="mono">{{ JSON.stringify(detail.diff?.align_key) }}</span>
        </p>
        <p class="kv" v-if="detail.last_fail_reason" style="color:var(--danger)">
          <b>最近失败/驳回原因</b>{{ detail.last_fail_reason }}
        </p>

        <!-- 待认领 -->
        <div v-if="detail.status === 'pending_claim'" class="form-row">
          <label>处理人<input v-model="assignee" placeholder="输入认领人" /></label>
          <button :disabled="busy" @click="doClaim">认领（进入处理中）</button>
        </div>

        <!-- 处理中：回灌 -->
        <template v-if="detail.status === 'in_progress'">
          <h4 style="margin-bottom:0.3rem">回灌到侧</h4>
          <div class="form-row">
            <label>目标侧
              <select v-model="side">
                <option value="left">左侧（{{ detail.pair?.left_name }}）</option>
                <option value="right">右侧（{{ detail.pair?.right_name }}）</option>
              </select>
            </label>
            <label>约定字段
              <select v-model="fieldName">
                <option v-for="f in detail.pair?.compare_fields || []" :key="f" :value="f">{{ f }}</option>
              </select>
            </label>
            <label>新值<input v-model="fieldValue" :placeholder="'写入 ' + fieldName" /></label>
            <label style="align-self:center;display:flex;gap:0.3rem;align-items:center">
              <input type="checkbox" v-model="simulateFailure" style="min-width:0" /> 模拟侧写入失败
            </label>
            <button :disabled="busy" @click="doReinject">执行回灌</button>
          </div>
          <p class="muted" style="font-size:0.8rem;margin:0.2rem 0">
            回灌把约定字段写入指定侧快照并记录旧值/新值；勾选故障注入可验证失败留痕与状态约束。
          </p>
          <button class="ghost" :disabled="busy" @click="doSubmit">提交复核</button>
        </template>

        <!-- 待复核 -->
        <template v-if="detail.status === 'pending_review'">
          <h4 style="margin-bottom:0.3rem">复核（对照侧实际值与回灌记录）</h4>
          <p class="muted" style="font-size:0.82rem">
            服务端重新读取侧快照逐条比对；侧值与回灌记录不符时，通过会被拒绝。
          </p>
          <label>驳回原因（驳回时必填）<textarea v-model="rejectReason"></textarea></label>
          <div class="form-row">
            <button class="danger-btn" :disabled="busy" @click="doReject">驳回（回处理中）</button>
            <button :disabled="busy" @click="doApprove">复核通过并关闭</button>
          </div>
        </template>

        <p v-if="detail.status === 'closed'" class="tag ok">
          已关闭于 {{ detail.closed_at }}；原差异保留历史，不可再操作。
        </p>

        <!-- 侧实际值 -->
        <h4>侧上实际值（复核依据）</h4>
        <table>
          <thead><tr><th>侧</th><th>当前行</th></tr></thead>
          <tbody>
            <tr v-for="(row, s) in detail.side_view" :key="s">
              <td>{{ s === 'left' ? '左' : '右' }}</td>
              <td class="mono">{{ row ? JSON.stringify(row) : '（无此行）' }}</td>
            </tr>
          </tbody>
        </table>

        <!-- 回灌台账 -->
        <h4>回灌记录（旧值 → 新值）</h4>
        <table>
          <thead><tr><th>ID</th><th>侧</th><th>结果</th><th>旧值</th><th>新值</th><th>失败原因</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="r in detail.reinjects" :key="r.id">
              <td>{{ r.id }}</td>
              <td>{{ r.side }}</td>
              <td><span class="tag" :class="r.result === 'success' ? 'ok' : 'danger'">
                {{ r.result === 'success' ? '成功' : '失败' }}
              </span></td>
              <td class="mono">{{ JSON.stringify(r.old_values) }}</td>
              <td class="mono">{{ JSON.stringify(r.new_values) }}</td>
              <td style="max-width:14rem">{{ r.fail_reason || '—' }}</td>
              <td class="mono">{{ r.created_at }}</td>
            </tr>
            <tr v-if="!detail.reinjects.length"><td colspan="7" class="muted">尚无回灌记录。</td></tr>
          </tbody>
        </table>

        <!-- 事件流 -->
        <h4>状态事件流</h4>
        <table>
          <thead><tr><th>时间</th><th>动作</th><th>从</th><th>到</th><th>处理人</th><th>明细</th></tr></thead>
          <tbody>
            <tr v-for="e in detail.events" :key="e.id">
              <td class="mono">{{ e.created_at }}</td>
              <td>{{ e.action }}</td>
              <td>{{ e.from_status || '—' }}</td>
              <td>{{ e.to_status || '—' }}</td>
              <td>{{ e.actor || '—' }}</td>
              <td class="mono" style="max-width:16rem;overflow:hidden;text-overflow:ellipsis">{{ e.detail || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel" v-else>
        <h3 style="margin-top:0">处置单详情</h3>
        <p class="muted">从左侧选择一张处置单，或到差异清单对未处置差异开单。</p>
      </div>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">侧快照（回灌真实落点；夹具原件不改写）</h3>
      <table>
        <thead><tr><th>对账对</th><th>侧</th><th>对齐键</th><th>侧行实际值</th><th>更新时间</th></tr></thead>
        <tbody>
          <tr v-for="s in snapshots" :key="s.id">
            <td class="mono">{{ s.pair_code }}</td>
            <td>{{ s.side }}</td>
            <td class="mono">{{ JSON.stringify(s.align_key) }}</td>
            <td class="mono">{{ JSON.stringify(s.row) }}</td>
            <td class="mono">{{ s.updated_at }}</td>
          </tr>
          <tr v-if="!snapshots.length"><td colspan="5" class="muted">尚无侧快照；成功回灌后在此体现。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
