<script setup>
// Papan Stock Entry — produksi-only (FU48): seluruh UI aksi gudang (buat /
// batalkan request, pilih aksi multi-role, drag & drop, tab Form Order di
// tampilan tabel) dihapus. User gudang-only dialihkan ke Desk oleh server
// (www/production_workspace.py); endpoint gudang tetap hidup, SPA hanya berhenti
// memanggilnya. Lane Cold Storage kini baca-saja (konteks stok, kartu inert).
// Produksi: klik kartu/baris request → form kirim Stock Entry (FU47, satu
// jalur server: send_handover membuat + submit SE Material Transfer via mapper
// native); baris Terkirim → detail baca-saja. Default view Tabel.
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import {
  handoverBoard, handoverLots, handoverRequests, handoverState,
  listPreferencesState, loadBoard, lotAvailablePcs, lotForWo, lotRemainingPcs, lotReservedPcs,
  normalizePageSize, PAGE_SIZE_OPTIONS, saveListPreferences, savedListPreferences, sendHandover
} from './store.js'
import { fmtInt, qtyMain, qtyStack } from './format.js'
import { handoverCard } from './handover-card.js'
import { boardMatch } from './handover-search.js'
import { distinctItems, filterSerahRows, serahFilterCount } from './handover-filter.js'
import { rowClickAction, seRouteLabel } from './handover-se.js'
import { boxAllocationText, groupBoxText } from './handover-box.js'
import { laneStatusMeta } from './form-order.js'
import {
  CheckCircle2, ChevronRight, ClipboardList, Filter, Inbox, KanbanSquare,
  Search, Snowflake, Table2, Truck
} from 'lucide-vue-next'

// FU49: tabel memakai baris kartu ala Work Order; klik baris (rowClickAction)
// tetap satu-satunya aksi — role diambil dari payload board saat dibutuhkan.

// ---- papan: 3 lane (T35), lot Cold Storage FIFO (urutan dari server) ----
const lanes = [
  { key: 'cold', title: 'Cold Storage', icon: Snowflake, empty: 'Belum ada lot' },
  { key: 'request', title: 'Request Gudang', icon: ClipboardList, empty: 'Belum ada request' },
  { key: 'kirim', title: 'Terkirim', icon: CheckCircle2, tone: 'ok', empty: 'Belum ada terkirim' }
]

// FU29: peringatan dini — stok batch/pool di GUDANG ASAL RUTE kurang dari qty
const routeWarn = (r) => (r.routeAvailable != null && r.requestedQtyPcs > r.routeAvailable)
  ? `Stok di ${r.fromWarehouse || 'gudang asal'} tinggal ${fmtInt(r.routeAvailable)} ${r.stockUom || 'Pcs'}`
  : ''

// ---- kartu: sumber tampilan (selalu turunan payload board) ----
// FU48: semua kartu inert kecuali request aktif — lot Cold Storage konteks
// stok baca-saja, kartu Terkirim riwayat.
function lotCard(lot) {
  const base = {
    kind: 'lot', ref: lot.workOrder, key: lot.workOrder,
    live: false,
    name: lot.item
  }
  if (lot.unsupported) {
    return {
      ...base,
      workOrder: lot.workOrder,
      document: '',
      batch: '',
      adonan: '',
      quantityLabel: 'Tidak didukung',
      quantity: '—',
      timestampLabel: '',
      timestamp: '',
      box: '',
      note: lot.unsupportedReason
    }
  }
  const reserved = lotReservedPcs(lot)
  return {
    ...base,
    ...handoverCard({
      workOrder: lot.workOrder,
      batch: lot.batch,
      adonan: lot.adonanKe,
      quantity: lot.producedQty,
      quantityLabel: 'Hasil WO',
      timestampLabel: 'Selesai',
      timestamp: lot.completedAt || lot.enteredAt,
      units: lot,
      box: null
    }),
    note: lot.batchless
      ? `Stok item digabung${reserved > 0 ? ` · tertahan ${fmtInt(reserved)} PCS` : ''}`
      : reserved > 0
        ? `Fisik ${fmtInt(lotRemainingPcs(lot))} PCS · tertahan ${fmtInt(reserved)} PCS`
        : ''
  }
}

