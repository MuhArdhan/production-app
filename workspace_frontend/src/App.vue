<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import WorkOrderList from './WorkOrderList.vue'
import Workspace from './Workspace.vue'
import WarehouseSettings from './WarehouseSettings.vue'
import HandoverBoard from './HandoverBoard.vue'
import FormOrderPage from './FormOrderPage.vue'
import FormOrderCreate from './FormOrderCreate.vue'
import Dashboard from './Dashboard.vue'
import MaterialUsagePage from './MaterialUsagePage.vue'
import StockAvailabilityPage from './StockAvailabilityPage.vue'
import LainnyaPage from './LainnyaPage.vue'
import { labelPrintPrompt } from './label-print-prompt.js'
import { printLabels } from './qz-tray.js'
import { workOrders, listState, loadList, loadListPreferences, loadSuggestionPreferences, loadUiPreferences, loadCompanyFilter, state, uiTopLoading, handoverBoard, handoverRequests, handoverState, loadBoard, formOrderState, dashboardState } from './store.js'
import { activeTotal } from './dashboard.js'
import { ClipboardCheck, ClipboardList, ChevronDown, LayoutGrid, MoreHorizontal, PackageSearch, RotateCw, Settings, HelpCircle, Factory, Package, Wheat } from 'lucide-vue-next'

// router hash minimal: '#/' (dashboard, FU72), '#/wo' (daftar), '#/wo/<id>'
// (work order), '#/handover' (stock entry), '#/penggunaan-bahan' (FU74; FU82
// rename dari '#/bahan')
// FU82: alias rute lama → kanonik. Prefix-match ketat ('#/bahan' diikuti akhir
// ATAU '?') agar '#/penggunaan-bahan' tidak ikut tertukar. Normalisasi WAJIB
// di dua titik (init ref + onHash) supaya cold-load deep-link lama tidak
// sempat merender Dashboard; location.replace = same-document, tanpa entri
// history, tanpa loop (hashchange berikutnya sudah kanonik).
const normalisasiHash = (h) =>
  /^#\/bahan(\?|$)/.test(h) ? '#/penggunaan-bahan' + h.slice('#/bahan'.length) : h
const hash = ref(normalisasiHash(window.location.hash))
if (hash.value !== window.location.hash) window.location.replace(hash.value)
const onHash = () => {
  const kanonik = normalisasiHash(window.location.hash)
  if (kanonik !== window.location.hash) {
    window.location.replace(kanonik) // hashchange berikutnya membawa hash kanonik
    return
  }
  hash.value = kanonik
  window.scrollTo(0, 0)
}
onMounted(() => {
  window.addEventListener('hashchange', onHash)
  history.scrollRestoration = 'manual'
  window.scrollTo(0, 0)
  // browser memulihkan posisi scroll reload setelah load; paksa kembali ke atas
  setTimeout(() => window.scrollTo(0, 0), 0)
})
onUnmounted(() => window.removeEventListener('hashchange', onHash))

const woId = computed(() =>
  hash.value.startsWith('#/wo/') ? decodeURIComponent(hash.value.slice(5)) : null
)
// FU72: '#/wo' menutup '#/wo/' juga; default (hash kosong/'#/') = dashboard
const section = computed(() =>
  hash.value.startsWith('#/wo') ? 'workorder'
    : hash.value.startsWith('#/settings') ? 'settings'
      : hash.value.startsWith('#/handover') ? 'handover'
        : hash.value.startsWith('#/form-order') ? 'form-order'
          : hash.value.startsWith('#/penggunaan-bahan') ? 'material-usage'
            : hash.value.startsWith('#/ketersediaan-stock') ? 'stock-availability'
              : hash.value.startsWith('#/lainnya') ? 'lainnya'
                : 'dashboard'
)
// FU74: '#/penggunaan-bahan?bahan=..&dari=..&sampai=..&company=..' → props awal
// halaman (FU82: rename rute dari '#/bahan'; param ?bahan= tetap — kosakata materi)
const usageQuery = computed(() => {
  if (!hash.value.startsWith('#/penggunaan-bahan')) return { bahan: '', dari: '', sampai: '', company: '' }
  const qs = hash.value.slice('#/penggunaan-bahan'.length)
  const params = new URLSearchParams(qs.startsWith('?') ? qs.slice(1) : '')
  return {
    bahan: params.get('bahan') || '',
    dari: params.get('dari') || '',
    sampai: params.get('sampai') || '',
    company: params.get('company') || ''
  }
})
// FU52: halaman buat Form Order terpisah dari riwayat
const foCreate = computed(() => hash.value === '#/form-order/baru')

