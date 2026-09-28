<script setup>
import { computed, reactive } from 'vue'
import { PackageCheck } from 'lucide-vue-next'
import { savePrePacking, suggestionPreferences } from '../store.js'
import { qtyMain } from '../format.js'
import QtyInput from '../QtyInput.vue'
import { labelCountForWorkOrder, offerLabelPrint } from '../label-print-prompt.js'

const props = defineProps({
  wo: { type: Object, required: true },
  review: Boolean,
  stageKey: String
})

const qip = props.wo

const src = props.wo.prepacking
// FU11: form baru (belum confirmed) mulai KOSONG — angka 0 default DB tidak
// ditampilkan agar operator mengisi hasil nyata. Mode Tinjauan dan data yang
// sudah confirmed SELALU menampilkan nilai tersimpan (termasuk legacy).
const start = (v) => (src.confirmed || props.review ? v : (v || null))
const form = reactive({
  goodQty: start(src.goodQty),
  rejectQty: start(src.rejectQty),
  trialQty: start(src.trialQty),
  sisaQty: start(src.sisaQty),
  jam: src.jam || '',
  qc: src.qc || ((!props.review && suggestionPreferences.enabled) ? props.wo.persiapan.qcProduksiSuggested : '') || ''
})

const ok = reactive({ goodQty: false, rejectQty: false, trialQty: false, sisaQty: false })

const qtyFields = computed(() => [
  { key: 'goodQty', label: 'Good Qty', required: true },
  { key: 'rejectQty', label: 'Reject Qty', required: false },
  { key: 'trialQty', label: 'Trial Qty', required: false },
  { key: 'sisaQty', label: 'Sisa Qty', required: false }
])

const qtyValid = computed(() => ok.goodQty)
// jam kosong = jam saat disimpan (diisi server); FU38: QC Produksi opsional
const canSave = computed(() => qtyValid.value && form.goodQty > 0)

const total = computed(() => {
  const v = [form.goodQty, form.rejectQty, form.trialQty, form.sisaQty]
  if (v.some((x) => x == null)) return null
  return v.reduce((a, b) => a + b, 0)
})
const over = computed(() => total.value != null && total.value > props.wo.plannedStockQty)

// FU12: panel dibuka ulang dari bar tahap setelah tahap ini lewat — mode perbaikan
const reedit = computed(() => !props.review && ['postpacking', 'finish'].includes(props.wo.stage))

async function save() {
  if (!canSave.value) return
  const saved = await savePrePacking(props.wo, {
    goodQty: form.goodQty,
    rejectQty: form.rejectQty ?? 0,
    trialQty: form.trialQty ?? 0,
    sisaQty: form.sisaQty ?? 0,
    jam: form.jam,
    qc: String(form.qc).trim()
  })
  if (saved) offerLabelPrint(saved.id, labelCountForWorkOrder(saved), true)
}
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <span class="p-ico"><PackageCheck :size="15" :stroke-width="1.9" /></span>
      <h2>Pre-Packing</h2>
      <span class="lead">Hasil awal packing.</span>
      <span v-if="review" class="chip chip-info" style="margin-left: auto">Tinjauan</span>
      <span v-else-if="reedit" class="chip chip-info" style="margin-left: auto">Perbaikan</span>
    </div>

    <div class="panel-body">
      <div class="callout ok" style="margin-bottom: 14px">
        Hasil akhir dicatat di Post-Packing dan menjadi jumlah Barang Jadi.
      </div>

      <!-- kuantitas 1 baris di desktop, bertumpuk di mobile -->
      <div class="form-grid cols4">
        <QtyInput
          v-for="f in qtyFields"
          :key="f.key"
          :label="f.label"
          :unit-key="f.key"
          :units="wo"
          :required="f.required"
          :disabled="review"
          v-model="form[f.key]"
          @update:valid="ok[f.key] = $event"
        />
      </div>

      <div class="form-grid" style="margin-top: 14px">
        <div class="field">
          <label :for="`f-jam-${stageKey}`">Jam Pembekuan</label>
          <input
            :id="`f-jam-${stageKey}`"
            v-model="form.jam"
            class="input"
            type="time"
            :disabled="review"
          />
        </div>

        <div class="field">
          <label :for="`f-qc-${stageKey}`">QC Produksi</label>
          <input :id="`f-qc-${stageKey}`" v-model="form.qc" class="input" type="text" :disabled="review" />
        </div>
      </div>

      <div style="margin-top: 14px">
        <div v-if="total != null" class="sum-row">
          <span class="k">Total hasil (Good + Reject + Trial + Sisa)</span>
          <span class="v" :class="{ neg: over }">{{ qtyMain(total, qip) }}</span>
        </div>
        <div class="sum-row">
          <span class="k">Rencana</span>
          <span class="v">{{ qtyMain(wo.plannedStockQty, qip) }}</span>
        </div>
        <p v-if="over" class="hint warn">Total melebihi rencana.</p>

        <div v-if="form.goodQty != null" class="fgbox">
          <span>Barang Jadi</span>
          <span>{{ qtyMain(form.goodQty, qip) }}</span>
        </div>
      </div>
    </div>

    <div v-if="!review" class="panel-foot">
      <button class="btn btn-primary" :disabled="!canSave" @click="save">{{ reedit ? 'Simpan Perbaikan' : 'Simpan &amp; Lanjut ke Post-Packing' }}</button>
    </div>
  </section>
</template>