// W19: grup box bersama → satu baris ringkasan grup; baris normal tetap
// chip per-box (incl. Box 3 W18). groupBoxText '' di baris non-grup.
const boxText = (r) => groupBoxText(r) || boxAllocationText(r) || undefined

function reqCard(r) {
  const lot0 = lotForWo(r.workOrder)
  const stopped = r.flag === 'stopped'
  return {
    kind: 'request', ref: r.id, key: r.id,
    live: !stopped,
    name: r.item,
    ...handoverCard({
      workOrder: r.workOrder,
      document: r.materialRequest,
      batch: r.batch,
      adonan: r.adonanKe ?? lot0?.adonanKe,
      quantity: r.requestedQtyPcs,
      quantityLabel: 'Diminta',
      timestampLabel: 'Waktu',
      timestamp: r.createdAt,
      units: lot0 || r,
      box: boxText(r)
    }),
    note: [stopped ? 'Dihentikan di Desk. Aktifkan kembali sebelum dilanjutkan.' : '', routeWarn(r)]
      .filter(Boolean).join(' · ')
  }
}

function doneCard(r) {
  const lot0 = lotForWo(r.workOrder)
  return {
    kind: 'done', ref: r.id, key: r.id,
    live: false,
    name: r.item,
    ...handoverCard({
      workOrder: r.workOrder,
      document: r.stockEntry,
      batch: r.batch,
      adonan: r.adonanKe ?? lot0?.adonanKe,
      quantity: r.requestedQtyPcs,
      quantityLabel: 'Ditransfer',
      timestampLabel: 'Dikirim',
      timestamp: r.sentAt,
      units: lot0 || r,
      box: boxText(r)
    }),
    note: ''
  }
}

// lane null + flag draft/cancelled = riwayat Desk — tidak digambar di papan;
// stopped TANPA SE tetap terlihat di Request Gudang (kartu mati).

// Filter only Cold Storage lots; work queues remain visible regardless of date.
const todayISO = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const lotFrom = ref('')
const lotTo = ref('')
const lotFilterOpen = ref(false)
const coldPageSize = ref(PAGE_SIZE_OPTIONS[0])
const coldPage = ref(1)
// FU44: search papan (nama item / WO / dokumen / batch) — client-side karena
// papan sudah termuat penuh; sengaja tidak dipersisten agar reload selalu
// menampilkan papan utuh.
const searchQ = ref('')
// FU50: filter mode tabel — status/item/tanggal dibuat; tersimpan per-user
// (pola FU18, panel + persistence meniru halaman Work Order).
const seStatus = ref('all')
const seItem = ref('all')
const seFrom = ref('')
const seTo = ref('')
const saveHandoverPreferences = () => saveListPreferences({
  workOrder: savedListPreferences.workOrder || {},
  handover: {
    from: lotFrom.value, to: lotTo.value, pageSize: coldPageSize.value, filterOpen: lotFilterOpen.value, viewMode: viewMode.value,
    seStatus: seStatus.value, seItem: seItem.value, seFrom: seFrom.value, seTo: seTo.value
  }
})
function setColdPageSize(value) {
  coldPageSize.value = normalizePageSize(value)
  coldPage.value = 1
  saveHandoverPreferences()
}
const lotFilterCount = computed(() => (lotFrom.value ? 1 : 0) + (lotTo.value ? 1 : 0))
const lotDateActive = computed(() => !!(lotFrom.value || lotTo.value))
const filteredLots = computed(() => handoverLots.filter((l) => {
  if (!lotDateActive.value) return true
  const day = (l.enteredAt || '').slice(0, 10)
  if (!day) return false
  return (!lotFrom.value || day >= lotFrom.value) && (!lotTo.value || day <= lotTo.value)
}))
const coldLots = computed(() => filteredLots.value.filter((l) => l.unsupported || lotAvailablePcs(l) > 0))
// FU44: cari pada kartu (bukan baris) SEBELUM pagination — searchQ mengubah
// jumlah kartu lane Cold Storage maupun halamannya.
const coldCards = computed(() => coldLots.value.map(lotCard).filter((c) => boardMatch(c, searchQ.value)))
const coldTotalPages = computed(() => Math.max(1, Math.ceil(coldCards.value.length / coldPageSize.value)))
const pagedColdLots = computed(() => coldCards.value.slice((coldPage.value - 1) * coldPageSize.value, coldPage.value * coldPageSize.value))
watch([lotFrom, lotTo], () => { coldPage.value = 1; saveHandoverPreferences() })
watch([seStatus, seItem, seFrom, seTo], () => saveHandoverPreferences())
watch(searchQ, () => { coldPage.value = 1 })
// panel filter tetap terbuka setelah refresh (preferensi per-user, FU18)
watch(lotFilterOpen, () => saveHandoverPreferences())
function lotToday() { lotFrom.value = todayISO(); lotTo.value = todayISO() }
function lotAllDates() { lotFrom.value = ''; lotTo.value = ''; lotFilterOpen.value = false }
function lotReset() { lotFrom.value = ''; lotTo.value = ''; lotFilterOpen.value = false }