// peran dari papan server (bukan simulasi) — untuk Pengaturan. FU48: user
// gudang-only tidak pernah sampai sini lagi (dialihkan ke /app oleh server);
// item menu Work Orders & Stock Entry SELALU tampil (permintaan user
// 2026-09-14): visibilitas menu tidak boleh bergantung pada flag asinkron
// (dulu menyebabkan menu "kedip/hilang" sebelum/saat gagal load board); isi
// halaman tetap difilter izin di server.
// FU64: kombinasi role dinilai sebagai union — manager + stock BUKAN
// gudang-only (dulu menu Pengaturan tersembunyi untuk kombinasi itu), dan
// Pengaturan mengikuti kapabilitas server `can_settings` (write
// Manufacturing Settings — native: Manufacturing Manager), bukan tebakan
// nama role di UI.
const isGudangOnly = computed(
  () => !!handoverBoard.roles.is_gudang &&
    !(handoverBoard.roles.is_produksi || handoverBoard.roles.is_manajer_produksi)
)
const canSettings = computed(() => !!handoverBoard.roles.can_settings)
// FO 2026-09-18: menu Form Order hanya produksi/manager. Default TERSEMBUNYI
// sampai role termuat (flag server `is_manajer_produksi`): pihak yang TIDAK
// berhak tidak pernah melihatnya; produksi melihatnya setelah papan termuat
// (pola serupa badge yang juga async). Guard otoritatif tetap di server.
const canFormOrder = computed(() =>
  !!(handoverBoard.roles.is_produksi || handoverBoard.roles.is_manajer_produksi)
)

// drawer ala YouTube: tutup default, burger di top bar membuka/menutup
const navOpen = ref(false)
// menu profil (2026-09-21): shortcut pulang ke Desk native (/app)
const userMenu = ref(false)
const errorDialog = ref(null)
const labelPrintDialog = ref(null)
const extraLabelCount = ref(0)
const labelCountValid = computed(() =>
  extraLabelCount.value !== '' &&
  Number.isInteger(Number(extraLabelCount.value)) &&
  Number(extraLabelCount.value) >= 0 &&
  labelPrintPrompt.labelCount + Number(extraLabelCount.value) <= 10000
)
const totalLabelCount = computed(() => labelPrintPrompt.labelCount + Number(extraLabelCount.value || 0))
const errorClose = ref(null)
const currentUser = window.workspace_user || 'Pengguna ERPNext'
const initials = currentUser.split(' ').slice(0, 2).map(s => s[0]).join('')
// FU72: landing Dashboard tidak memuat daftar WO — badge pakai ringkasan tahap
// dari dashboard_summary; setelah daftar dimuat, hitungan daftar yang menang.
// FU112 (temuan E2E): sejak pagination (FU94) "daftar" hanyalah SATU halaman —
// menghitung darinya bisa menampilkan 0 padahal ada puluhan WO aktif (8081:
// halaman-1 kebetulan semua Completed → badge hilang). Aturan baru:
// (1) daftar LENGKAP termuat (total == baris) → hitung dari daftar (bisa
//     ter-filter, perilaku lama); (2) selain itu ringkasan tahap LIVE dari
//     server (dashboard_summary) menang; (3) ringkasan belum ada (buka
//     langsung #/wo) → perkiraan dari baris yang termuat (perilaku lama).
const activeCount = computed(() => {
  if (state.loaded && listState.total > 0 && workOrders.length >= listState.total) {
    return workOrders.filter((w) => w.stage !== 'completed').length
  }
  const stages = dashboardState.summary?.stages
  if (stages) return activeTotal(stages)
  return state.loaded ? workOrders.filter((w) => w.stage !== 'completed').length : 0
})
const handoverCount = computed(() =>
  handoverRequests.filter((r) => r.lane === 'request').length
)
// FU20: spinner global di tepi atas untuk semua pemuatan halaman
const busyLoading = computed(() =>
  state.loading || handoverState.loading || formOrderState.loading || uiTopLoading.active
)
onMounted(async () => {
  loadUiPreferences() // FU70: segera setelah mungkin — hindari kedipan zoom telat
  loadCompanyFilter() // FU95: filter company global sedini mungkin (semua fetch menunggunya)
  await loadListPreferences()
  if (section.value !== 'workorder' || woId.value) loadList()
  loadBoard(); loadSuggestionPreferences()
})
watch(() => state.actionError, error => {
  if (!error) return
  nextTick(() => {
    if (!errorDialog.value?.open) errorDialog.value?.showModal()
    errorClose.value?.focus()
  })
})

