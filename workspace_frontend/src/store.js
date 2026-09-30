import { reactive } from 'vue'

// ============================================================================
// ADAPTER SERVER (T13/T14) — menggantikan mock store.
// Semua data & stage datang dari server: /api/method/production_app.api...
// Frontend TIDAK PERNAH menurunkan stage sendiri; setelah tiap aksi, state
// lokal di-update dari respons server (server = sumber kebenaran).
// Serah terima (handover) T25: papan selalu diganti dari payload `board`
// server (HANDOVER_PLAN §4.1) — UI tidak pernah menulis state lane sendiri.
// ============================================================================

export const STAGE_LABELS = {
  persiapan: 'Persiapan',
  material: 'Material',
  operasi: 'Operasi',
  pre_packing: 'Pre-Packing',
  post_packing: 'Post-Packing',
  finish: 'Finish',
  selesai: 'Selesai',
  cancelled: 'Dibatalkan',
  review: 'Review'
}
// kompatibilitas panggilan lama yang memakai kunci mock
STAGE_LABELS.prepacking = 'Pre-Packing'
STAGE_LABELS.postpacking = 'Post-Packing'
STAGE_LABELS.completed = 'Selesai'

// stage server -> kunci internal mockup (komponen tak perlu tahu bedanya)
const STAGE_UI = {
  persiapan: 'persiapan', material: 'material', operasi: 'operasi',
  pre_packing: 'prepacking', post_packing: 'postpacking',
  finish: 'finish', selesai: 'completed',
  cancelled: 'cancelled', review: 'review'
}

// FU20/T35: penanda serah terima per WO — server hanya mengirim dua lane
export const HANDOVER_LABELS = {
  request: 'Diminta Gudang',
  terkirim: 'Terkirim'
}

export const fieldUnits = reactive({})
export const listPrefs = reactive({ view: 'tabel' })
export const PAGE_SIZE_OPTIONS = [20, 100, 500, 1000, 2500]
export function normalizePageSize(value) {
  const size = Number(value)
  return PAGE_SIZE_OPTIONS.includes(size) ? size : PAGE_SIZE_OPTIONS[0]
}
export const listState = reactive({ total: 0, page: 1, pageSize: PAGE_SIZE_OPTIONS[0] })
export const savedListPreferences = reactive({ workOrder: {}, handover: {} })
export const listPreferencesState = reactive({ loaded: false })

export const state = reactive({ loading: false, error: null, loaded: false, pending: null, actionError: null })
// FU20: loading halaman detail/pengaturan diteruskan ke spinner global di tepi atas
export const uiTopLoading = reactive({ active: false })

const ACTION_LABELS = {
  prepare: 'Persiapan',
  transfer_materials: 'Transfer Material',
  jobcard_start: 'Mulai Operasi',
  jobcard_complete: 'Selesaikan Operasi',
  confirm_prepacking: 'Pre-Packing',
  confirm_postpacking: 'Post-Packing',
  finish: 'Finish'
}

function decodeErrorText(value) {
  return String(value || '')
    .replace(/<[^>]*>/g, '')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/\s+/g, ' ')
    .trim()
}

function errorDetails(raw) {
  const text = decodeErrorText(raw)
  const shortage = text.match(/^(.+?)\s+units? of (.+?) needed in (.+?) to complete this transaction\.?$/i)
  if (shortage) {
    return {
      title: 'Transfer Material Gagal',
      message: 'Stok tidak cukup untuk menyelesaikan transfer material.',
      details: [
        `Kebutuhan: ${shortage[1]}`,
        `Material: ${shortage[2]}`,
        `Gudang: ${shortage[3]}`
      ],
      hint: 'Tambahkan stok atau gunakan gudang sumber lain, lalu coba lagi.'
    }
  }
  return {
    title: 'Aksi Tidak Berhasil',
    message: text || 'ERPNext menolak aksi ini. Periksa data Work Order lalu coba lagi.',
    details: [],
    hint: ''
  }
}

export function setActionError(error, method = '') {
  const info = errorDetails(error?.message || error)
  if (method && ACTION_LABELS[method] && info.title === 'Aksi Tidak Berhasil') {
    info.title = `${ACTION_LABELS[method]} Gagal`
  }
  state.actionError = info
}

export const workOrders = reactive([])

