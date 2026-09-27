<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import WorkOrderList from './WorkOrderList.vue'
import Workspace from './Workspace.vue'
import WarehouseSettings from './WarehouseSettings.vue'
import HandoverBoard from './HandoverBoard.vue'
import FormOrderPage from './FormOrderPage.vue'
import FormOrderCreate from './FormOrderCreate.vue'
import { labelPrintPrompt } from './label-print-prompt.js'
import { workOrderLabelUrl } from './work-order-label.js'
import { workOrders, loadList, loadListPreferences, loadSuggestionPreferences, state, uiTopLoading, handoverBoard, handoverRequests, handoverState, loadBoard, formOrderState } from './store.js'
import { ClipboardCheck, ClipboardList, ChevronDown, LayoutGrid, Settings, HelpCircle, Factory, Package } from 'lucide-vue-next'

// router hash minimal: '#/' + '#/wo/<id>' (work order), '#/handover' (stock entry)
const hash = ref(window.location.hash)
const onHash = () => {
  hash.value = window.location.hash
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
const section = computed(() =>
  hash.value.startsWith('#/settings') ? 'settings'
    : hash.value.startsWith('#/handover') ? 'handover'
      : hash.value.startsWith('#/form-order') ? 'form-order'
        : 'workorder'
)
// FU52: halaman buat Form Order terpisah dari riwayat
const foCreate = computed(() => hash.value === '#/form-order/baru')

// peran dari papan server (bukan simulasi) — untuk Pengaturan. FU48: user
// gudang-only tidak pernah sampai sini lagi (dialihkan ke /app oleh server);
// item menu Work Orders & Stock Entry SELALU tampil (permintaan user
// 2026-09-14): visibilitas menu tidak boleh bergantung pada flag asinkron
// (dulu menyebabkan menu "kedip/hilang" sebelum/saat gagal load board); isi
// halaman tetap difilter izin di server.
const isGudangOnly = computed(() =>
  !!handoverBoard.roles.is_gudang && !handoverBoard.roles.is_produksi
)
const canSettings = computed(() => !isGudangOnly.value)
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
const labelPrintFrame = ref(null)
const printJob = ref(null)
const extraLabelCount = ref(0)
const labelCountValid = computed(() =>
  extraLabelCount.value !== '' &&
  Number.isInteger(Number(extraLabelCount.value)) &&
  Number(extraLabelCount.value) >= 0 &&
  labelPrintPrompt.labelCount + Number(extraLabelCount.value) <= 10000
)
const totalLabelCount = computed(() => labelPrintPrompt.labelCount + Number(extraLabelCount.value || 0))
let printJobSequence = 0
const errorClose = ref(null)
const currentUser = window.workspace_user || 'Pengguna ERPNext'
const initials = currentUser.split(' ').slice(0, 2).map(s => s[0]).join('')
const activeCount = computed(() => workOrders.filter((w) => w.stage !== 'completed').length)
const handoverCount = computed(() =>
  handoverRequests.filter((r) => r.lane === 'request').length
)
// FU20: spinner global di tepi atas untuk semua pemuatan halaman
const busyLoading = computed(() =>
  state.loading || handoverState.loading || formOrderState.loading || uiTopLoading.active
)
onMounted(async () => {
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
  labelPrintPrompt.afterSave = false
}

function confirmLabelPrint() {
  const workOrder = labelPrintPrompt.workOrder
  if (!workOrder || !labelCountValid.value) return
  printJob.value = {
    key: ++printJobSequence,
    url: workOrderLabelUrl(workOrder, Number(extraLabelCount.value)) + '&embedded=1'
  }
  closeLabelPrint()
}

function onLabelFrameLoad() {
  const frameWindow = labelPrintFrame.value?.contentWindow
  if (!frameWindow?.workOrderLabelReady) {
    state.actionError = {
      title: 'Cetak Label Gagal',
      message: 'Halaman label tidak dapat dimuat. Buka kembali Work Order lalu coba cetak lagi.',
      details: [],
      hint: ''
    }
    printJob.value = null
    return
  }
  try {
    frameWindow.focus()
    frameWindow.print()
  } catch (_) {
    state.actionError = {
      title: 'Cetak Label Gagal',
      message: 'Browser tidak dapat membuka dialog printer. Periksa izin cetak pada browser.',
      details: [],
      hint: ''
    }
    printJob.value = null
  }
}

function closeError() {
  if (errorDialog.value?.open) errorDialog.value.close()
  state.actionError = null
}

function onNavClick() {
  navOpen.value = false
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

        <div class="navsection">Workspace</div>
        <a
          href="#/"
          class="navitem"
          :class="{ on: section === 'workorder' }"
          :aria-current="section === 'workorder' ? 'page' : undefined"
          @click="onNavClick"
        >
          <ClipboardCheck :size="18" :stroke-width="1.9" class="nicon" />
          <span class="nlabel">Work Orders</span>
          <span class="navbadge">{{ activeCount }}</span>
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
    </nav>

    <div class="maincol">
      <div class="content">
        <div v-if="state.error" class="appfoot" style="color:#b3261e">Gagal memuat: {{ state.error }} — <a href="#" @click.prevent="loadList()">coba lagi</a></div>
        <WarehouseSettings v-else-if="section === 'settings'" />
        <HandoverBoard v-else-if="section === 'handover'" />
        <FormOrderCreate v-else-if="foCreate" />
        <FormOrderPage v-else-if="section === 'form-order'" />
        <Workspace v-else-if="woId" :key="woId" :id="woId" />
        <WorkOrderList v-else />
        <footer class="appfoot">
          Tersambung ERPNext — server adalah sumber kebenaran.
        </footer>
      </div>
    </div>

    <dialog ref="labelPrintDialog" class="dialog" aria-labelledby="label-print-title" @cancel.prevent="closeLabelPrint" @click.self="closeLabelPrint">
      <h3 id="label-print-title">{{ labelPrintPrompt.afterSave ? 'Pre-Packing tersimpan' : 'Cetak label?' }}</h3>
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

    <iframe
      v-if="printJob"
      :key="printJob.key"
      ref="labelPrintFrame"
      :src="printJob.url"
      title="Dokumen cetak label"
      aria-hidden="true"
      tabindex="-1"
      style="position: fixed; left: -10000px; top: 0; width: 1px; height: 1px; border: 0"
      @load="onLabelFrameLoad"
    ></iframe>

    <dialog ref="errorDialog" class="dialog error-dialog" aria-labelledby="error-dialog-title" @cancel.prevent="closeError" @click.self="closeError">
      <div class="error-dialog-icon" aria-hidden="true">!</div>
      <h2 id="error-dialog-title">{{ state.actionError?.title }}</h2>
      <p class="error-dialog-message">{{ state.actionError?.message }}</p>
      <ul v-if="state.actionError?.details?.length" class="error-dialog-details">
        <li v-for="detail in state.actionError.details" :key="detail">{{ detail }}</li>
      </ul>
      <p v-if="state.actionError?.hint" class="error-dialog-hint">{{ state.actionError.hint }}</p>
      <div class="dlg-actions">
        <button ref="errorClose" class="btn btn-primary" @click="closeError">Tutup</button>
      </div>
    </dialog>
  </div>
</template>