// FU50: aksi cepat filter tabel (pola halaman WO — panel tetap terbuka)
const seFilter = () => ({ status: seStatus.value, item: seItem.value, from: seFrom.value, to: seTo.value })
const seToday = () => { seFrom.value = todayISO(); seTo.value = todayISO() }
const seAllDates = () => { seFrom.value = ''; seTo.value = '' }
const seReset = () => { seStatus.value = 'all'; seItem.value = 'all'; seFrom.value = ''; seTo.value = '' }
const itemOptions = computed(() => distinctItems(handoverRequests))
// opsi item tersimpan bisa basi (item tak lagi ada di papan) — tetap
// ditampilkan agar pilihan lama terbaca (pola fProduct halaman WO)
const itemStale = computed(() =>
  seItem.value !== 'all' && !itemOptions.value.some((o) => o.name === seItem.value))
// badge tombol Filter mengikuti mode yang sedang aktif
const activeFilterCount = computed(() =>
  viewMode.value === 'tabel' ? serahFilterCount(seFilter()) : lotFilterCount.value)

const byLane = computed(() => ({
  cold: pagedColdLots.value,
  request: handoverRequests
    .filter((r) => r.lane === 'request' && r.flag !== 'cancelled' && r.flag !== 'draft')
    .map(reqCard)
    .filter((c) => boardMatch(c, searchQ.value)),
  kirim: handoverRequests
    .filter((r) => r.lane === 'terkirim')
    .sort((a, b) => (a.sentAt || '').localeCompare(b.sentAt || ''))
    .map(doneCard)
    .filter((c) => boardMatch(c, searchQ.value))
}))

function clickCard(c) {
  if (!c.live) return
  // satu-satunya aksi kartu yang tersisa (FU48): request aktif → form kirim
  openSendDialog(c.ref)
}

// FU47: klik BARIS tabel = semantik klik kartu kanban (rowClickAction murni
// ter-test) — request tanpa flag → kirim; Terkirim → detail baca-saja.
function rowAction(r) {
  const action = rowClickAction(r, handoverBoard.roles)
  if (action === 'send') openSendDialog(r.id)
  else if (action === 'done') openSentDialog(r.id)
}

