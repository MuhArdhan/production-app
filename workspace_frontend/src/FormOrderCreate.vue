<script setup>
// FU52: form buat Form Order jadi HALAMAN SENDIRI (#/form-order/baru) — bukan
// panel nyempil di halaman riwayat. Alur: tombol "Buat Form Order" di riwayat
// menavigasi ke sini; sukses submit menavigasi kembali + pesan sukses
// ber-nama MR disimpan di formOrderState (dibaca halaman riwayat).
// Isi form dipindah apa adanya dari FormOrderPage (FU48c: dropdown Satuan,
// default satuan terakhir user; validasi server tetap otoritatif).
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ArrowLeft, Camera, ClipboardList, Paperclip, Plus, Trash2 } from 'lucide-vue-next'
import LinkInput from './LinkInput.vue'
import { createFormOrder, foItemInfo, formOrders, formOrderState, loadFormOrders, retryFormOrderAttachment } from './store.js'
import { defaultUom, formCanSubmit, itemRowProblem, ITEM_PROBLEM_TEXT, uomOptions, uomProblem, validateFormRows } from './form-order.js'

const tomorrow = () => {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const rows = reactive([{ code: '', qty: '', info: null, infoFor: '', uom: '' }])
const scheduleDate = ref(tomorrow())
const note = ref('')
const error = ref('')
const attachments = ref([])
const attachmentInput = ref(null)
const cameraInput = ref(null)
const cameraDialog = ref(null)
const cameraVideo = ref(null)
const cameraReady = ref(false)
const cameraCapturing = ref(false)
const cameraError = ref('')
let cameraStream = null
let cameraRequest = 0
const createdRequest = ref('')
const attachmentError = ref('')

// ambil info item (satuan/nama) saat kode terpilih; ber-batch/non-stok
// mendapat peringatan dini di barisnya — validasi server tetap otoritatif.
// Info termuat → satuan baris di-reset ke default item TERPILIH (last_uom
// tervalidasi → stock_uom): ganti item tak pernah menyisakan satuan lama.
watch(rows, async () => {
  for (const row of rows) {
    if (row.code && row.infoFor !== row.code) {
      row.infoFor = row.code
      row.info = await foItemInfo(row.code)
      row.uom = defaultUom(row.info)
    } else if (!row.code && (row.info || row.infoFor)) {
      row.info = null
      row.infoFor = ''
      row.uom = ''
    }
  }
}, { deep: true })

const rowErrors = computed(() => validateFormRows(rows))
const rowProblems = computed(() => rows.map((r) => itemRowProblem(r.info) || uomProblem(r)))
const rowNotes = computed(() =>
  rows.map((row, i) =>
    [rowProblems.value[i] ? ITEM_PROBLEM_TEXT[rowProblems.value[i]] : '', rowErrors.value[i]]
      .filter(Boolean)
      .join(' · ')
  )
)
const canSubmit = computed(() =>
  formCanSubmit(rows, rowErrors.value)
  && !rowProblems.value.some(Boolean)
  && !!scheduleDate.value
  && !createdRequest.value
  && !formOrderState.pending
)

function addRow() {
  rows.push({ code: '', qty: '', info: null, infoFor: '', uom: '' })
}
function removeRow(i) {
  if (rows.length > 1) rows.splice(i, 1)
}
function goBack() {
  window.location.hash = '#/form-order'
}

function addAttachments(files) {
  const added = Array.from(files || [])
  if (!added.length) return
  attachments.value.push(...added)
  if (createdRequest.value && formOrderState.attachmentIssue) {
    formOrderState.attachmentIssue.files.push(...added)
  }
}

function chooseAttachment(event) {
  addAttachments(event.target.files)
  // Let the same file or photo be selected again after clearing it.
  event.target.value = ''
}

function isPendingAttachment(file) {
  return formOrderState.attachmentIssue?.files.includes(file)
}

function removeAttachment(index) {
  const file = attachments.value[index]
  if (createdRequest.value) {
    const issue = formOrderState.attachmentIssue
    const pendingIndex = issue?.files.indexOf(file) ?? -1
    if (pendingIndex < 0) return // Already uploaded to the Material Request.
    issue.files.splice(pendingIndex, 1)
    if (issue.files.length) {
      issue.message = 'Lampiran yang tersisa belum diunggah.'
      attachmentError.value = issue.message
    }
  }
  attachments.value.splice(index, 1)
  if (createdRequest.value && !formOrderState.attachmentIssue.files.length) {
    formOrderState.attachmentIssue = null
    finishCreated(createdRequest.value)
  }
}

function stopCamera() {
  cameraRequest += 1
  cameraStream?.getTracks().forEach((track) => track.stop())
  cameraStream = null
  if (cameraVideo.value) cameraVideo.value.srcObject = null
  cameraReady.value = false
}

function closeCamera() {
  stopCamera()
  cameraDialog.value?.close()
}

async function openCamera() {
  if (formOrderState.pending) return
  // On phones, use the same native camera picker as Frappe's attachment UI.
  if (window.matchMedia('(pointer: coarse)').matches || !navigator.mediaDevices?.getUserMedia) {
    cameraInput.value?.click()
    return
  }
  cameraError.value = ''
  cameraReady.value = false
  cameraDialog.value?.showModal()
  const request = ++cameraRequest
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false })
    if (request !== cameraRequest || !cameraDialog.value?.open) {
      stream.getTracks().forEach((track) => track.stop())
      return
    }
    cameraStream = stream
    cameraVideo.value.srcObject = stream
    await cameraVideo.value.play()
    if (request === cameraRequest) cameraReady.value = true
  } catch (error) {
    if (request !== cameraRequest) return
    stopCamera()
    cameraError.value = error?.name === 'NotAllowedError'
      ? 'Izin kamera ditolak. Izinkan akses kamera di browser, lalu coba lagi.'
      : 'Kamera tidak tersedia. Anda dapat memilih foto dari perangkat.'
  }
}

