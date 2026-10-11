<script setup>
import { computed, nextTick, ref } from 'vue'
import { Flag } from 'lucide-vue-next'
import { completeProduction, producedQty, state } from '../store.js'
import { qtyMain } from '../format.js'

const props = defineProps({
  wo: { type: Object, required: true },
  review: Boolean,
  stageKey: String
})

const qip = props.wo
const pre = props.wo.prepacking
const post = props.wo.postpacking
// Barang Jadi = Good Qty Post-Packing; fallback Pre-Packing hanya WO completed legacy
const fg = computed(() => producedQty(props.wo))

const blockTotal = (b) => {
  const v = [b.goodQty, b.rejectQty, b.trialQty, b.sisaQty]
  return v.some((x) => x == null) ? null : v.reduce((a, c) => a + c, 0)
}
const preTotal = computed(() => blockTotal(pre))
const postTotal = computed(() => blockTotal(post))
const qtyText = (v) => (v != null ? qtyMain(v, qip) : '-')

const diff = computed(() => fg.value - props.wo.plannedStockQty)
const diffText = computed(() => {
  if (diff.value === 0) return 'Sesuai'
  const sign = diff.value > 0 ? '+' : '-'
  const a = Math.abs(diff.value)
  return `${sign}${qtyMain(a, qip)}`
})
const diffCls = computed(() => (diff.value === 0 ? '' : diff.value > 0 ? 'pos' : 'neg'))

const dlg = ref(null)

function ask() {
  nextTick(() => dlg.value.showModal())
}
function closeDlg() {
  dlg.value.close()
}
async function confirmComplete() {
  if (await completeProduction(props.wo)) dlg.value.close()
}
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <span class="p-ico"><Flag :size="15" :stroke-width="1.9" /></span>
      <h2>Finish</h2>
      <span v-if="review" class="chip chip-info" style="margin-left: auto">Tinjauan</span>
    </div>

    <div class="panel-body">
      <div class="fin-kpis">
        <div class="fin-kpi">
          <span class="fk-label">Rencana</span>
          <span class="fk-value">{{ qtyMain(wo.plannedStockQty, qip) }}</span>
        </div>
        <div class="fin-kpi">
          <span class="fk-label">Hasil</span>
          <span class="fk-value">{{ qtyMain(fg, qip) }}</span>
        </div>
        <div class="fin-kpi">
          <span class="fk-label">Selisih</span>
          <span class="fk-value" :class="diffCls">{{ diffText }}</span>
        </div>
      </div>

      <div class="fin-cols">
        <div v-if="!wo.skipPrepacking" class="fin-block">
          <div class="fin-block-head">Pre-Packing</div>
          <div class="fin-rows">
            <div class="fin-row"><span class="k">Good</span><span class="v">{{ qtyText(pre.goodQty) }}</span></div>
            <div class="fin-row"><span class="k">Reject</span><span class="v">{{ qtyText(pre.rejectQty) }}</span></div>
            <div class="fin-row"><span class="k">Trial</span><span class="v">{{ qtyText(pre.trialQty) }}</span></div>
            <div class="fin-row"><span class="k">Sisa</span><span class="v">{{ qtyText(pre.sisaQty) }}</span></div>
            <div class="fin-row total"><span class="k">Total</span><span class="v">{{ preTotal != null ? qtyMain(preTotal, qip) : '-' }}</span></div>
            <div class="fin-row"><span class="k">Jam · QC</span><span class="v dim">{{ pre.jam || '-' }} · {{ pre.qc || '-' }}</span></div>
          </div>
        </div>

        <div class="fin-block">
          <div class="fin-block-head">Post-Packing</div>
          <div class="fin-rows">
            <div class="fin-row"><span class="k">Good</span><span class="v">{{ qtyText(post.goodQty) }}</span></div>
            <div class="fin-row"><span class="k">Reject</span><span class="v">{{ qtyText(post.rejectQty) }}</span></div>
            <div class="fin-row"><span class="k">Trial</span><span class="v">{{ qtyText(post.trialQty) }}</span></div>
            <div class="fin-row"><span class="k">Sisa</span><span class="v">{{ qtyText(post.sisaQty) }}</span></div>
            <div class="fin-row total"><span class="k">Total</span><span class="v">{{ postTotal != null ? qtyMain(postTotal, qip) : '-' }}</span></div>
          </div>
        </div>
      </div>

      <div class="fin-result">
        <div class="fin-result-main">
          <span>Barang Jadi</span>
          <span class="fin-result-qty">{{ qtyMain(fg, qip) }}</span>
        </div>
        <div class="fin-result-sub">
          <span class="k">Gudang</span>
          <span class="v">{{ wo.warehouse }}</span>
        </div>
      </div>
    </div>

    <div v-if="!review" class="panel-foot">
      <!-- F09: hardening — server tetap menolak good ≤ 0; tombol dimatikan di
           klien agar tidak ada jalur klik yang sia-sia -->
      <button class="btn btn-primary" :disabled="!(fg > 0)" @click="ask">Selesaikan</button>
    </div>

    <dialog ref="dlg" class="dialog" @click.self="closeDlg">
      <h3>Selesaikan Produksi?</h3>
      <p>
        {{ wo.id }} akan diselesaikan dengan hasil
        <strong>{{ qtyMain(fg, qip) }}</strong> di
        <strong>{{ wo.warehouse }}</strong>.
      </p>
      <div class="dlg-actions">
        <button class="btn" @click="closeDlg">Batal</button>
        <button class="btn btn-primary" :disabled="!!state.pending || !(fg > 0)" @click="confirmComplete">Selesaikan</button>
      </div>
    </dialog>
  </section>
</template>
