<script setup>
import { computed, reactive } from 'vue'
import { PackageCheck } from 'lucide-vue-next'
import { confirmPostPacking, suggestionPreferences } from '../store.js'
import { qtyMain } from '../format.js'
import QtyInput from '../QtyInput.vue'

const props = defineProps({
  wo: { type: Object, required: true },
  review: Boolean,
  stageKey: String
})

const qip = props.wo
const pre = props.wo.prepacking
const src = props.wo.postpacking
// WO completed sebelum tahap ini ada: tampilkan angka pre-packing (display
// saja — tidak ada backfill), POSTPACKING_PLAN §7.
const legacyReview = props.review && props.wo.stage === 'completed' && src.goodQty == null
const display = legacyReview ? pre : src
// FU11: form baru (belum confirmed) mulai KOSONG, tetapi Good di-suggest dari
// Good Pre-Packing (bisa diubah); sisa kini INPUT MANUAL, bukan otomatis.
// Mode Tinjauan dan data yang sudah confirmed SELALU menampilkan nilai
// tersimpan (termasuk legacy mirror pra-T28 yang belum confirmed).
const start = (v) => (src.confirmed || props.review ? v : (v || null))
const form = reactive({
  goodQty: start(display.goodQty) ?? pre.goodQty ?? null,
  rejectQty: start(display.rejectQty),
  trialQty: start(display.trialQty),
  sisaQty: start(display.sisaQty),
  jam: display.jam || '',
  qc: display.qc || ((!props.review && suggestionPreferences.enabled) ? src.qcSuggested : '') || ''
})

const ok = reactive({ goodQty: false, rejectQty: false, trialQty: false, sisaQty: false })

// FU41: total hasil (good+reject+trial+sisa) tidak lagi dibatasi Good Qty
// pre-packing — batas akhirnya tetap overproduksi native saat finish.
const total = computed(() => {
  const v = [form.goodQty, form.rejectQty, form.trialQty, form.sisaQty]
  return v.some((x) => x == null) ? null : v.reduce((a, b) => a + b, 0)
})
// jam kosong = jam saat disimpan (diisi server); FU38: QC Packing opsional
const canSave = computed(() =>
  Object.values(ok).every(Boolean) && form.goodQty > 0
)

// FU12: panel dibuka ulang dari bar tahap saat sudah di finish — mode perbaikan
const reedit = computed(() => !props.review && props.wo.stage === 'finish')

async function save() {
  if (!canSave.value) return
  const saved = await confirmPostPacking(props.wo, {
    goodQty: form.goodQty,
    rejectQty: form.rejectQty ?? 0,
    trialQty: form.trialQty ?? 0,
    sisaQty: form.sisaQty ?? 0,
    jam: form.jam,
    qc: String(form.qc).trim()
  })
  if (saved) window.dispatchEvent(new CustomEvent('production:packing-saved', {
    detail: { workOrder: saved, stage: 'postpacking' }
  }))
}
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <span class="p-ico"><PackageCheck :size="15" :stroke-width="1.9" /></span>
      <h2>Post-Packing</h2>
      <span v-if="review" class="chip chip-info" style="margin-left: auto">Tinjauan</span>
      <span v-else-if="reedit" class="chip chip-info" style="margin-left: auto">Perbaikan</span>
    </div>

    <div class="panel-body">
      <p v-if="legacyReview" class="hint">
        Data lama: angka diambil dari Pre-Packing.
      </p>

      <!-- kuantitas 1 baris di desktop, bertumpuk di mobile; Sisa input manual -->
      <div class="form-grid cols4" style="margin-top: 14px">
        <QtyInput
          label="Good"
          unit-key="postGoodQty"
          :units="wo"
          required
          :disabled="review"
          v-model="form.goodQty"
          @update:valid="ok.goodQty = $event"
        />
        <QtyInput
          label="Reject"
          unit-key="postRejectQty"
          :units="wo"
          :disabled="review"
          v-model="form.rejectQty"
          @update:valid="ok.rejectQty = $event"
        />
        <QtyInput
          label="Trial"
          unit-key="postTrialQty"
          :units="wo"
          :disabled="review"
          v-model="form.trialQty"
          @update:valid="ok.trialQty = $event"
        />
        <QtyInput
          label="Sisa"
          unit-key="postSisaQty"
          :units="wo"
          :disabled="review"
          v-model="form.sisaQty"
          @update:valid="ok.sisaQty = $event"
        />
      </div>

      <div class="form-grid" style="margin-top: 14px">
        <div class="field">
          <label :for="`f-post-jam-${stageKey}`">Jam Packing</label>
          <input
            :id="`f-post-jam-${stageKey}`"
            v-model="form.jam"
            class="input"
            type="time"
            :disabled="review"
          />
        </div>

        <div class="field">
          <label :for="`f-post-qc-${stageKey}`">QC Packing</label>
          <input :id="`f-post-qc-${stageKey}`" v-model="form.qc" class="input" type="text" :disabled="review" />
        </div>
      </div>

      <div style="margin-top: 14px">
        <div class="sum-row">
          <span class="k">Total</span>
          <span class="v">{{ total != null ? qtyMain(total, qip) : '-' }}</span>
        </div>

        <div v-if="form.goodQty != null" class="fgbox">
          <span>Barang Jadi</span>
          <span>{{ qtyMain(form.goodQty, qip) }}</span>
        </div>
      </div>
    </div>

    <div v-if="!review" class="panel-foot">
      <button class="btn btn-primary" :disabled="!canSave" @click="save">{{ reedit ? 'Simpan' : 'Simpan &amp; Lanjut' }}</button>
    </div>
  </section>
</template>
