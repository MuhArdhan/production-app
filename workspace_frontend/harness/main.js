// ---------------------------------------------------------------------------
// DEV-ONLY layout harness (2026-10-10).
//
// Purpose: screenshot the two workspace tables (Work Order list + Stock Entry)
// at real widths WITHOUT logging into ERPNext. It renders the exact production
// markup (same classes: .wo-body/.wo-thead/.wo-row/.c-*) against sample rows so
// the column geometry in styles.css can be verified visually.
//
// It is NOT part of the shipped bundle: it is a separate vite entry loaded only
// by layout-harness.html and never imported by main.js / index.html.
// ---------------------------------------------------------------------------
import { createApp, h } from 'vue'
import '../src/styles.css'
import { fmtDate, qtyStack } from '../src/format.js'
import { ChevronRight, ArrowDown } from 'lucide-vue-next'

// Sample units so qtyStack renders "X Pack · Y Pcs" (mirrors real payloads).
const UNITS = { stockUom: 'Pcs', displayUom: 'Pack', qtyInPack: 25 }

const WO_ROWS = [
  { id: 'WO-JURI-261009061', adonan: 34, product: 'Krim Kopi', itemCode: 'PJ260016', date: '2026-10-09', status: 'Completed', stage: 'Selesai', handover: 'Terkirim', qty: 25 },
  { id: 'WO-JURI-261009060', adonan: 33, product: 'Krim Kopi', itemCode: 'PJ260016', date: '2026-10-09', status: 'Completed', stage: 'Selesai', handover: 'Terkirim', qty: 25 },
  { id: 'WO-JURI-261009059', adonan: 32, product: 'Krim Kopi', itemCode: 'PJ260016', date: '2026-10-09', status: 'Completed', stage: 'Selesai', handover: 'Terkirim', qty: 25 },
  { id: 'WO-JURI-261009058', adonan: 31, product: 'Krim Kopi', itemCode: 'PJ260016', date: '2026-10-09', status: 'In Process', stage: 'Post-Packing', handover: '', qty: 25 },
  { id: 'WO-JURI-261009057', adonan: 30, product: 'Krim Kopi Susu Gula Aren', itemCode: 'PJ260016', date: '2026-10-09', status: 'In Process', stage: 'Pre-Packing', handover: '', qty: 250 },
]

const SE_ROWS = [
  { mr: 'MREQ-MTR-26-0233', se: 'SE-MTF-261010007', item: 'Krim Kopi', itemCode: 'PJ260016', qty: 25, wo: 'WO-JURI-261009028', batch: '-', owner: 'PT. JUARA ROTI INDONESIA', status: 'Terkirim' },
  { mr: 'MREQ-MTR-26-0233', se: 'SE-MTF-261010007', item: 'Krim Kopi', itemCode: 'PJ260016', qty: 25, wo: 'WO-JURI-261009029', batch: '-', owner: 'PT. JUARA ROTI INDONESIA', status: 'Terkirim' },
  { mr: 'MREQ-MTR-26-0233', se: 'SE-MTF-261010007', item: 'Krim Kopi', itemCode: 'PJ260016', qty: 26, wo: 'WO-JURI-261009030', batch: 'BATCH-2610101', owner: 'PT. JUARA ROTI INDONESIA', status: 'Terkirim' },
  { mr: 'MREQ-MTR-26-0234', se: '', item: 'Krim Kopi Susu Gula Aren', itemCode: 'PJ260019', qty: 250, wo: 'WO-JURI-261009031', batch: 'BATCH-2610102', owner: 'PT. JUARA ROTI INDONESIA', status: 'Diminta' },
]

// Form Order history rows (same .wo-row base + fo-history variant).
const FO_ROWS = [
  { mr: 'MREQ-MTR-26-0240', se: 'SE-MTF-261010010', items: 'Krim Kopi, Gula Halus', route: 'Gudang Bahan - ROPI → Gudang Produksi - ROPI', date: '10 Okt 2026', owner: 'PT. JUARA ROTI INDONESIA', status: 'Terkirim', note: 'Prioritas lini 2' },
  { mr: 'MREQ-MTR-26-0241', se: '', items: 'Tepung Serbaguna', route: 'Gudang Bahan - ROPI → Work In Progress - ROPI', date: '11 Okt 2026', owner: 'PT. JUARA ROTI INDONESIA', status: 'Menunggu', note: '-' },
]