// ---- dialog (Produksi): Kirim Stock Entry — form review (FU47). Submit tetap
// SATU jalur server: send_handover membuat + submit SE Material Transfer via
// mapper native dalam satu transaksi. Panel sukses dipakai juga sebagai detail
// baca-saja untuk baris Terkirim (kirimReadonly). ----
const dlgKirim = ref(null)
const kirimReq = ref(null)
const kirimError = ref('')
const kirimDone = ref(false)
// true = dibuka dari baris Terkirim (tanpa tombol kirim, judul detail)
const kirimReadonly = ref(false)

// T32 (R6): qty kirim = qty diminta MR (== produced_qty WO); server tidak pernah stop
const kirimGood = computed(() => kirimReq.value?.requestedQtyPcs ?? 0)
const kirimRoute = computed(() => seRouteLabel(kirimReq.value, handoverBoard.targetWarehouse))
// FU29: peringatan stok rute tampil DI DALAM form review sebelum submit
const kirimRouteWarn = computed(() =>
  kirimReq.value && !kirimReadonly.value ? routeWarn(kirimReq.value) : ''
)

function openSendDialog(reqId) {
  const r = handoverRequests.find((x) => x.id === reqId)
  if (!r || r.lane !== 'request' || r.flag) return
  kirimReq.value = r
  kirimError.value = ''
  kirimDone.value = false
  kirimReadonly.value = false
  nextTick(() => dlgKirim.value.showModal())
}

// FU47: baris Terkirim → panel baca-saja (detail SE yang sudah terkirim)
function openSentDialog(reqId) {
  const r = handoverRequests.find((x) => x.id === reqId)
  if (!r || r.lane !== 'terkirim' || !r.stockEntry) return
  kirimReq.value = r
  kirimError.value = ''
  kirimDone.value = true
  kirimReadonly.value = true
  nextTick(() => dlgKirim.value.showModal())
}
function closeKirim() {
  dlgKirim.value.close()
  kirimReq.value = null
}
async function confirmKirim() {
  kirimError.value = ''
  try {
    await sendHandover(kirimReq.value.id)
    // ganti kartu dengan versi terbaru dari papan server
    const fresh = handoverRequests.find((x) => x.id === kirimReq.value.id)
    if (fresh) kirimReq.value = fresh
    kirimDone.value = true
  } catch (e) {
    kirimError.value = e.message
  }
}

// ---- FO 2026-09-18: mode tampilan Kanban|Tabel (preferensi per-user).
// FU48: default Tabel — hanya produksi yang sampai sini (antrian MR → form
// Stock Entry). Preferensi tersimpan (toggle / perubahan filter ikut
// menyimpan viewMode) selalu menang. ----
const viewMode = ref('tabel') // 'kanban' | 'tabel'
function setViewMode(mode) {
  viewMode.value = mode
  saveHandoverPreferences()
}

// baris tabel Stock Entry: seluruh request papan (status dari lane/flag),
// dicari dengan helper kartu yang sama (nama item / WO / dokumen / batch),
// lalu difilter status/item/tanggal (FU50, helper murni ter-test)
const serahRows = computed(() =>
  filterSerahRows(handoverRequests, seFilter())
    .map((r) => ({ r, meta: laneStatusMeta(r) }))
    .filter(({ r }) => boardMatch({ name: r.item, workOrder: r.workOrder, document: r.materialRequest || r.stockEntry, batch: r.batch }, searchQ.value))
)
// FU50: empty state membedakan "belum ada request" vs "terfilter habis"
const serahFilteredOut = computed(() =>
  !serahRows.value.length && (!!searchQ.value || serahFilterCount(seFilter()) > 0))
// FU49: qty bertumpuk ala baris Work Order (Pack utama · Pcs sub)
const qtyStk = (r) => qtyStack(r.requestedQtyPcs, lotForWo(r.workOrder) || r)