async function capturePhoto() {
  if (!cameraReady.value || cameraCapturing.value) return
  cameraCapturing.value = true
  cameraError.value = ''
  try {
    const video = cameraVideo.value
    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    const context = canvas.getContext('2d')
    if (!context || !canvas.width || !canvas.height) throw new Error('Gambar kamera belum siap.')
    context.drawImage(video, 0, 0)
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9))
    if (!blob) throw new Error('Foto gagal disimpan.')
    if (!cameraDialog.value?.open) return
    addAttachments([new File([blob], `form-order-${Date.now()}.jpg`, { type: 'image/jpeg' })])
    closeCamera()
  } catch (error) {
    cameraError.value = error.message || 'Foto gagal diambil.'
  } finally {
    cameraCapturing.value = false
  }
}

onBeforeUnmount(stopCamera)

function finishCreated(materialRequest) {
  formOrderState.justSaved = materialRequest || ''
  goBack()
}

// FU53: prefill dari MR Form Order TERAKHIR user — item sama (urutan MR),
// qty sengaja kosong. List server creation-desc + scoped per-user; satuan
// diisi watch di bawah dari last_uom → stock_uom (mekanisme FU48c) saat
// info item termuat, jadi tiap baris langsung pakai satuan transaksi
// terakhir untuk item itu.
onMounted(async () => {
  if (!formOrderState.loaded) {
    try { await loadFormOrders() } catch { /* list gagal → form kosong tetap jalan */ }
  }
  const last = formOrders.find((o) => (o.items || []).length)
  if (last) {
    rows.splice(0, rows.length, ...last.items.map((i) => (
      { code: i.code, qty: '', info: null, infoFor: '', uom: '' }
    )))
  }
})

async function submit() {
  if (!canSubmit.value) return
  error.value = ''
  try {
    const res = await createFormOrder(rows, scheduleDate.value, note.value, attachments.value)
    if (res?.attachment_error) {
      createdRequest.value = res.material_request
      attachmentError.value = res.attachment_error
      return
    }
    // sukses: daftar sudah diganti server truth — kembali ke riwayat dan
    // umumkan nama MR di sana (dibaca onMounted halaman riwayat)
    finishCreated(res?.material_request)
  } catch (e) {
    error.value = e.message // seluruh isian dipertahankan (pola FU14)
  }
}