function woRow(w) {
  const q = qtyStack(w.qty, UNITS)
  return h('button', { class: 'wo-row', key: w.id }, [
    h('span', { class: 'wo-id c-id' }, w.id),
    h('span', { class: 'c-adonan' }, [h('span', { class: 'mlabel' }, 'Adonan'), ` ${w.adonan}`]),
    h('span', { class: 'wo-prod c-prod' }, [w.product, h('small', { class: 'mono' }, w.itemCode)]),
    h('span', { class: 'c-date' }, fmtDate(w.date)),
    h('span', { class: 'c-status' }, [h('span', { class: ['badge', w.status === 'Completed' ? 'b-done' : 'b-run'] }, w.status)]),
    h('span', { class: 'c-stage' }, [
      h('span', { class: 'chip' }, w.stage),
      w.handover ? h('span', { class: ['chip-gudang', w.handover.toLowerCase()] }, w.handover) : null
    ]),
    h('span', { class: 'wo-qty c-qty' }, [h('span', { class: 'qmain' }, q.main), h('span', { class: 'qsub' }, q.sub)]),
    h('span', { class: 'c-arrow' }, [h(ChevronRight, { size: 16, strokeWidth: 2 })])
  ])
}

function seRow(r) {
  const q = qtyStack(r.qty, UNITS)
  return h('div', { class: 'wo-row se-queue-row rowlink' }, [
    h('span', { class: 'wo-id c-doc' }, [r.mr, r.se ? h('small', { class: 'mono' }, r.se) : null]),
    h('span', { class: 'wo-prod c-item' }, [r.item, h('small', { class: 'mono' }, r.itemCode)]),
    h('span', { class: 'wo-qty c-qty' }, [h('span', { class: 'qmain' }, q.main), q.sub ? h('span', { class: 'qsub' }, q.sub) : null]),
    h('span', { class: 'c-wo' }, r.wo),
    h('span', { class: 'c-batch' }, r.batch),
    h('span', { class: 'c-owner' }, r.owner),
    h('span', { class: 'c-status' }, [h('span', { class: ['chip', r.status === 'Terkirim' ? 'chip-ok' : ''] }, r.status)]),
    h('span', { class: 'c-arrow' }, [h(ChevronRight, { size: 16, strokeWidth: 2 })])
  ])
}

const th = (label) => h('button', { class: 'th-sort' }, [label, h(ArrowDown, { size: 11, strokeWidth: 2.4, class: 'sort-ico' })])

function foRow(o) {
  return h('div', { class: 'wo-row fo-hist-row rowlink' }, [
    h('span', { class: 'wo-id c-doc' }, [o.mr, o.se ? h('small', { class: 'mono' }, o.se) : null]),
    h('span', { class: 'wo-prod c-item' }, [o.items, h('small', {}, o.route)]),
    h('span', { class: 'c-date' }, o.date),
    h('span', { class: 'c-owner' }, o.owner),
    h('span', { class: 'c-status' }, [h('span', { class: ['chip', o.status === 'Terkirim' ? 'chip-ok' : ''] }, o.status)]),
    h('span', { class: 'c-note' }, o.note),
    h('span', { class: 'c-act' }, [o.status === 'Menunggu' ? h('button', { class: 'btn btn-sm' }, 'Batalkan') : null])
  ])
}

const WO_HEAD = ['No. WO', 'Adonan', 'Produk', 'Jadwal', 'Status', 'Tahap', 'Rencana', '']
const SE_HEAD = ['Dokumen', 'Item', 'Qty', 'Work Order', 'Batch', 'Dibuat oleh', 'Status', '']
const FO_HEAD = ['Dokumen', 'Item', 'Dibutuhkan', 'Dibuat oleh', 'Status', 'Catatan', '']

