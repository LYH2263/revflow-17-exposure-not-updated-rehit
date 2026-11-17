<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)
const proofsByRun = ref({})
const expandedRun = ref(null)
const proofErr = ref('')

async function load() {
  runs.value = await getJson('/api/settle-runs')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：净额 ${(r.net_cents / 100).toFixed(2)}，隔离 ${r.quarantine_count}（超额 ${r.excess_quarantine_count}），剩余敞口 ${(r.open_cents / 100).toFixed(2)}，配平证明 ${r.proof_count} 笔`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function toggleProofs(run) {
  proofErr.value = ''
  if (expandedRun.value === run.id) {
    expandedRun.value = null
    return
  }
  expandedRun.value = run.id
  if (!proofsByRun.value[run.id]) {
    try {
      proofsByRun.value[run.id] = await getJson(`/api/settle-runs/${run.id}/exposure-proofs`)
    } catch (e) {
      proofErr.value = String(e.message || e)
      proofsByRun.value[run.id] = []
    }
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">对「待归并」正向与冲正跑一轮净额归并：先校验物化敞口，再同事务改余额/敞口/隔离/证明。超额整笔失败闭进未平隔离。</p>
    <p class="note-strip">
      本轮只处理状态为「待归并」的流水；隔离项不计入账户余额。超额不部分扣账；每个被触及正向落一行敞口配平证明，可按运行展开核对配平前/后敞口。
    </p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
    </div>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <p v-if="proofErr" class="msg">{{ proofErr }}</p>
    <div class="panel">
      <h2>归并运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>正向</th><th>冲正</th><th>已归并</th><th>隔离</th>
            <th>净额（元）</th><th>已冲累计（元）</th><th>剩余敞口合计（元）</th>
            <th>闭口正向</th><th>超额隔离</th><th>敞口不符</th><th>配平证明</th><th>提交</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="r in runs" :key="r.id">
            <tr>
              <td>{{ r.id }}</td>
              <td>{{ r.status }}</td>
              <td>{{ r.forward_count }}</td>
              <td>{{ r.reversal_count }}</td>
              <td>{{ r.settled_count }}</td>
              <td>{{ r.quarantine_count }}</td>
              <td>{{ yuan(r.net_cents) }}</td>
              <td>{{ yuan(r.reversed_total_cents) }}</td>
              <td>{{ yuan(r.open_cents) }}</td>
              <td>{{ r.closed_count }}</td>
              <td>{{ r.excess_quarantine_count }}</td>
              <td>
                <span class="tag" :class="r.exposure_mismatch_count ? 'bad' : 'on'">
                  {{ r.exposure_mismatch_count }}
                </span>
              </td>
              <td>
                <button class="btn btn-mini" @click="toggleProofs(r)">
                  {{ expandedRun === r.id ? '收起' : `证明 ${r.proof_count}` }}
                </button>
              </td>
              <td>{{ r.commit_mode }}</td>
              <td>{{ r.finished_at || '—' }}</td>
            </tr>
            <tr v-if="expandedRun === r.id" class="proof-row">
              <td colspan="15">
                <table class="proof-table">
                  <thead>
                    <tr>
                      <th>正向键</th><th>原金额（元）</th>
                      <th>配平前已冲（元）</th><th>配平前剩余敞口（元）</th>
                      <th>配平后已冲（元）</th><th>配平后剩余敞口（元）</th>
                      <th>本运配平额（元）</th><th>隔离超额笔数</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="p in proofsByRun[r.id]" :key="p.id">
                      <td>{{ p.forward_entry_key }}</td>
                      <td>{{ yuan(p.original_cents) }}</td>
                      <td>{{ yuan(p.before_reversed_cents) }}</td>
                      <td>{{ yuan(p.before_remaining_cents) }}</td>
                      <td>{{ yuan(p.after_reversed_cents) }}</td>
                      <td>
                        <strong :class="p.after_remaining_cents === 0 ? 'tag on' : ''">
                          {{ yuan(p.after_remaining_cents) }}
                        </strong>
                        <span v-if="p.after_remaining_cents === 0" class="muted-sub">已闭口</span>
                      </td>
                      <td>{{ yuan(p.applied_cents) }}</td>
                      <td>
                        <span class="tag" :class="p.excess_quarantine_count ? 'bad' : 'on'">
                          {{ p.excess_quarantine_count }}
                        </span>
                      </td>
                    </tr>
                    <tr v-if="!proofsByRun[r.id]?.length">
                      <td colspan="8">本轮无被触及正向，无配平证明。</td>
                    </tr>
                  </tbody>
                </table>
              </td>
            </tr>
          </template>
          <tr v-if="!runs.length"><td colspan="15">尚无归并运行</td></tr>
        </tbody>
      </table>
    </div>
    <p class="hint-line">{{ runs.length ? '报告口径与敞口表、配平证明、流水状态、账户余额同源；刷新不回退。' : '' }}</p>
  </div>
</template>