function hhmm(v) {
  return v ? (String(v).match(/(?:^|[ T])(\d{1,2}):([0-9]{2})/)?.slice(1).map(x => x.padStart(2, '0')).join(':') || '') : ''
}

export function mapDetail(d) {
  // server wo_detail -> bentuk WO yang dipakai komponen mockup
  return {
    id: d.name,
    name: d.name,
    product: d.production_item_name || d.production_item,
    itemCode: d.production_item,
    plannedDate: (d.planned_start_date || '').slice(0, 10),
    status: d.status,
    stage: STAGE_UI[d.stage] || d.stage,
    // FU46: server memutuskan bolehkah sesi ini mengoreksi Data Adonan
    // (WO selesai hanya Manufacturing Manager) — frontend hanya mengikuti
    canEditPersiapan: !!d.can_edit_persiapan,
    handover: d.handover || null,
    hasOperations: (d.operations || []).length > 0,
    qtyInPack: d.display_conversion_factor ?? null,
    displayUom: d.display_uom || d.stock_uom,
    wholeNumber: !!d.stock_uom_whole_number,
    uomWarning: d.uom_warning,
    producedStockQty: d.produced_qty ?? 0,
    plannedStockQty: d.qty ?? 0,
    stockUom: d.stock_uom,
    conversionFactor: d.custom_conversion_factor ?? 1,
    remainingTransfer: d.remaining_transfer ?? 0,
    remainingProduce: d.remaining_produce ?? 0,
    maxAllowedQty: d.max_allowed_qty ?? 0,
    warehouse: d.fg_warehouse || '',
    wipWarehouse: d.wip_warehouse || '',
    sourceWarehouse: d.source_warehouse || '',
    // FU20: template stage membaca wo.suggestionSources.<key> — tanpa ini
    // undefined.penimbang melempar TypeError dan panel form hilang total.
    suggestionSources: d.suggestion_sources || {},
    persiapan: {
      adonanKe: d.custom_adonan_ke || null, // FU69: Int — 0 = kosong
      jamAdonan: hhmm(d.custom_jam_adonan),
      suhuAdonan: d.custom_suhu_adonan ?? null,
      namaPenimbang: d.custom_nama_penimbang || '',
      penimbangLabel: d.custom_nama_penimbang_full || d.custom_nama_penimbang || '',
      penimbangSuggested: d.suggested_penimbang || '',
      jumlahKru: d.custom_jumlah_kru ?? null,
      jumlahKruSuggested: d.suggested_jumlah_kru ?? null,
      leaderProduksi: d.custom_leader_produksi || '',
      leaderSuggested: d.suggested_leader || '',
      qcProduksiSuggested: d.suggested_qc_produksi || '',
      suggestionSources: d.suggestion_sources || {}
    },
    material: {
      items: (d.required_items || []).map((i) => ({
        code: i.item_code,
        name: i.item_name || i.item_code,
        required: i.required_qty,
        transferred: i.transferred_qty,
        uom: i.stock_uom
      }))
    },
    operations: (d.job_cards || []).map((card) => {
      const operation = (d.operations || []).find(o => o.name === card.operation_id) || {}
      const times = card.time_logs || []
      return {
        id: card.name,
        name: card.operation,
        workstation: card.workstation || operation.workstation || '',
        employees: card.employees || [],
        operator: (card.employees || []).map(e => e.employee_name).join(', '),
        plannedPcs: card.for_quantity ?? d.qty,
        completedPcs: card.total_completed_qty ?? 0,
        lossQty: card.process_loss_qty ?? 0,
        status: card.docstatus === 1 ? 'done' : times.some(t => t.from_time && !t.to_time) ? 'in_progress' : 'pending',
        start: hhmm(times[0]?.from_time),
        end: hhmm(times[times.length - 1]?.to_time)
      }
    }),
    prepacking: {
      goodQty: d.custom_good_qty_prepacking ?? null,
      rejectQty: d.custom_reject_qty_prepacking ?? null,
      trialQty: d.custom_trial_qty_prepacking ?? null,
      sisaQty: d.custom_sisa_qty_prepacking ?? null,
      jam: hhmm(d.custom_jam_pembekuan),
      qc: d.custom_qc_produksi || '',
      qcLabel: d.custom_qc_produksi_full || d.custom_qc_produksi || '',
      confirmed: !!d.custom_prepacking_confirmed
    },
    requiredItemsLoaded: true,
    postpacking: {
      goodQty: d.custom_good_qty_postpacking ?? null,
      rejectQty: d.custom_reject_qty_postpacking ?? null,
      trialQty: d.custom_trial_qty_postpacking ?? null,
      sisaQty: d.custom_sisa_qty_postpacking ?? null,
      jam: hhmm(d.custom_jam_packing),
      qc: d.custom_qc_packing || '',
      qcLabel: d.custom_qc_packing_full || d.custom_qc_packing || '',
      qcSuggested: d.suggested_qc_packing || '',
      confirmed: !!d.custom_postpacking_confirmed
    },
    finishedAt: d.actual_end_date ? String(d.actual_end_date).slice(0, 10) : ''
  }
}

