<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/quarantine')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

const excessCount = computed(() => rows.value.filter((q) => q.reason === '冲正超额').length)
</script>

<template>
  <div>
    <h1>未平隔离</h1>
    <p class="lead">
      冲正无原单、账户不一致或超额等未能配平的流水。超额项整笔失败，
      写明当时剩余敞口与超出金额，不动正式余额；并附当轮敞口配平证明
      （配平前/后敞口、本运配平额、该正向超额笔数），可与归并台按运行核对。
    </p>
    <p class="note-strip">按原因看：超额隔离 {{ excessCount }} 笔；共 {{ rows.length }} 笔。</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>原因</th><th>冲正金额（元）</th>
            <th>原单键</th><th>当时敞口（元）</th><th>超额（元）</th>
            <th>证明配平前敞口（元）</th><th>证明配平后敞口（元）</th>
            <th>证明本运配平额（元）</th><th>证明超额笔数</th>
            <th>说明</th><th>归并#</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="q in rows" :key="q.id">
            <td>{{ q.entry_key }}</td>
            <td>{{ q.account_code }}</td>
            <td><span class="tag" :class="q.reason === '冲正超额' ? 'bad' : 'warn'">{{ q.reason }}</span></td>
            <td>{{ yuan(q.amount_cents) }}</td>
            <td>{{ q.target_entry_key || '—' }}</td>
            <td>{{ q.reason === '冲正超额' ? yuan(q.exposure_cents) : '—' }}</td>
            <td>{{ q.reason === '冲正超额' ? yuan(q.excess_cents) : '—' }}</td>
            <td>{{ q.proof_before_remaining_cents == null ? '—' : yuan(q.proof_before_remaining_cents) }}</td>
            <td>{{ q.proof_after_remaining_cents == null ? '—' : yuan(q.proof_after_remaining_cents) }}</td>
            <td>{{ q.proof_applied_cents == null ? '—' : yuan(q.proof_applied_cents) }}</td>
            <td>
              <span v-if="q.proof_excess_quarantine_count != null"
                    class="tag" :class="q.proof_excess_quarantine_count ? 'bad' : 'on'">
                {{ q.proof_excess_quarantine_count }}
              </span>
              <span v-else>—</span>
            </td>
            <td class="reason-note">
              <template v-if="q.reason === '冲正超额'">
                冲正 {{ yuan(q.amount_cents) }} 元：超额 {{ yuan(q.excess_cents) }} 元（敞口 {{ yuan(q.exposure_cents) }} 元）。
              </template>
              <template v-else>{{ q.note || '—' }}</template>
            </td>
            <td>{{ q.settle_run_id }}</td>
            <td>{{ q.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="14">暂无隔离项（先跑归并）</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