watch(() => labelPrintPrompt.workOrder, workOrder => {
  if (!workOrder) return
  extraLabelCount.value = 0
  nextTick(() => {
    if (!labelPrintDialog.value?.open) labelPrintDialog.value?.showModal()
  })
})

function closeLabelPrint() {
  if (labelPrintDialog.value?.open) labelPrintDialog.value.close()
  extraLabelCount.value = 0
  labelPrintPrompt.workOrder = ''
  labelPrintPrompt.labelCount = 0
  labelPrintPrompt.afterSave = ''
}

async function confirmLabelPrint() {
  const workOrder = labelPrintPrompt.workOrder
  if (!workOrder || !labelCountValid.value) return
  
  try {
    await printLabels(workOrder, Number(extraLabelCount.value))
    closeLabelPrint()
  } catch (err) {
    state.actionError = {
      title: 'Cetak Label Gagal',
      message: err.message || 'Gagal terhubung ke QZ Tray atau printer.',
      details: [],
      hint: /sign|sertifikat|certificate/i.test(err.message || '')
        ? 'Periksa sertifikat QZ Tray di komputer dan private key di server.'
        : 'Pastikan QZ Tray sedang berjalan di komputer Anda.'
    }
  }
}


function closeError() {
  if (errorDialog.value?.open) errorDialog.value.close()
  state.actionError = null
}

function onNavClick() {
  navOpen.value = false
}

// FU86: refresh global dekat profil — muat ulang seluruh SPA (hash terjaga,
// semua data halaman aktif diambil ulang); ikon saja tanpa tulisan
function muatUlangHalaman() {
  window.location.reload()
}
</script>

