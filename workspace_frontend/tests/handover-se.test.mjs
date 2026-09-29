import assert from 'node:assert/strict'
import { test } from 'node:test'
import { rowClickAction, seRouteLabel } from '../src/handover-se.js'

const req = (over = {}) => ({ lane: 'request', flag: null, stockEntry: null, ...over })

test('request tanpa flag: produksi ATAU manager → kirim (FU64: gate server selaras kanban)', () => {
  const produksi = { is_gudang: false, is_produksi: true }
  assert.equal(rowClickAction(req(), produksi), 'send')
  // FU64: Manufacturing Manager murni juga boleh kirim (dulu baris mati
  // padahal kanban bisa — inkonsisten dengan gate server)
  assert.equal(rowClickAction(req(), { is_produksi: false, is_manajer_produksi: true }), 'send')
  // role lain (gudang-only / tanpa role) tidak lagi punya aksi baris
  assert.equal(rowClickAction(req(), { is_gudang: true, is_produksi: false }), null)
  assert.equal(rowClickAction(req(), { is_gudang: false, is_produksi: false }), null)
})

test('flag stopped/cancelled/draft tidak klikabel (kartu mati/riwayat Desk)', () => {
  const roles = { is_gudang: false, is_produksi: true }
  assert.equal(rowClickAction(req({ flag: 'stopped' }), roles), null)
  assert.equal(rowClickAction(req({ flag: 'cancelled' }), roles), null)
  assert.equal(rowClickAction(req({ flag: 'draft' }), roles), null)
})

test('baris terkirim membuka detail baca-saja SE termuat', () => {
  const roles = { is_gudang: false, is_produksi: true }
  assert.equal(rowClickAction(req({ lane: 'terkirim', stockEntry: 'STE-2026-001' }), roles), 'done')
  // lane terkirim tanpa bukti SE termuat — tetap diam, tidak ada yang bisa dibaca
  assert.equal(rowClickAction(req({ lane: 'terkirim', stockEntry: null }), roles), null)
  // detail baca-saja tersedia untuk SEMUA role
  assert.equal(rowClickAction(req({ lane: 'terkirim', stockEntry: 'STE-2026-001' }), { is_gudang: true, is_produksi: false }), 'done')
})

test('input kosong/null aman', () => {
  assert.equal(rowClickAction(null, { is_produksi: true }), null)
  assert.equal(rowClickAction(req(), null), null)
})

test('seRouteLabel: rute MR + fallback target papan / label Cold Storage', () => {
  assert.equal(seRouteLabel({ fromWarehouse: 'Cold Storage', toWarehouse: 'Gudang BJ' }), 'Cold Storage → Gudang BJ')
  assert.equal(seRouteLabel({ fromWarehouse: 'Cold Storage' }, 'Gudang BJ'), 'Cold Storage → Gudang BJ')
  assert.equal(seRouteLabel({}, 'Gudang BJ'), 'Cold Storage → Gudang BJ')
  assert.equal(seRouteLabel({}), 'Cold Storage → -')
  assert.equal(seRouteLabel(null, 'Gudang BJ'), 'Cold Storage → Gudang BJ')
})