onMounted(() => {
  const apply = () => {
    const p = savedListPreferences.handover || {}
    lotFrom.value = p.from || ''; lotTo.value = p.to || ''
    coldPageSize.value = normalizePageSize(p.pageSize)
    lotFilterOpen.value = !!p.filterOpen
    seStatus.value = p.seStatus || 'all'; seItem.value = p.seItem || 'all'
    seFrom.value = p.seFrom || ''; seTo.value = p.seTo || ''
    // preferensi eksplisit menang; tanpa preferensi → default Tabel
    if (p.viewMode === 'tabel' || p.viewMode === 'kanban') viewMode.value = p.viewMode
    loadBoard()
  }
  if (listPreferencesState.loaded) apply()
  else {
    let applied = false
    const run = () => { if (applied) return; applied = true; apply() }
    const timer = setInterval(() => { if (listPreferencesState.loaded) { clearInterval(timer); run() } }, 25)
    setTimeout(() => { clearInterval(timer); run() }, 2000) // preferensi gagal termuat → board tetap jalan
  }
})
</script>

<template>
  <div class="page-head">
    <div class="ph-left">
      <h1>Stock Entry</h1>
      <p class="sub">Kirim barang jadi dari Cold Storage ke gudang.</p>
    </div>
    <div class="ph-date">{{ new Date().toLocaleDateString('id-ID', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }) }}</div>
  </div>

  <div v-if="handoverState.error" class="appfoot" style="color:#b3261e">Gagal memuat: {{ handoverState.error }} — <a href="#" @click.prevent="loadBoard()">coba lagi</a></div>

  <div class="toolbar">
    <div class="searchbox">
      <Search :size="15" :stroke-width="2" class="search-ico" />
      <input
        v-model="searchQ"
        class="input"
        type="search"
        placeholder="Cari nama item, WO, atau dokumen"
        aria-label="Cari item di papan Stock Entry"
      />
    </div>
    <!-- FU49/50: urutan toolbar serupa daftar WO — cari → filter → mode
         tampilan; tombol Filter tampil di KEDUA mode (FU50), isi panel
         menyesuaikan mode: tabel = status/item/tanggal dibuat, kanban =
         tanggal lot masuk (FU18). -->
    <div class="filterwrap">
      <button
        class="btn filterbtn"
        :class="{ active: activeFilterCount }"
        aria-label="Filter"
        @click="lotFilterOpen = !lotFilterOpen"
      >
        <Filter :size="14" :stroke-width="2" />
        <span class="btext">Filter</span>
        <span v-if="activeFilterCount" class="filtercount">{{ activeFilterCount }}</span>
      </button>
      <div v-if="lotFilterOpen" class="popoverlay" @click="lotFilterOpen = false"></div>
      <Transition name="pop">
        <div v-if="lotFilterOpen" class="filterpanel">
          <template v-if="viewMode === 'tabel'">
            <div class="ffield">
              <label>Status</label>
              <select v-model="seStatus" class="select">
                <option value="all">Semua status</option>
                <option value="request">Diminta</option>
                <option value="terkirim">Terkirim</option>
                <option value="cancelled">Dibatalkan</option>
                <option value="stopped">Dihentikan</option>
                <option value="draft">Draf</option>
              </select>
            </div>
            <div class="ffield">
              <label>Item</label>
              <select v-model="seItem" class="select">
                <option value="all">Semua item</option>
                <option v-if="itemStale" :value="seItem">{{ seItem }}</option>
                <option v-for="o in itemOptions" :key="o.name" :value="o.name">
                  {{ o.code ? `${o.code} · ${o.name}` : o.name }}
                </option>
              </select>
            </div>
            <div class="frow2">
              <div class="ffield">
                <label>Dibuat dari</label>
                <input v-model="seFrom" class="input" type="date" />
              </div>
              <div class="ffield">
                <label>Dibuat s.d.</label>
                <input v-model="seTo" class="input" type="date" />
              </div>
            </div>
            <div class="frow2 filter-actions">
              <button class="linkbtn" type="button" @click="seToday">Hari ini</button>
              <button class="linkbtn" type="button" @click="seAllDates">Semua tanggal</button>
            </div>
            <button class="linkbtn filter-clear" type="button" @click="seReset">Hapus semua filter</button>
          </template>
          <template v-else>
            <div class="frow2">
              <div class="ffield">
                <label>Lot masuk dari</label>
                <input v-model="lotFrom" class="input" type="date" />
              </div>
              <div class="ffield">
                <label>Lot masuk s.d.</label>
                <input v-model="lotTo" class="input" type="date" />
              </div>
            </div>
            <div class="frow2 filter-actions">
              <button class="linkbtn" type="button" @click="lotToday">Hari ini</button>
              <button class="linkbtn" type="button" @click="lotAllDates">Semua tanggal</button>
            </div>
            <button class="linkbtn filter-clear" type="button" @click="lotReset">Hapus semua filter</button>
          </template>
        </div>
      </Transition>
    </div>
    <div class="viewswitch" role="group" aria-label="Mode tampilan">
      <button
        type="button"
        :class="{ on: viewMode === 'tabel' }"
        :aria-pressed="viewMode === 'tabel'"
        @click="setViewMode('tabel')"
      >
        <Table2 :size="14" :stroke-width="2" /> Tabel
      </button>
      <button
        type="button"
        :class="{ on: viewMode === 'kanban' }"
        :aria-pressed="viewMode === 'kanban'"
        @click="setViewMode('kanban')"
      >
        <KanbanSquare :size="14" :stroke-width="2" /> Kanban
      </button>
    </div>
  </div>

  <template v-if="viewMode === 'kanban'">
  <div class="kb">
    <section
      v-for="l in lanes"
      :key="l.key"
      class="kb-lane"
      :class="l.tone ? `tone-${l.tone}` : ''"
    >
      <header class="kb-lane-head">
        <span class="kb-ico" :class="l.tone ? `ok` : ''" aria-hidden="true">
          <component :is="l.icon" :size="14" :stroke-width="1.9" />
        </span>
        <div class="kb-hgroup">
          <span class="kb-title">{{ l.title }}</span>
        </div>
        <span class="kb-count">{{ byLane[l.key].length }}</span>
      </header>
      <TransitionGroup name="kc" tag="div" class="kb-cards">
        <div
          v-for="c in byLane[l.key]"
          :key="c.key"
          class="kb-card se-kb-card"
          :class="{ live: c.live }"
          @click="clickCard(c)"
        >
          <div class="kb-top">
            <span class="kb-name">{{ c.name }}</span>
          </div>
          <div class="se-card-docs">
            <span class="kb-id">{{ c.workOrder }}</span>
            <span v-if="c.document" class="kb-id">{{ c.document }}</span>
          </div>
          <dl class="se-card-rows">
            <div v-if="c.batch" class="se-card-row se-card-batch">
              <dt>Batch</dt>
              <dd>{{ c.batch }}</dd>
            </div>
            <div v-if="c.adonan" class="se-card-row">
              <dt>Adonan</dt>
              <dd>{{ c.adonan }}</dd>
            </div>
            <div class="se-card-row se-card-qty">
              <dt>{{ c.quantityLabel }}</dt>
              <dd>{{ c.quantity }}</dd>
            </div>
            <div v-if="c.timestampLabel && c.timestamp" class="se-card-row">
              <dt>{{ c.timestampLabel }}</dt>
              <dd>{{ c.timestamp }}</dd>
            </div>
          </dl>
          <span v-if="c.note" class="kb-note">{{ c.note }}</span>
          <div v-if="c.box" class="se-card-foot">
            <span class="kb-pill">{{ c.box }}</span>
          </div>
        </div>
        <div v-if="!byLane[l.key].length" key="empty" class="kb-empty">
          <Inbox :size="16" :stroke-width="1.8" aria-hidden="true" />
          <span>{{ l.empty }}</span>
        </div>
      </TransitionGroup>
    </section>
  </div>

  <div class="pagination-bar pagination-footer cold-pagination">
    <label class="page-size-control">
      <span>Tampilkan</span>
      <select class="select" :value="coldPageSize" aria-label="Jumlah lot Cold Storage per halaman" @change="setColdPageSize($event.target.value)">
        <option v-for="size in PAGE_SIZE_OPTIONS" :key="size" :value="size">{{ size }}</option>
      </select>
    </label>
  </div>
  </template>

  <!-- ============ FU49: tampilan Tabel — baris kartu ala daftar Work Order
       (wo-body/wo-row reuse; klik baris = semantik FU47: kirim / detail) ============ -->
  <template v-else>
    <div class="wo-body se-queue">
      <div class="wo-thead" v-if="serahRows.length">
        <span>Dokumen</span>
        <span>Item</span>
        <span style="text-align: right">Qty</span>
        <span>Work Order</span>
        <span>Batch</span>
        <span>Dibuat oleh</span>
        <span>Status</span>
        <span></span>
      </div>

      <div
        v-for="{ r, meta } in serahRows"
        :key="r.id"
        class="wo-row se-queue-row"
        :class="{ rowlink: !!rowClickAction(r, handoverBoard.roles) }"
        :role="rowClickAction(r, handoverBoard.roles) ? 'button' : undefined"
        :tabindex="rowClickAction(r, handoverBoard.roles) ? 0 : undefined"
        @click="rowAction(r)"
        @keydown.enter="rowAction(r)"
      >
        <span class="wo-id c-doc">
          {{ r.materialRequest }}
          <small v-if="r.stockEntry" class="mono">{{ r.stockEntry }}</small>
        </span>
        <span class="wo-prod c-item">{{ r.item }} <small class="mono">{{ r.itemCode }}</small></span>
        <span class="wo-qty c-qty">
          <span class="qmain">{{ qtyStk(r).main }}</span>
          <span v-if="qtyStk(r).sub" class="qsub">{{ qtyStk(r).sub }}</span>
        </span>
        <span class="c-wo">{{ r.workOrder }}</span>
        <span class="c-batch">{{ r.batch || '-' }}</span>
        <span class="c-owner">{{ r.ownerName || '-' }}</span>
        <span class="c-status"><span class="chip" :class="meta.cls">{{ meta.label }}</span></span>
        <span v-if="rowClickAction(r, handoverBoard.roles)" class="c-arrow"><ChevronRight :size="16" :stroke-width="2" /></span>
        <span v-else class="c-arrow"></span>
      </div>

      <div v-if="!serahRows.length" class="empty-inset">
        <span class="eico"><Inbox :size="19" :stroke-width="1.8" /></span>
        <p class="etitle">{{ serahFilteredOut ? 'Tidak ada request' : 'Belum ada request' }}</p>
        <p class="ehint">{{ serahFilteredOut ? 'Tidak ada request untuk filter saat ini. Ubah filter atau kata pencarian.' : 'Request gudang dari Desk akan muncul di sini untuk dikirim.' }}</p>
      </div>
    </div>
  </template>

  <!-- ============ dialog: Kirim Stock Entry — form review
       (Produksi, dari request; FU47 juga detail baca-saja baris Terkirim) ============ -->
  <dialog ref="dlgKirim" class="dialog" @click.self="closeKirim">
    <template v-if="kirimReq">
      <template v-if="!kirimDone">
        <header class="dlg-head">
          <span class="dlg-ico" aria-hidden="true"><Truck :size="16" :stroke-width="1.9" /></span>
          <div class="dlg-hgroup">
            <h3>Kirim Stock Entry</h3>
            <p class="dlg-sub"><strong>{{ kirimReq.workOrder }}</strong> · {{ kirimReq.item }}</p>
          </div>
        </header>
        <div class="dlg-context">
          <div class="sum-row">
            <span class="k">Material Request</span>
            <span class="v">{{ kirimReq.materialRequest }}</span>
          </div>
          <div class="sum-row">
            <span class="k">Item</span>
            <span class="v">
              {{ kirimReq.item }}
              <span v-if="kirimReq.itemCode" class="tbl-sub">{{ kirimReq.itemCode }}</span>
            </span>
          </div>
          <div class="sum-row">
            <span class="k">Rute</span>
            <span class="v"><span class="dim">{{ kirimRoute }}</span></span>
          </div>
          <div class="sum-row">
            <span class="k">Batch</span>
            <span class="v">{{ kirimReq.batch || '-' }}</span>
          </div>
          <div class="sum-row">
            <span class="k">Box</span>
            <span class="v" style="white-space: pre-line">{{ boxText(kirimReq) || '-' }}</span>
          </div>
        </div>
        <div class="boxgroup" style="margin-top: 14px">
          <div class="grouptitle">Stock Entry — Material Transfer</div>
          <div class="fgbox">
            <span>Qty Transfer</span>
            <span>{{ qtyMain(kirimGood, lotForWo(kirimReq.workOrder) || kirimReq) }}</span>
          </div>
          <p class="hint" style="margin-top: 6px">
            Qty terkunci sesuai permintaan (hasil Work Order saat request dibuat). Tanggal catat otomatis
            (sekarang). Stock Entry dibuat dan disubmit server saat dikirim — kekurangan stok ditolak apa adanya.
          </p>
          <p v-if="kirimRouteWarn" class="callout" role="alert">{{ kirimRouteWarn }}</p>
        </div>
        <p v-if="kirimError" class="err" style="margin-top: 10px" role="alert">{{ kirimError }}</p>
        <p v-if="handoverState.pending" role="status">Menyimpan…</p>
        <div class="dlg-actions">
          <button class="btn" :disabled="!!handoverState.pending" @click="closeKirim">Batal</button>
          <button class="btn btn-primary" :disabled="!!handoverState.pending" @click="confirmKirim">Buat &amp; Kirim Stock Entry</button>
        </div>
      </template>
      <template v-else>
        <header class="dlg-head">
          <span class="dlg-ico ok" aria-hidden="true"><CheckCircle2 :size="16" :stroke-width="1.9" /></span>
          <div class="dlg-hgroup">
            <h3>{{ kirimReadonly ? 'Detail Stock Entry' : 'Terkirim' }}</h3>
            <p class="dlg-sub"><strong>{{ kirimReq.workOrder }}</strong> · {{ kirimReq.item }}</p>
          </div>
        </header>
        <div class="fgbox">
          <span>Ditransfer</span>
          <span>{{ qtyMain(kirimGood, lotForWo(kirimReq.workOrder) || kirimReq) }}</span>
        </div>
        <div class="dlg-context" style="margin-top: 8px">
          <div class="sum-row">
            <span class="k">Stock Entry</span>
            <span class="v">{{ kirimReq.stockEntry }}</span>
          </div>
          <div class="sum-row">
            <span class="k">Material Request</span>
            <span class="v">{{ kirimReq.materialRequest }}</span>
          </div>
          <div class="sum-row">
            <span class="k">Rute</span>
            <span class="v"><span class="dim">{{ kirimRoute }}</span></span>
          </div>
          <div class="sum-row">
            <span class="k">Batch</span>
            <span class="v">{{ kirimReq.batch || '-' }}</span>
          </div>
          <div v-if="kirimReq.sentAt" class="sum-row">
            <span class="k">Dikirim</span>
            <span class="v">{{ kirimReq.sentAt }}</span>
          </div>
        </div>
        <div class="dlg-actions">
          <button class="btn" @click="closeKirim">Tutup</button>
        </div>
      </template>
    </template>
  </dialog>
</template>