<template>
  <div class="app" :class="{ 'nav-open': navOpen }">
    <div class="backdrop" :class="{ show: navOpen }" aria-hidden="true" @click="navOpen = false"></div>

    <div v-if="busyLoading" class="top-loading" role="status" aria-label="Memuat data">
      <span class="top-loading-spin" aria-hidden="true"></span>
      <span class="top-loading-label">Memuat…</span>
    </div>

    <header class="topbar">
      <button class="navburger" aria-label="Buka/tutup menu" @click="navOpen = !navOpen">
        <span class="bars"><span></span><span></span><span></span></span>
      </button>
      <div class="brand">
        <span class="brandmark"><Factory :size="18" :stroke-width="1.9" /></span>
        <div class="brandtext">
          <span class="brandname">PROD<span class="brandtint">APP</span></span>
          <span class="brandsub">Production workspace</span>
        </div>
      </div>
      <div class="topuser">
        <button class="topbtn" type="button" aria-label="Muat ulang halaman" title="Muat ulang" @click="muatUlangHalaman">
          <RotateCw :size="15" :stroke-width="2" />
        </button>
        <button
          type="button"
          class="userbtn"
          :aria-expanded="userMenu"
          aria-haspopup="menu"
          aria-label="Menu pengguna"
          @click="userMenu = !userMenu"
          @keydown.esc="userMenu = false"
        >
          <span class="avatar">{{ initials }}</span>
          <span class="uinfo">
            <span class="uname">{{ currentUser }}</span>
            <span class="urole">ERPNext</span>
          </span>
          <ChevronDown :size="14" :stroke-width="2" class="uchev" :class="{ open: userMenu }" />
        </button>
        <template v-if="userMenu">
          <div class="usermenu-overlay" aria-hidden="true" @click="userMenu = false"></div>
          <transition name="pop" appear>
            <div class="usermenu" role="menu">
              <a role="menuitem" class="usermenu-item" href="/app">
                <LayoutGrid :size="16" :stroke-width="1.9" />
                Buka ERPNext Desk
              </a>
            </div>
          </transition>
        </template>
      </div>
    </header>

    <aside class="sidenav" :class="{ open: navOpen }" aria-label="Navigasi utama">
      <div class="sideinner">
        <div class="sidedrop">
          <button class="navburger" aria-label="Tutup menu" @click="navOpen = false">
            <span class="bars"><span></span><span></span><span></span></span>
          </button>
          <div class="brand">
            <span class="brandmark"><Factory :size="18" :stroke-width="1.9" /></span>
            <div class="brandtext">
              <span class="brandname">PROD<span class="brandtint">APP</span></span>
              <span class="brandsub">Production workspace</span>
            </div>
          </div>
        </div>

        <div class="navsection">Ringkasan</div>
        <a
          href="#/"
          class="navitem"
          :class="{ on: section === 'dashboard' }"
          :aria-current="section === 'dashboard' ? 'page' : undefined"
          @click="onNavClick"
        >
          <LayoutGrid :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Dashboard</span>
        </a>

        <div class="navsection">Produksi</div>
        <a
          href="#/wo"
          class="navitem"
          :class="{ on: section === 'workorder' }"
          :aria-current="section === 'workorder' ? 'page' : undefined"
          @click="onNavClick"
        >
          <ClipboardCheck :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Work Orders</span>
          <span v-if="activeCount" class="navbadge">{{ activeCount }}</span>
        </a>
        <a
          href="#/handover"
          class="navitem"
          :class="{ on: section === 'handover' }"
          :aria-current="section === 'handover' ? 'page' : undefined"
          @click="onNavClick"
        >
          <Package :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Stock Entry</span>
          <span v-if="handoverCount" class="navbadge">{{ handoverCount }}</span>
        </a>

        <div class="navsection">Bahan Baku</div>
        <a
          href="#/ketersediaan-stock"
          class="navitem"
          :class="{ on: section === 'stock-availability' }"
          :aria-current="section === 'stock-availability' ? 'page' : undefined"
          @click="onNavClick"
        >
          <PackageSearch :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Ketersediaan Stock</span>
        </a>
        <a
          v-if="canFormOrder"
          href="#/form-order"
          class="navitem"
          :class="{ on: section === 'form-order' }"
          :aria-current="section === 'form-order' ? 'page' : undefined"
          @click="onNavClick"
        >
          <ClipboardList :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Form Order</span>
        </a>
        <a
          href="#/penggunaan-bahan"
          class="navitem"
          :class="{ on: section === 'material-usage' }"
          :aria-current="section === 'material-usage' ? 'page' : undefined"
          @click="onNavClick"
        >
          <Wheat :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Penggunaan bahan baku</span>
        </a>
        <div class="navsection">Sistem</div>
        <a
          v-if="canSettings"
          href="#/settings"
          class="navitem"
          :class="{ on: section === 'settings' }"
          :aria-current="section === 'settings' ? 'page' : undefined"
          @click="onNavClick"
        >
          <Settings :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Pengaturan</span>
        </a>
        <span class="navitem disabled" title="Belum tersedia">
          <HelpCircle :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Bantuan</span>
        </span>
      </div>
    </aside>

    <nav class="bottomnav" aria-label="Navigasi utama">
      <a
        href="#/"
        class="bnav-item"
        :class="{ on: section === 'dashboard' }"
        :aria-current="section === 'dashboard' ? 'page' : undefined"
        @click="onNavClick"
      >
        <span class="bnav-ic">
          <LayoutGrid :size="20" :stroke-width="1.9" />
        </span>
        <span class="bnav-label">Dashboard</span>
      </a>
      <a
        href="#/wo"
        class="bnav-item"
        :class="{ on: section === 'workorder' }"
        :aria-current="section === 'workorder' ? 'page' : undefined"
        @click="onNavClick"
      >
        <span class="bnav-ic">
          <ClipboardCheck :size="20" :stroke-width="1.9" />
          <span v-if="activeCount" class="bnav-badge">{{ activeCount }}</span>
        </span>
        <span class="bnav-label">Work Order</span>
      </a>
      <a
        href="#/handover"
        class="bnav-item"
        :class="{ on: section === 'handover' }"
        :aria-current="section === 'handover' ? 'page' : undefined"
        @click="onNavClick"
      >
        <span class="bnav-ic">
          <Package :size="20" :stroke-width="1.9" />
          <span v-if="handoverCount" class="bnav-badge">{{ handoverCount }}</span>
        </span>
        <span class="bnav-label">Stock Entry</span>
      </a>
      <a
        v-if="canFormOrder"
        href="#/form-order"
        class="bnav-item"
        :class="{ on: section === 'form-order' }"
        :aria-current="section === 'form-order' ? 'page' : undefined"
        @click="onNavClick"
      >
        <span class="bnav-ic">
          <ClipboardList :size="20" :stroke-width="1.9" />
        </span>
        <span class="bnav-label">Form Order</span>
      </a>
      <!-- FU84: bottom nav HP dipangkas — Penggunaan & Ketersediaan Stock pindah
           ke halaman Lainnya (#/lainnya); sidebar PC tidak berubah -->
      <a
        href="#/lainnya"
        class="bnav-item"
        :class="{ on: section === 'lainnya' }"
        :aria-current="section === 'lainnya' ? 'page' : undefined"
        @click="onNavClick"
      >
        <span class="bnav-ic">
          <MoreHorizontal :size="20" :stroke-width="1.9" />
        </span>
        <span class="bnav-label">Lainnya</span>
      </a>
    </nav>

    <div class="maincol">
      <div class="content">
        <!-- FU70: key per view/WO → wrapper remount → animasi fade halus antarhalaman -->
        <div :key="(section || '') + (foCreate ? '-baru' : '') + (woId || '')" class="view-anim">
          <!-- F04: banner error loader latar adalah STRIP informasi, bukan
               pengganti rute — rantai view di bawah punya v-if sendiri agar
               satu kegagalan endpoint tidak menyembunyikan seluruh halaman -->
          <div v-if="state.error" class="appfoot" style="color:#b3261e">Gagal memuat: {{ state.error }} — <a href="#" @click.prevent="loadList()">coba lagi</a></div>
          <WarehouseSettings v-if="section === 'settings'" />
          <HandoverBoard v-else-if="section === 'handover'" />
          <FormOrderCreate v-else-if="foCreate" />
          <FormOrderPage v-else-if="section === 'form-order'" />
          <Dashboard v-else-if="section === 'dashboard'" />
          <MaterialUsagePage
            v-else-if="section === 'material-usage'"
            :initial-bahan="usageQuery.bahan"
            :initial-dari="usageQuery.dari"
            :initial-sampai="usageQuery.sampai"
            :initial-company="usageQuery.company"
          />
          <StockAvailabilityPage v-else-if="section === 'stock-availability'" />
          <LainnyaPage v-else-if="section === 'lainnya'" />
          <Workspace v-else-if="woId" :key="woId" :id="woId" />
          <WorkOrderList v-else-if="section === 'workorder'" />
        </div>
        <!-- FU85: footer statis "Tersambung ERPNext" dihapus atas permintaan user;
             .appfoot tetap dipakai pesan error fungsional -->
      </div>
    </div>

    <dialog ref="labelPrintDialog" class="dialog" aria-labelledby="label-print-title" @cancel.prevent="closeLabelPrint" @click.self="closeLabelPrint">
      <h3 id="label-print-title">{{ labelPrintPrompt.afterSave ? `${labelPrintPrompt.afterSave} tersimpan` : 'Cetak label?' }}</h3>
      <p>Cetak label untuk Work Order <strong>{{ labelPrintPrompt.workOrder }}</strong>?</p>
      <p>Label sesuai Good Qty: {{ labelPrintPrompt.labelCount }}</p>
      <label for="extra-label-count">Label tambahan</label>
      <input id="extra-label-count" v-model.number="extraLabelCount" type="number" min="0" :max="Math.max(0, 10000 - labelPrintPrompt.labelCount)" step="1" inputmode="numeric" class="input" style="display: block; width: 100%; margin-top: 6px">
      <p v-if="labelCountValid">Total cetak: <strong>{{ totalLabelCount }} label</strong></p>
      <p v-else style="color: #b3261e">Masukkan bilangan bulat 0 atau lebih; maksimal 10.000 label per cetakan.</p>
      <div class="dlg-actions">
        <button class="btn" @click="closeLabelPrint">Nanti saja</button>
        <button class="btn btn-primary" :disabled="!labelCountValid" @click="confirmLabelPrint">Ya, Cetak Label</button>
      </div>
    </dialog>



    <dialog ref="errorDialog" class="dialog error-dialog" aria-labelledby="error-dialog-title" @cancel.prevent="closeError" @click.self="closeError">
      <div class="error-dialog-icon" aria-hidden="true">!</div>
      <h2 id="error-dialog-title">{{ state.actionError?.title }}</h2>
      <p class="error-dialog-message">{{ state.actionError?.message }}</p>
      <ul v-if="state.actionError?.details?.length" class="error-dialog-details">
        <li v-for="detail in state.actionError.details" :key="detail">{{ detail }}</li>
      </ul>
      <div class="dlg-actions">
        <button ref="errorClose" class="btn btn-primary" @click="closeError">Tutup</button>
      </div>
    </dialog>
  </div>
</template>
