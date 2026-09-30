<script setup>
import { onMounted, reactive, ref } from 'vue'
import { Warehouse } from 'lucide-vue-next'
import { call, loadSuggestionPreferences, saveSuggestionPreferences, suggestionPreferences, uiState, uiTopLoading, saveUiPreferences } from './store.js'
import LinkInput from './LinkInput.vue'

// FU40: label konsisten Indonesia (istilah ERPNext dipertahankan di keterangan),
// grid dipecah dua blok: default Work Order vs default serah terima.
const woFields = [
  { key: 'source_warehouse', label: 'Gudang Sumber', desc: 'Source Warehouse — lokasi bahan baku tersedia.' },
  { key: 'wip_warehouse', label: 'Gudang Work in Progress', desc: 'WIP Warehouse — lokasi operasi produksi dijalankan.' },
  { key: 'fg_warehouse', label: 'Gudang Target', desc: 'Target Warehouse — lokasi barang jadi disimpan.' },
  { key: 'scrap_warehouse', label: 'Gudang Scrap', desc: 'Scrap Warehouse — lokasi material scrap disimpan.' }
]
const handoverFields = [
  { key: 'handover_warehouse', label: 'Gudang Tujuan Stock Entry', desc: 'Tujuan pengiriman barang jadi (halaman Stock Entry).' },
  { key: 'handover_source_warehouse', label: 'Gudang Asal Stock Entry', desc: 'Asal pengiriman — biasanya Cold Storage (halaman Stock Entry).' }
]
// FO 2026-09-18: rute default Form Order (produksi minta barang dari gudang).
const formOrderFields = [
  { key: 'form_order_source_warehouse', label: 'Gudang Asal Form Order', desc: 'Gudang yang diminta produksi (mis. Gudang Bahan Baku).' },
  { key: 'form_order_target_warehouse', label: 'Gudang Tujuan Form Order', desc: 'Tujuan pemindahan stok saat gudang memproses (mis. WIP).' }
]

// FU61: 8 kunci flat kontrak API — payload simpan dikirim eksplisit dari
// daftar ini dan hanya kunci ini yang disalin balik dari response, agar kunci
// propagated/skipped/failed tidak pernah terkirim balik / menempel di form.
const FLAT_KEYS = [
  'source_warehouse',
  'wip_warehouse',
  'fg_warehouse',
  'scrap_warehouse',
  'handover_warehouse',
  'handover_source_warehouse',
  'form_order_source_warehouse',
  'form_order_target_warehouse'
]

const form = reactive({
  source_warehouse: '',
  wip_warehouse: '',
  fg_warehouse: '',
  scrap_warehouse: '',
  handover_warehouse: '',
  handover_source_warehouse: '',
  form_order_source_warehouse: '',
  form_order_target_warehouse: ''
})
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const savedAt = ref('')
const savedInfo = ref('')
const saveFailed = ref([])
const suggestionSaving = ref(false)

onMounted(async () => {
  uiTopLoading.active = true
  try {
    await Promise.all([
      call('production_app.api.work_order.warehouse_defaults').then(values => Object.assign(form, values)),
      loadSuggestionPreferences()
    ])
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
    uiTopLoading.active = false
  }
})

async function toggleSuggestions() {
  suggestionSaving.value = true
  error.value = ''
  try {
    await saveSuggestionPreferences(suggestionPreferences.enabled)
  } catch (e) {
    suggestionPreferences.enabled = !suggestionPreferences.enabled
    error.value = e.message
  } finally {
    suggestionSaving.value = false
  }
}

// FU70: ukuran font per-user — tersimpan saat digeser, berlaku langsung
const fontScale = ref(uiState.fontScale)
const fontSaving = ref(false)
async function onFontScaleChange() {
  fontSaving.value = true
  error.value = ''
  try {
    fontScale.value = await saveUiPreferences(fontScale.value)
  } catch (e) {
    error.value = e.message
    fontScale.value = uiState.fontScale // kembalikan ke nilai tersimpan
  } finally {
    fontSaving.value = false
  }
}