// StageFinish summary markup (exact classes from stages/StageFinish.vue) so the
// label/value layout can be screenshotted at real panel width.
function finRow(label, value, cls) {
  return h('div', { class: 'fin-row' + (cls ? ' ' + cls : '') }, [
    h('span', { class: 'k' }, label),
    h('span', { class: 'v' }, value)
  ])
}
function finBlock(title, rows) {
  return h('div', { class: 'fin-block' }, [
    h('div', { class: 'fin-block-head' }, title),
    h('div', { class: 'fin-rows' }, rows)
  ])
}
function finishPanel() {
  return h('section', { class: 'panel' }, [
    h('div', { class: 'panel-head' }, [
      h('h2', 'Finish')
    ]),
    h('div', { class: 'panel-body' }, [
      h('div', { class: 'fin-kpis' }, [
        h('div', { class: 'fin-kpi' }, [h('span', { class: 'fk-label' }, 'Rencana'), h('span', { class: 'fk-value' }, '25 Pcs')]),
        h('div', { class: 'fin-kpi' }, [h('span', { class: 'fk-label' }, 'Hasil'), h('span', { class: 'fk-value' }, '26 Pcs')]),
        h('div', { class: 'fin-kpi' }, [h('span', { class: 'fk-label' }, 'Selisih'), h('span', { class: 'fk-value pos' }, '+1 Pcs')])
      ]),
      h('div', { class: 'fin-cols' }, [
        finBlock('Pre-Packing', [
          finRow('Good', '26 Pcs'),
          finRow('Reject', '0 Pcs'),
          finRow('Trial', '0 Pcs'),
          finRow('Sisa', '0 Pcs'),
          finRow('Total', '26 Pcs', 'total'),
          h('div', { class: 'fin-row' }, [h('span', { class: 'k' }, 'Jam · QC'), h('span', { class: 'v dim' }, '15:00 · Tita')])
        ]),
        finBlock('Post-Packing', [
          finRow('Good', '26 Pcs'),
          finRow('Reject', '0 Pcs'),
          finRow('Trial', '0 Pcs'),
          finRow('Sisa', '0 Pcs'),
          finRow('Total', '26 Pcs', 'total')
        ])
      ]),
      h('div', { class: 'fin-result' }, [
        h('div', { class: 'fin-result-main' }, [h('span', 'Barang Jadi'), h('span', { class: 'fin-result-qty' }, '26 Pcs')]),
        h('div', { class: 'fin-result-sub' }, [h('span', { class: 'k' }, 'Gudang'), h('span', { class: 'v' }, 'Cold Storage Produksi - JURI')])
      ])
    ])
  ])
}

const Harness = {
  setup() {
    return () => h('div', { class: 'content' }, [
      h('div', { class: 'page-head' }, [h('div', { class: 'ph-left' }, [h('h1', 'Work Order'), h('p', { class: 'sub' }, 'harness layout — tabel WO (contoh data)')])]),
      h('div', { class: 'wo-body' }, [
        h('div', { class: 'wo-thead' }, WO_HEAD.map((l, i) => l ? th(l) : h('span', { key: i }))),
        ...WO_ROWS.map(woRow)
      ]),
      h('div', { class: 'page-head', style: 'margin-top:34px' }, [h('div', { class: 'ph-left' }, [h('h1', 'Stock Entry'), h('p', { class: 'sub' }, 'harness layout — tabel Stock Entry (contoh data)')])]),
      h('div', { class: 'wo-body se-queue' }, [
        h('div', { class: 'wo-thead' }, SE_HEAD.map((l, i) => l ? th(l) : h('span', { key: i }))),
        ...SE_ROWS.map(seRow)
      ]),
      h('div', { class: 'page-head', style: 'margin-top:34px' }, [h('div', { class: 'ph-left' }, [h('h1', 'Form Order'), h('p', { class: 'sub' }, 'harness layout — riwayat Form Order (contoh data)')])]),
      h('div', { class: 'wo-body fo-history' }, [
        h('div', { class: 'wo-thead' }, FO_HEAD.map((l, i) => l ? th(l) : h('span', { key: i }))),
        ...FO_ROWS.map(foRow)
      ]),
      h('div', { class: 'page-head', style: 'margin-top:34px' }, [h('div', { class: 'ph-left' }, [h('h1', 'Finish'), h('p', { class: 'sub' }, 'harness layout — panel Finish (contoh data)')])]),
      finishPanel()
    ])
  }
}

createApp(Harness).mount('#harness')
