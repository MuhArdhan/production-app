import assert from 'node:assert/strict'
import test from 'node:test'

import { handoverCard } from '../src/handover-card.js'

const units = { stockUom: 'Pcs', displayUom: null, qtyInPack: null }

test('keeps request document and batch on separate rows', () => {
  assert.deepEqual(handoverCard({
    kind: 'request',
    workOrder: 'MFG-WO-2026-02043',
    document: 'MAT-MR-2026-00270',
    batch: 'DubaiKK-260814-012',
    adonan: 12,
    quantity: 22,
    quantityLabel: 'Diminta',
    timestampLabel: 'Waktu',
    timestamp: '2026-09-15 06:23:00',
    units
  }), {
    workOrder: 'MFG-WO-2026-02043',
    document: 'MAT-MR-2026-00270',
    batch: 'DubaiKK-260814-012',
    adonan: '12',
    quantityLabel: 'Diminta',
    quantity: '22 Pcs',
    timestampLabel: 'Waktu',
    timestamp: '15 Sep, 06:23',
    box: 'Box kosong'
  })
})

test('omits an absent batch and shows alternate quantity on one line', () => {
  const card = handoverCard({
    kind: 'done',
    workOrder: 'MFG-WO-2026-03133',
    document: 'MAT-STE-2026-06677',
    quantity: 216,
    quantityLabel: 'Ditransfer',
    timestampLabel: 'Dikirim',
    timestamp: '2026-09-15 07:05:00',
    units: { stockUom: 'Pcs', displayUom: 'Pack', qtyInPack: 6 },
    box: '12.5 / 8.25 kg'
  })

  assert.equal(card.batch, '')
  assert.equal(card.quantity, '36 Pack · 216 Pcs')
  assert.equal(card.box, '12.5 / 8.25 kg')
})

test('does not show a box status on Cold Storage cards', () => {
  const card = handoverCard({
    workOrder: 'MFG-WO-2026-02043',
    quantity: 22,
    quantityLabel: 'Hasil WO',
    timestampLabel: 'Selesai',
    timestamp: '2026-09-15 06:23:00',
    units,
    box: null
  })

  assert.equal(card.box, '')
})

test('adonan 0 (kosong pasca-Int) tampil kosong, bukan nol', () => {
  assert.equal(handoverCard({
    kind: 'request',
    workOrder: 'MFG-WO-2026-00001',
    document: 'MAT-MR-2026-00001',
    batch: null,
    adonan: 0,
    quantity: 1,
    quantityLabel: 'Diminta',
    timestampLabel: 'Waktu',
    timestamp: '2026-09-30 08:00:00',
    units
  }).adonan, '')
})