async function save() {
  saving.value = true
  error.value = ''
  savedAt.value = ''
  savedInfo.value = ''
  saveFailed.value = []
  try {
    // FU58 §9.4: payload eksplisit 9 field (bukan {...form}) — lihat FLAT_KEYS.
    const payload = {}
    for (const key of FLAT_KEYS) payload[key] = form[key]
    const saved = await call('production_app.api.work_order.warehouse_defaults_save', payload)
    for (const key of FLAT_KEYS) if (key in saved) form[key] = saved[key] ?? ''
    savedAt.value = new Date().toLocaleTimeString('id-ID')
    const propagated = saved.propagated || []
    if (propagated.length) {
      const names = propagated.map(p => p.name).slice(0, 5).join(', ')
      savedInfo.value = propagated.length > 5
        ? `${propagated.length} Work Order berjalan diperbarui (${names} …)`
        : `${propagated.length} Work Order berjalan diperbarui: ${names}`
    }
    // Kegagalan per-WO tidak menggagalkan tersimpannya pengaturan (§4.5).
    saveFailed.value = (saved.failed || []).map(f => `${f.name}: ${f.error}`)
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <span class="p-ico"><Warehouse :size="15" :stroke-width="1.9" /></span>
      <h2>Pengaturan Gudang</h2>
      <span class="lead">Gudang default untuk Work Order dan Stock Entry di workspace ini. Perubahan nilai default otomatis diterapkan ke Work Order yang sedang berjalan selama nilainya belum diubah manual.</span>
    </div>

    <div class="panel-body">
      <!-- FU20: loading ditandai spinner global di tepi atas (App.vue) -->
      <div v-if="loading" aria-hidden="true"></div>
      <template v-else>
        <div class="settings-block">
          <div class="settings-block-head">
            <div>
              <h3>Tampilan</h3>
              <p class="hint">Ukuran font untuk akun ini saja — tersimpan per pengguna, tidak memengaruhi pengguna lain.</p>
            </div>
          </div>
          <div class="field" style="max-width: 380px; margin-top: 4px">
            <label for="ui-font-scale">Ukuran font: <strong>{{ fontScale }}%</strong></label>
            <input id="ui-font-scale" v-model.number="fontScale" type="range" min="90" max="125" step="5" :disabled="fontSaving" @change="onFontScaleChange" />
            <div class="hint">Rentang dibatasi 90–125% agar layout tetap rapi.</div>
          </div>
        </div>

        <div class="settings-block">
          <div class="settings-block-head">
            <div>
              <h3>Saran isian berulang</h3>
              <p class="hint">Gunakan nilai dari Work Order sebelumnya dengan produk yang sama untuk nama tim, jumlah kru, dan QC.</p>
            </div>
            <label class="toggle-control">
              <input v-model="suggestionPreferences.enabled" type="checkbox" :disabled="suggestionSaving" @change="toggleSuggestions" />
              <span>{{ suggestionPreferences.enabled ? 'Aktif' : 'Nonaktif' }}</span>
            </label>
          </div>
        </div>

        <div class="settings-block">
          <div class="settings-block-head">
            <div>
              <h3>Default Work Order</h3>
            </div>
          </div>
          <div class="callout ok" style="margin-bottom: 14px">
            Dipakai Work Order yang kolom gudangnya masih kosong saat Persiapan disimpan.
            Ubah nilai langsung ke nilai baru (jangan kosongkan dulu lalu isi belakangan):
            perubahan langsung otomatis diterapkan ke Work Order yang sedang berjalan —
            header dan baris bahan — selama nilainya masih sama dengan nilai lama.
            Nilai yang sudah diubah manual di Work Order tidak ditimpa; kasus lain
            (termasuk WO yang gagal diperbarui) perlu sinkronisasi oleh admin.
          </div>
          <div class="form-grid cols2">
            <div v-for="f in woFields" :key="f.key" class="field">
              <label :for="`wh-${f.key}`">{{ f.label }}</label>
              <LinkInput :id="`wh-${f.key}`" v-model="form[f.key]" doctype="Warehouse" />
              <div class="hint">{{ f.desc }}</div>
            </div>
          </div>
        </div>

        <div class="settings-block">
          <div class="settings-block-head">
            <div>
              <h3>Stock Entry (Kirim ke Gudang)</h3>
              <p class="hint">Asal dan tujuan default pengiriman di papan Stock Entry.</p>
            </div>
          </div>
          <div class="form-grid cols2" style="margin-top: 12px">
            <div v-for="f in handoverFields" :key="f.key" class="field">
              <label :for="`wh-${f.key}`">{{ f.label }}</label>
              <LinkInput :id="`wh-${f.key}`" v-model="form[f.key]" doctype="Warehouse" />
              <div class="hint">{{ f.desc }}</div>
            </div>
          </div>
        </div>

        <div class="settings-block">
          <div class="settings-block-head">
            <div>
              <h3>Form Order</h3>
              <p class="hint">Rute default permintaan produksi ke gudang — wajib diisi sebelum Form Order bisa dibuat.</p>
            </div>
          </div>
          <div class="form-grid cols2" style="margin-top: 12px">
            <div v-for="f in formOrderFields" :key="f.key" class="field">
              <label :for="`wh-${f.key}`">{{ f.label }}</label>
              <LinkInput :id="`wh-${f.key}`" v-model="form[f.key]" doctype="Warehouse" />
              <div class="hint">{{ f.desc }}</div>
            </div>
          </div>
        </div>

        <p v-if="error" class="err" role="alert">{{ error }}</p>
        <!-- FU58 §9.4: kegagalan per-WO saat propagasi — pengaturan tetap tersimpan. -->
        <div v-if="saveFailed.length" class="err" role="alert">
          <div>Pengaturan tersimpan, tetapi Work Order berikut gagal diperbarui:</div>
          <div v-for="item in saveFailed" :key="item">{{ item }}</div>
        </div>

        <div class="panel-foot" style="padding-left: 0">
          <button class="btn btn-primary" :disabled="saving" @click="save">
            {{ saving ? 'Menyimpan…' : 'Simpan Pengaturan' }}
          </button>
          <span v-if="savedAt" class="why">Tersimpan pada {{ savedAt }}<template v-if="savedInfo"> — {{ savedInfo }}</template>.</span>
        </div>
      </template>
    </div>
  </section>
</template>
