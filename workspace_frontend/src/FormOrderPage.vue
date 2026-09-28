<script setup>
// Halaman riwayat Form Order (FU52): form buat pindah ke halaman sendiri
// (#/form-order/baru, FormOrderCreate.vue) — tombol toolbar menavigasi ke
// sana; pesan sukses ber-nama MR ditinggalkan formCreate via
// formOrderState.justSaved dan dibaca di sini saat mount.
import { nextTick, onMounted, ref } from 'vue'
import { Inbox, Plus } from 'lucide-vue-next'
import { cancelFormOrder, formOrders, formOrderState, loadFormOrders, retryFormOrderAttachment, uiTopLoading } from './store.js'
import { foItemsText, foStatusMeta } from './form-order.js'

const savedMsg = ref('')

function goCreate() {
  window.location.hash = '#/form-order/baru'
}

// ---- batalkan (pembuat / manager; guard di server) ----
const dlgCancel = ref(null)
const cancelTarget = ref(null)
const cancelError = ref('')

function openCancel(id) {
  cancelTarget.value = formOrders.find((o) => o.id === id) || null
  cancelError.value = ''
  if (cancelTarget.value) nextTick(() => dlgCancel.value.showModal())
}
function closeCancel() {
  dlgCancel.value.close()
  cancelTarget.value = null
}
async function confirmCancel() {
  cancelError.value = ''
  try {
    await cancelFormOrder(cancelTarget.value.id)
    closeCancel()
  } catch (e) {
    cancelError.value = e.message
  }
}

async function retryAttachment() {
  const issue = formOrderState.attachmentIssue
  try {
    const uploaded = await retryFormOrderAttachment()
    savedMsg.value = `${uploaded} lampiran berhasil ditambahkan ke ${issue.materialRequest}.`
  } catch { /* pesan rinci tetap tersimpan pada attachmentIssue */ }
}

onMounted(async () => {
  if (formOrderState.justSaved) {
    savedMsg.value = formOrderState.justSaved
      ? `Form Order ${formOrderState.justSaved} terkirim.`
      : 'Form Order terkirim.'
    formOrderState.justSaved = null
  }
  uiTopLoading.active = true
  try {
    await Promise.all([loadFormOrders()])
  } finally {
    uiTopLoading.active = false
  }
})
</script>

<template>
  <div class="page-head">
    <div class="ph-left">
      <h1>Form Order</h1>
      <p class="sub">Permintaan barang dari gudang.</p>
    </div>
    <div class="ph-date">{{ new Date().toLocaleDateString('id-ID', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }) }}</div>
  </div>

  <div v-if="formOrderState.error" class="appfoot" style="color:#b3261e">Gagal memuat: {{ formOrderState.error }} — <a href="#" @click.prevent="loadFormOrders()">coba lagi</a></div>

  <div v-if="formOrderState.attachmentIssue" class="callout bad fo-attachment-warning" role="alert">
    <div>
      <strong>{{ formOrderState.attachmentIssue.materialRequest }} sudah dibuat, tetapi {{ formOrderState.attachmentIssue.files.length }} lampiran masih menunggu unggah.</strong>
      <p>{{ formOrderState.attachmentIssue.message }}</p>
      <a :href="'/app/material-request/' + encodeURIComponent(formOrderState.attachmentIssue.materialRequest)" target="_blank" rel="noopener noreferrer">Buka Material Request di ERPNext</a>
    </div>
    <button class="btn btn-sm" :disabled="!!formOrderState.pending" @click="retryAttachment">
      {{ formOrderState.pending === 'attach_form_order' ? 'Mengunggah…' : 'Ulangi Unggah Lampiran' }}
    </button>
  </div>

  <!-- FU52: tombol menavigasi ke halaman buat (bukan panel toggle) -->
  <div class="toolbar fo-actions">
    <transition name="pop" mode="out-in">
      <span v-if="savedMsg" class="why fo-saved" role="status">{{ savedMsg }}</span>
    </transition>
    <button class="btn btn-primary" @click="goCreate">
      <Plus :size="14" :stroke-width="2" />
      Buat Form Order
    </button>
  </div>

  <!-- FU51/52: riwayat memakai pola kartu Work Order (wo-body/wo-thead/wo-row,
       reuse kelas FU49) — baris tidak klikabel, aksi Batalkan di kolom akhir -->
  <div class="wo-body fo-history">
    <div class="wo-thead" v-if="formOrders.length">
      <span>Dokumen</span>
      <span>Item</span>
      <span>Dibutuhkan</span>
      <span>Dibuat oleh</span>
      <span>Status</span>
      <span>Catatan</span>
      <span></span>
    </div>

    <div v-for="o in formOrders" :key="o.id" class="wo-row fo-hist-row">
      <span class="wo-id c-doc">
        {{ o.materialRequest }}
        <small v-if="o.stockEntry" class="mono">{{ o.stockEntry }}</small>
      </span>
      <span class="wo-prod c-item">
        {{ foItemsText(o) }}
        <small>{{ o.fromWarehouse || '-' }} → {{ o.toWarehouse || '-' }}</small>
      </span>
      <span class="c-date">{{ o.scheduleDate || '-' }}</span>
      <span class="c-owner">{{ o.ownerName }}</span>
      <span class="c-status"><span class="chip" :class="foStatusMeta(o.status).cls">{{ foStatusMeta(o.status).label }}</span></span>
      <span class="c-note">{{ o.note || '-' }}</span>
      <span class="c-act">
        <button v-if="o.status === 'menunggu'" class="btn btn-sm" :disabled="!!formOrderState.pending" @click="openCancel(o.id)">Batalkan</button>
      </span>
    </div>

    <div v-if="!formOrders.length" class="empty-inset">
      <span class="eico"><Inbox :size="19" :stroke-width="1.8" /></span>
      <p class="etitle">Belum ada Form Order</p>
      <p class="ehint">Tekan "Buat Form Order" untuk menambah permintaan.</p>
    </div>
  </div>

  <dialog ref="dlgCancel" class="dialog" @click.self="closeCancel">
    <template v-if="cancelTarget">
      <h3>Batalkan Form Order?</h3>
      <p>
        {{ cancelTarget.materialRequest }} — {{ foItemsText(cancelTarget) }} akan dibatalkan
        selama belum diproses gudang.
      </p>
      <p v-if="cancelError" class="err" role="alert">{{ cancelError }}</p>
      <p v-if="formOrderState.pending === 'cancel_form_order'" role="status">Menyimpan…</p>
      <div class="dlg-actions">
        <button class="btn" :disabled="!!formOrderState.pending" @click="closeCancel">Kembali</button>
        <button class="btn btn-primary" :disabled="!!formOrderState.pending" @click="confirmCancel">Ya, Batalkan</button>
      </div>
    </template>
  </dialog>
</template>