function applyDetail(mapped) {
  const i = workOrders.findIndex((w) => w.id === mapped.id)
  if (i === -1) workOrders.push(mapped)
  else Object.assign(workOrders[i], mapped)
  return workOrders.find(w => w.id === mapped.id)
}

export async function call(method, args) {
  const res = await fetch(`/api/method/${method}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Frappe-CSRF-Token': window.csrf_token || ''
    },
    body: JSON.stringify(args || {})
  })
  const data = await res.json()
  if (!res.ok) {
    const msg = data && data._server_messages
      ? JSON.parse(data._server_messages).map((m) => JSON.parse(m).message).join(' ')
      : (data && data.exc) ? 'Validasi ERPNext gagal'
        : (data && data.message) || res.statusText
    throw new Error(msg)
  }
  return data.message
}

export const suggestionPreferences = reactive({ enabled: true })

export async function loadSuggestionPreferences() {
  try {
    const result = await call('production_app.api.work_order.suggestion_preferences')
    suggestionPreferences.enabled = !!result.enabled
  } catch (error) {
    state.error = error.message
  }
  return suggestionPreferences.enabled
}

export async function saveSuggestionPreferences(enabled) {
  const result = await call('production_app.api.work_order.suggestion_preferences_save', { enabled: enabled ? 1 : 0 })
  suggestionPreferences.enabled = !!result.enabled
  return suggestionPreferences.enabled
}

export async function loadList(filters = {}) {
  state.loading = true
  state.error = null
  try {
    const result = await call('production_app.api.work_order.wo_list', {
      search: filters.search || null,
      production_item: filters.productionItem || null,
      status: filters.status || null,
      start_date: filters.startDate || null,
      end_date: filters.endDate || null,
      stage: filters.stage || null,
      start: filters.start ?? 0,
      page_len: filters.pageLen ?? listState.pageSize,
      meta: 1
    })
    const rows = result.rows || []
    listState.total = result.total ?? rows.length
    listState.page = Math.floor((result.start || 0) / (result.page_len || listState.pageSize)) + 1
    const details = new Map(workOrders.filter(w => w.requiredItemsLoaded).map(w => [w.id, w]))
    const mapped = rows.map(r => details.get(r.name) || { ...mapDetail(r), hasOperations: !!r.has_operations, requiredItemsLoaded: false })
    workOrders.splice(0, workOrders.length, ...mapped)
    state.loaded = true
  } catch (e) {
    state.error = e.message
  } finally {
    state.loading = false
  }
}

export async function loadListPreferences() {
  try {
    Object.assign(savedListPreferences, await call('production_app.api.work_order.list_preferences'))
    listPreferencesState.loaded = true
  } catch (e) {
    state.error = e.message
  }
  return savedListPreferences
}

export async function saveListPreferences(values) {
  const result = await call('production_app.api.work_order.list_preferences_save', { values })
  Object.assign(savedListPreferences, result)
  return result
}

export async function refreshWo(id) {
  const d = await call('production_app.api.work_order.wo_detail', { name: id })
  return applyDetail(mapDetail(d))
}

export function getWo(id) {
  return workOrders.find((w) => w.id === id) || null
}

export async function openWo(id) { return refreshWo(id) }

// ---- helper material (bentuk mapped) ----
export function materialShortages(w) {
  return (w.material?.items || []).filter((i) => i.transferred < i.required)
}
export function materialComplete(w) {
  return materialShortages(w).length === 0
}
export function opsAllDone(w) {
  return !!w.operations && w.operations.every((o) => o.status === 'done')
}
// Barang Jadi manufaktur = Good Qty Post-Packing; fallback Pre-Packing hanya
// DISPLAY untuk WO completed legacy (tanpa backfill) — POSTPACKING_PLAN §7.
export function producedQty(w) {
  return w.postpacking?.goodQty ?? w.prepacking?.goodQty ?? 0
}

// Shared by workspace panels and kanban dialogs. Errors remain visible in the form.
async function perform(w, method, args = {}) {
  if (state.pending) return null
  state.pending = w.id
  state.actionError = null
  try {
    await call(`production_app.api.work_order.${method}`, { name: w.id, ...args })
    return await refreshWo(w.id)
  } catch (error) {
    setActionError(error, method)
    return null
  } finally {
    state.pending = null
  }
}

export function startProduction(w, data) {
  return perform(w, 'prepare', {
    values: {
      adonan_ke: data.adonanKe, jam_adonan: data.jamAdonan,
      suhu_adonan: data.suhuAdonan, penimbang: data.namaPenimbang,
      jumlah_kru: data.jumlahKru, leader: data.leaderProduksi
    }, submit: 1
  })
}
export function transferAll(w) { return perform(w, 'transfer_materials') }
export function confirmMaterial(w) { return refreshWo(w.id) }
export function confirmOperations(w) { return refreshWo(w.id) }
export function startOperation(w, op, employee) {
  return perform(w, 'jobcard_start', {
    job_card: op.id, employees: employee ? [{ employee }] : op.employees
  })
}
export function finishOperation(w, op, qty, loss = 0) {
  return perform(w, 'jobcard_complete', { job_card: op.id, qty, process_loss_qty: loss, auto_submit: 1 })
}
export function savePrePacking(w, data) {
  return perform(w, 'confirm_prepacking', { values: {
    // kolom opsional kosong dikirim sebagai 0 (valid menurut kontrak T10)
    good: data.goodQty, reject: data.rejectQty ?? 0, trial: data.trialQty ?? 0, sisa: data.sisaQty ?? 0,
    jam_pembekuan: data.jam, qc_produksi: data.qc
  } })
}
// sisa kini input manual (FU11); jam kosong diisi server dgn jam saat simpan
export function confirmPostPacking(w, data) {
  return perform(w, 'confirm_postpacking', { values: {
    good: data.goodQty, reject: data.rejectQty ?? 0, trial: data.trialQty ?? 0,
    sisa: data.sisaQty ?? 0,
    jam_packing: data.jam, qc_packing: data.qc
  } })
}
export function completeProduction(w) { return perform(w, 'finish') }

// ============================================================================
// SERAH TERIMA / STOCK ENTRY (T25) — papan 4 lane dari server (T23/T24).
// Kontrak payload: .superpowers/sdd/HANDOVER_PLAN/task-23-report.md.
// ============================================================================

export const handoverState = reactive({ loading: false, error: null, loaded: false, pending: null, coldPage: 1, coldPageSize: 20 })
export const handoverBoard = reactive({
  targetWarehouse: null,
  // FU64: can_settings = kapabilitas server (write Manufacturing Settings)
  // yang menentukan menu Pengaturan — bukan tebakan nama role di UI.
  roles: { is_gudang: false, is_produksi: false, is_manajer_produksi: false, can_settings: false }
})
export const handoverLots = reactive([])
export const handoverRequests = reactive([])

// lot/request yang sudah dipetakan dobel sebagai objek satuan QtyInput
// (qtyInPack/displayUom/stockUom/wholeNumber) — pola sama dengan :units="wo".
function mapLot(l) {
  return {
    workOrder: l.work_order, batch: l.batch, batchless: !!l.batchless, item: l.item_name, itemCode: l.item_code,
    stockQty: l.qty, adonanKe: l.adonan_ke, stockUom: l.stock_uom,
    displayUom: l.display_uom, qtyInPack: l.display_conversion_factor,
    wholeNumber: !!l.stock_uom_whole_number, uomWarning: l.uom_warning,
    physicalQty: l.physical_qty, reservedQty: l.reserved_qty, availableQty: l.available_qty,
    warehouse: l.warehouse, enteredAt: l.entered_at,
    producedQty: l.produced_qty, completedAt: l.completed_at,
    hasOlderLotSameItem: !!l.has_older_lot_same_item,
    unsupported: !!l.unsupported, unsupportedReason: l.unsupported_reason
  }
}

function mapRequest(r) {
  return {
    id: r.mr, materialRequest: r.mr, workOrder: r.work_order,
    item: r.item_name, itemCode: r.item_code,
    requestedQtyPcs: r.qty, stockUom: r.stock_uom, qtyInPack: r.qty_in_pack,
    adonanKe: r.adonan_ke, batch: r.batch,
    // T35/T39: alokasi box (kg + jumlah) dari ringkasan Work Order / MR
    // legacy; `unit` = satuan gudang item (Pack/Pcs/dll.) untuk label UI
    box1: r.box_1, box1Qty: r.box_1_qty,
    box2: r.box_2, box2Qty: r.box_2_qty,
    box3: r.box_3, box3Qty: r.box_3_qty,
    // W19: grup box bersama — rencana fisik dipakai bersama anggota grup
    boxPlan: r.box_plan || null, groupBoxes: r.group_boxes || [], groupSize: r.group_size ?? null,
    unit: r.display_uom || r.stock_uom,
    lane: r.lane, flag: r.flag,
    fromWarehouse: r.from_warehouse, toWarehouse: r.to_warehouse,
    // FU29: stok live di gudang asal rute (batch/pool) — null = tak dapat dihitung
    routeAvailable: r.route_available ?? null,
    stockEntry: r.stock_entry, sentAt: r.sent_at, ownerName: r.owner_name,
    createdAt: r.creation
  }
}

function applyBoard(board) {
  handoverBoard.targetWarehouse = board.target_warehouse || null
  handoverBoard.sourceWarehouse = board.source_warehouse || null
  handoverBoard.roles = board.roles || { is_gudang: false, is_produksi: false, is_manajer_produksi: false, can_settings: false }
  handoverLots.splice(0, handoverLots.length, ...board.lots.map(mapLot))
  handoverRequests.splice(0, handoverRequests.length, ...board.requests.map(mapRequest))
}

export async function loadBoard() {
  handoverState.loading = true
  handoverState.error = null
  try {
    applyBoard(await call('production_app.api.handover.handover_board'))
    handoverState.loaded = true
  } catch (e) {
    handoverState.error = e.message
  } finally {
    handoverState.loading = false
  }
}

async function handoverAction(method, args) {
  if (handoverState.pending) return null
  handoverState.pending = method
  try {
    const res = await call(`production_app.api.handover.${method}`, args)
    applyBoard(res.board) // server truth menggantikan seluruh papan
    return res
  } finally {
    handoverState.pending = null
  }
}

// FU48 produksi-only: create_request/cancel_request/fulfill_form_order tidak
// lagi dipanggil SPA (UI gudang dihapus); endpoint server tetap hidup dan
// bisa dipanggil via HTTP langsung bila kelak perlu.
export function sendHandover(materialRequest) {
  return handoverAction('send_handover', { material_request: materialRequest })
}

export function lotForWo(woId) {
  return handoverLots.find((l) => l.workOrder === woId) || null
}
// fisik = saldo ledger live (server); reservasi/tersedia juga dari server (§4.1)
export const lotRemainingPcs = (lot) => lot?.physicalQty
export const lotReservedPcs = (lot) => lot?.reservedQty ?? 0
export const lotAvailablePcs = (lot) => lot?.availableQty

// ============================================================================
// FORM ORDER (FO 2026-09-18, TASKS.md section I) — produksi meminta barang
// dari gudang via MR native. Status selalu dari server (menunggu/terkirim/
// batal/draf); setelah tiap aksi, daftar diganti dari respons server.
// ============================================================================

export const formOrderState = reactive({ loading: false, error: null, loaded: false, pending: null, attachmentIssue: null })
export const formOrders = reactive([])

function mapFormOrder(o) {
  return {
    id: o.mr,
    materialRequest: o.mr,
    status: o.status,
    docstatus: o.docstatus,
    items: (o.items || []).map((i) => ({ code: i.item_code, name: i.item_name, qty: i.qty, uom: i.uom })),
    note: o.note || null,
    scheduleDate: o.schedule_date || null,
    fromWarehouse: o.from_warehouse || null,
    toWarehouse: o.to_warehouse || null,
    stockEntry: o.stock_entry || null,
    sentAt: o.sent_at || null,
    owner: o.owner,
    ownerName: o.owner_name || o.owner,
    createdAt: o.creation
  }
}

function applyFormOrders(orders) {
  formOrders.splice(0, formOrders.length, ...(orders || []).map(mapFormOrder))
}

export async function loadFormOrders() {
  formOrderState.loading = true
  formOrderState.error = null
  try {
    applyFormOrders((await call('production_app.api.form_order.form_order_list')).orders)
    formOrderState.loaded = true
  } catch (e) {
    formOrderState.error = e.message
  } finally {
    formOrderState.loading = false
  }
}

async function formOrderAction(method, args) {
  if (formOrderState.pending) return null
  formOrderState.pending = method
  try {
    const res = await call(`production_app.api.form_order.${method}`, args)
    applyFormOrders(res.orders) // server truth menggantikan seluruh daftar
    return res
  } finally {
    formOrderState.pending = null
  }
}

// error dilempar apa adanya — form/aksi mempertahankan isian pengguna (FU14)
export function foItemInfo(itemCode) {
  return call('production_app.api.form_order.item_info', { item_code: itemCode })
}
async function uploadNativeAttachment(file, doctype, docname) {
  const form = new FormData()
  form.append('file', file, file.name)
  form.append('doctype', doctype)
  form.append('docname', docname)
  form.append('folder', 'Home/Attachments')
  form.append('is_private', '1')

  const response = await fetch('/api/method/upload_file', {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'X-Frappe-CSRF-Token': window.csrf_token || ''
    },
    body: form
  })
  let data = {}
  try { data = await response.json() } catch { /* non-JSON server error */ }
  if (!response.ok || data.exc) {
    let message = data.message || response.statusText || 'Unggah lampiran gagal.'
    try {
      const messages = JSON.parse(data._server_messages || '[]')
        .map((entry) => JSON.parse(entry).message)
        .filter(Boolean)
      if (messages.length) message = messages.join(' ')
    } catch { /* keep the ordinary response message */ }
    throw new Error(decodeErrorText(message) || 'Unggah lampiran gagal.')
  }
  return data.message
}

async function uploadFormOrderAttachments(issue) {
  while (issue.files.length) {
    const file = issue.files[0]
    try {
      await uploadNativeAttachment(file, 'Material Request', issue.materialRequest)
    } catch (error) {
      issue.message = `Gagal mengunggah ${file.name}: ${error.message}`
      throw new Error(issue.message)
    }
    // Keep only files that have not been uploaded; retry must not duplicate earlier files.
    issue.files.shift()
  }
}

export async function createFormOrder(rows, scheduleDate, note, attachments = []) {
  if (formOrderState.pending) throw new Error('Form Order sedang diproses.')
  formOrderState.pending = 'create_form_order'
  try {
    const result = await call('production_app.api.form_order.create_form_order', {
      // FU48c: satuan terpilih per baris (kosong → server pakai stock_uom)
      items: rows.map((r) => ({ item_code: r.code, qty: Number(r.qty), uom: r.uom || null })),
      schedule_date: scheduleDate || null,
      note: note || null
    })
    applyFormOrders(result.orders)

    if (attachments.length) {
      const issue = { materialRequest: result.material_request, files: [...attachments], message: '' }
      formOrderState.pending = 'attach_form_order'
      try {
        await uploadFormOrderAttachments(issue)
      } catch (error) {
        // The MR already exists. Keep only files still waiting for upload.
        formOrderState.attachmentIssue = issue
        return { ...result, attachment_error: error.message }
      }
    }
    return result
  } finally {
    formOrderState.pending = null
  }
}

export async function retryFormOrderAttachment() {
  const issue = formOrderState.attachmentIssue
  if (!issue?.files?.length || !issue.materialRequest) throw new Error('Lampiran Form Order tidak tersedia untuk diunggah ulang.')
  if (formOrderState.pending) throw new Error('Unggah lampiran sedang diproses.')
  formOrderState.pending = 'attach_form_order'
  try {
    const count = issue.files.length
    await uploadFormOrderAttachments(issue)
    formOrderState.attachmentIssue = null
    return count
  } finally {
    formOrderState.pending = null
  }
}
export function cancelFormOrder(materialRequest) {
  return formOrderAction('cancel_form_order', { material_request: materialRequest })
}
