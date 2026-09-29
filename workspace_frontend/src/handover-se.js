// FU47: logika klik baris tabel Stock Entry — helper murni (tanpa akses
// store/Vue) agar ter-test terpisah. Semantik sengaja IDENTIK dengan klik
// kartu kanban (clickCard). FU48 produksi-only: request tanpa flag → kirim
// (cabang gudang/multi-role dihapus); baris Terkirim membuka detail baca-saja
// (afordansi khusus tabel); flag draft/cancelled/stopped tidak klikabel.
// FU64: Manufacturing Manager juga boleh kirim — selaras gate server
// send_handover (produksi ATAU manager) dan klik kartu kanban.
// Parameter roles dipertahankan agar signature stabil dan null-safety teruji.
export function rowClickAction(r, roles) {
  if (!r || !roles) return null
  if (r.lane === 'request' && !r.flag) {
    return roles.is_produksi || roles.is_manajer_produksi ? 'send' : null
  }
  if (r.lane === 'terkirim' && r.stockEntry) return 'done'
  return null
}

// label rute kiriman: gudang asal → tujuan; MR legacy tanpa rute eksplisit
// jatuh ke label Cold Storage / target papan.
export function seRouteLabel(r, fallbackTarget) {
  return `${r?.fromWarehouse || 'Cold Storage'} → ${r?.toWarehouse || fallbackTarget || '-'}`
}