async function retryAttachment() {
  attachmentError.value = ''
  try {
    await retryFormOrderAttachment()
    finishCreated(createdRequest.value)
  } catch (e) {
    attachmentError.value = e.message
  }
}
</script>

<template>
  <div class="page-head">
    <div class="ph-left">
      <h1>Buat Form Order</h1>
      <p class="sub">Satu form menjadi satu Material Request.</p>
    </div>
    <div class="ph-date">{{ new Date().toLocaleDateString('id-ID', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }) }}</div>
  </div>

  <div class="toolbar fo-actions">
    <button class="btn" @click="goBack">
      <ArrowLeft :size="14" :stroke-width="2" />
      Kembali ke Riwayat
    </button>
  </div>

  <section class="panel">
    <div class="panel-head">
      <span class="p-ico"><ClipboardList :size="15" :stroke-width="1.9" /></span>
      <h2>Permintaan Barang</h2>
    </div>
    <div class="panel-body">
      <!-- field induk dulu, grid item di bawahnya — urutan ala form ERPNext -->
      <div class="form-grid cols2">
        <div class="field">
          <label for="fo-date">Tanggal dibutuhkan <span class="req">*</span></label>
          <input id="fo-date" v-model="scheduleDate" class="input" type="date" />
        </div>
        <div class="field">
          <label for="fo-note">Catatan</label>
          <input id="fo-note" v-model="note" class="input" type="text" maxlength="280" placeholder="Opsional" />
        </div>
      </div>

      <div class="field fo-attachment-field">
        <label for="fo-attachment">Lampiran</label>
        <div class="fo-attachment-control">
          <label class="btn fo-attach-button" :class="{ disabled: !!formOrderState.pending }">
            <Paperclip :size="14" :stroke-width="2" />
            Attach
            <input
              id="fo-attachment"
              ref="attachmentInput"
              class="fo-attachment-native"
              type="file"
              multiple
              aria-label="Pilih lampiran Form Order"
              :disabled="!!formOrderState.pending"
              @change="chooseAttachment"
            />
          </label>
          <button type="button" class="btn" :disabled="!!formOrderState.pending" @click="openCamera">
            <Camera :size="14" :stroke-width="2" />
            Kamera
          </button>
          <input
            ref="cameraInput"
            class="fo-camera-native"
            type="file"
            accept="image/*"
            capture="environment"
            tabindex="-1"
            aria-hidden="true"
            :disabled="!!formOrderState.pending"
            @change="chooseAttachment"
          />
        </div>
        <span v-if="!attachments.length" class="hint fo-attachment-empty">Belum ada lampiran</span>
        <ul v-else class="fo-attachment-list" aria-label="Lampiran dipilih">
          <li v-for="(file, index) in attachments" :key="index" class="fo-attachment-item">
            <Paperclip :size="14" :stroke-width="2" aria-hidden="true" />
            <span class="fo-attachment-name" :title="file.name">{{ file.name }}</span>
            <span v-if="createdRequest" class="hint fo-attachment-status">
              {{ isPendingAttachment(file) ? 'Menunggu unggah' : 'Terunggah' }}
            </span>
            <button
              v-if="!createdRequest || isPendingAttachment(file)"
              type="button"
              class="btn btn-sm"
              :disabled="!!formOrderState.pending"
              :aria-label="`Hapus lampiran ${file.name}`"
              @click="removeAttachment(index)"
            >Hapus</button>
          </li>
        </ul>
        <p class="hint">Pilih beberapa file sekaligus atau tambahkan foto dari kamera. Semua lampiran akan tersimpan pada Material Request setelah Form Order dikirim.</p>
      </div>

      <div v-if="createdRequest && attachmentError" class="callout bad fo-attachment-warning" role="alert">
        <div>
          <strong>{{ createdRequest }} sudah dibuat, tetapi {{ formOrderState.attachmentIssue?.files.length }} lampiran masih menunggu unggah.</strong>
          <p>{{ attachmentError }} Anda dapat menambah atau menghapus lampiran yang belum terunggah, lalu mengulang unggahan. Form Order tidak akan dibuat ulang.</p>
          <a :href="'/app/material-request/' + encodeURIComponent(createdRequest)" target="_blank" rel="noopener noreferrer">Buka Material Request di ERPNext</a>
        </div>
        <button class="btn btn-sm" :disabled="!!formOrderState.pending" @click="retryAttachment">Ulangi Unggah Lampiran</button>
      </div>

      <!-- grid item ala child table ERPNext -->
      <div class="tbl-wrap fo-grid-wrap">
        <table class="datatable fo-grid">
          <thead>
            <tr>
              <th class="fo-c-item">Item</th>
              <th class="fo-c-qty">Qty</th>
              <th class="fo-c-uom">Satuan</th>
              <th class="fo-c-act" aria-label="Aksi baris"></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="(row, i) in rows" :key="i">
              <tr>
                <td class="fo-c-item">
                  <LinkInput :id="`fo-item-${i}`" v-model="row.code" :display-label="row.info?.item_name" doctype="Item" />
                </td>
                <td class="fo-c-qty">
                  <input
                    :id="`fo-qty-${i}`"
                    v-model="row.qty"
                    class="input fo-qty-input"
                    type="number"
                    step="any"
                    min="0"
                    inputmode="decimal"
                    :aria-invalid="!!rowNotes[i]"
                  />
                </td>
                <td class="fo-c-uom fo-uom">
                  <select
                    v-if="row.info"
                    v-model="row.uom"
                    class="input fo-uom-select"
                    :aria-label="`Satuan baris ${i + 1}`"
                  >
                    <option v-for="u in uomOptions(row.info)" :key="u" :value="u">{{ u }}</option>
                  </select>
                  <span v-else aria-hidden="true">—</span>
                </td>
                <td class="fo-c-act">
                  <button class="btn fo-del" :disabled="rows.length === 1" :aria-label="`Hapus baris ${i + 1}`" @click="removeRow(i)">
                    <Trash2 :size="14" :stroke-width="2" />
                  </button>
                </td>
              </tr>
              <tr v-if="rowNotes[i]" class="fo-grid-err">
                <td colspan="4"><p class="err" role="alert">{{ rowNotes[i] }}</p></td>
              </tr>
            </template>
            <tr class="fo-grid-addrow">
              <td colspan="4">
                <button type="button" class="btn fo-add" @click="addRow">
                  <Plus :size="14" :stroke-width="2" /> Tambah Baris
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="error" class="err" role="alert">{{ error }}</p>
      <p v-if="formOrderState.pending === 'create_form_order'" role="status">Menyimpan Form Order…</p>
      <p v-else-if="formOrderState.pending === 'attach_form_order'" role="status">Mengunggah lampiran…</p>
      <div class="panel-foot" style="padding-left: 0">
        <button class="btn" :disabled="!!formOrderState.pending" @click="goBack">Batal</button>
        <button v-if="createdRequest" class="btn btn-primary" :disabled="!!formOrderState.pending" @click="retryAttachment">Ulangi Unggah Lampiran</button>
        <button v-else class="btn btn-primary" :disabled="!canSubmit" @click="submit">Kirim Form Order</button>
      </div>
    </div>
  </section>

  <dialog ref="cameraDialog" class="dialog dialog-wide fo-camera-dialog" @cancel.prevent="closeCamera" @close="stopCamera">
    <h3>Ambil Foto Lampiran</h3>
    <div class="fo-camera-preview">
      <video ref="cameraVideo" autoplay muted playsinline aria-label="Pratinjau kamera"></video>
      <span v-if="!cameraReady && !cameraError" class="hint">Menyiapkan kamera…</span>
    </div>
    <p v-if="cameraError" class="err" role="alert">{{ cameraError }}</p>
    <div class="dlg-actions">
      <button type="button" class="btn" @click="closeCamera">Batal</button>
      <button type="button" class="btn btn-primary" :disabled="!cameraReady || cameraCapturing" @click="capturePhoto">
        <Camera :size="14" :stroke-width="2" />
        {{ cameraCapturing ? 'Menyimpan…' : 'Ambil Foto' }}
      </button>
    </div>
  </dialog>
</template>
