import assert from 'node:assert/strict'
import test from 'node:test'

import {
  boxAllocationText, expectedUnits, groupBoxText, unitLabel, unitProblem, validateBoxAllocation
} from '../src/handover-box.js'

// UX check only — server validation (create_request T35/T39) stays authoritative.
test('expectedUnits counts whole units for valid Pack-display AND stock-UOM items', () => {
  // Pack item (valid factor): the pre-T39 behavior is unchanged
  assert.equal(expectedUnits({ displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: 6, producedQty: 234 }), 39)
  assert.equal(expectedUnits({ displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: 6, producedQty: 235 }), null)
  // no factor=1 fallback for a genuinely unconverted ALTERNATE UOM
  assert.equal(expectedUnits({ displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: 0, producedQty: 12 }), null)
  assert.equal(expectedUnits({ displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: NaN, producedQty: 12 }), null)
  assert.equal(expectedUnits({ displayUom: 'Box', stockUom: 'Pcs', qtyInPack: null, producedQty: 12 }), null)
  // a VALID alternate conversion counts in that unit (factor respected)
  assert.equal(expectedUnits({ displayUom: 'Box', stockUom: 'Pcs', qtyInPack: 6, producedQty: 12 }), 2)
  // T39 universal: display == stock UOM needs NO conversion row (factor 1 exact)
  assert.equal(expectedUnits({ displayUom: 'Pcs', stockUom: 'Pcs', qtyInPack: null, producedQty: 22 }), 22)
  assert.equal(expectedUnits({ displayUom: null, stockUom: 'Pcs', qtyInPack: null, producedQty: 22 }), 22)
  assert.equal(expectedUnits({ displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: 6, producedQty: 0 }), 0)
})

test('validateBoxAllocation accepts the exact allocation and a valid two-box split', () => {
  assert.deepEqual(validateBoxAllocation(
    { box1: '12.5', box1Qty: '39', box2: '0', box2Qty: '0' }, 39, 'Pack'
  ), {})
  assert.deepEqual(validateBoxAllocation(
    { box1: '12.5', box1Qty: '20', box2: '8', box2Qty: '19' }, 39, 'Pack'
  ), {})
  // the same rules hold in the item's own unit (Pcs path)
  assert.deepEqual(validateBoxAllocation(
    { box1: '5', box1Qty: '15', box2: '3', box2Qty: '7' }, 22, 'Pcs'
  ), {})
})

test('validateBoxAllocation accepts a valid three-box split and Box 3 untouched by default', () => {
  // W18: Box 3 empty = plain 2-box allocation stays valid
  assert.deepEqual(validateBoxAllocation(
    { box1: '12.5', box1Qty: '20', box2: '8', box2Qty: '19', box3: '', box3Qty: '' }, 39, 'Pack'
  ), {})
  // happy 3-box split: 12 + 7 + 1 = 20 Pack
  assert.deepEqual(validateBoxAllocation(
    { box1: '12.5', box1Qty: '12', box2: '8', box2Qty: '7', box3: '2', box3Qty: '1' }, 20, 'Pack'
  ), {})
})

test('validateBoxAllocation rejects half-filled Box 2 and below/above unit totals', () => {
  assert.ok(validateBoxAllocation(
    { box1: '12.5', box1Qty: '38', box2: '8', box2Qty: '0' }, 39, 'Pack'
  ).box2)
  assert.ok(validateBoxAllocation(
    { box1: '12.5', box1Qty: '20', box2: '8', box2Qty: '18' }, 39, 'Pack'
  ).total)
  assert.ok(validateBoxAllocation(
    { box1: '12.5', box1Qty: '20', box2: '8', box2Qty: '20' }, 39, 'Pack'
  ).total)
  assert.ok(validateBoxAllocation(
    { box1: '0', box1Qty: '39', box2: '0', box2Qty: '0' }, 39, 'Pack'
  ).box1)
  assert.ok(validateBoxAllocation(
    { box1: '12.5', box1Qty: '38.5', box2: '0', box2Qty: '0' }, 39, 'Pack'
  ).box1Qty)
  // a Pcs total mismatch reports the item's own unit in the message
  assert.match(
    validateBoxAllocation({ box1: '5', box1Qty: '21', box2: '0', box2Qty: '0' }, 30, 'Pcs').total,
    /tepat 30 Pcs/
  )
})

test('validateBoxAllocation applies the Box 2 all-or-nothing rule to Box 3 (W18)', () => {
  // half-filled Box 3: kg without count, and count without kg
  assert.ok(validateBoxAllocation(
    { box1: '12.5', box1Qty: '12', box2: '8', box2Qty: '8', box3: '2', box3Qty: '0' }, 20, 'Pack'
  ).box3)
  assert.ok(validateBoxAllocation(
    { box1: '12.5', box1Qty: '12', box2: '8', box2Qty: '8', box3: '0', box3Qty: '3' }, 20, 'Pack'
  ).box3)
  // negative Box 3 kg flagged client-side
  assert.match(
    validateBoxAllocation(
      { box1: '12.5', box1Qty: '20', box2: '0', box2Qty: '0', box3: '-3', box3Qty: '0' }, 20, 'Pack'
    ).box3,
    /non-negatif/
  )
  // fractional Box 3 count flagged
  assert.ok(validateBoxAllocation(
    { box1: '12.5', box1Qty: '12', box2: '8', box2Qty: '8', box3: '2', box3Qty: '1.5' }, 20, 'Pack'
  ).box3Qty)
  // the sum now spans three boxes
  assert.match(
    validateBoxAllocation(
      { box1: '12.5', box1Qty: '12', box2: '8', box2Qty: '8', box3: '2', box3Qty: '2' }, 20, 'Pack'
    ).total,
    /Box 1 \+ Box 2 \+ Box 3 \(22\) harus tepat 20 Pack/
  )
})

test('boxAllocationText renders kg + the row unit per box, kg-only pre-cutover, empty', () => {
  assert.equal(
    boxAllocationText({ box1: 12.5, box1Qty: 20, box2: 8, box2Qty: 19, unit: 'Pack' }),
    'Box 1: 12.5 kg · 20 Pack\nBox 2: 8 kg · 19 Pack'
  )
  assert.equal(
    boxAllocationText({ box1: 5, box1Qty: 15, box2: 3, box2Qty: 7, unit: 'Pcs' }),
    'Box 1: 5 kg · 15 Pcs\nBox 2: 3 kg · 7 Pcs'
  )
  assert.equal(
    boxAllocationText({ box1: 12.5, box1Qty: null, box2: null, box2Qty: null }),
    'Box 1: 12.5 kg'
  )
  assert.equal(boxAllocationText({ box1: null, box1Qty: null, box2: null, box2Qty: null }), '')
})

test('boxAllocationText renders Box 3 (W18) and skips empty middle/last boxes', () => {
  assert.equal(
    boxAllocationText({ box1: 12.5, box1Qty: 12, box2: 8, box2Qty: 7, box3: 2, box3Qty: 1, unit: 'Pack' }),
    'Box 1: 12.5 kg · 12 Pack\nBox 2: 8 kg · 7 Pack\nBox 3: 2 kg · 1 Pack'
  )
  // an empty Box 2 with a kg-only Box 3 (count never invented) keeps the
  // honest Box numbering
  assert.equal(
    boxAllocationText({ box1: 12.5, box1Qty: 20, box2: null, box2Qty: null, box3: 2, box3Qty: null, unit: 'Pack' }),
    'Box 1: 12.5 kg · 20 Pack\nBox 3: 2 kg'
  )
  // rows mapped before box 3 existed (undefined) stay two-box
  assert.equal(
    boxAllocationText({ box1: 12.5, box1Qty: 20, box2: 8, box2Qty: 19, unit: 'Pack' }),
    'Box 1: 12.5 kg · 20 Pack\nBox 2: 8 kg · 19 Pack'
  )
})

test('expectedUnits uses the server integral tolerance (passed/default precision)', () => {
  // 234.0024 / 6 = 39.0004 < 0.0005 (server default precision 3) -> 39 Pack
  const lot = { displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: 6, producedQty: 234.0024 }
  assert.equal(expectedUnits(lot), 39)
  assert.equal(unitProblem(lot), null)
  // 39.0006 >= tolerance -> fractional, and reported as such (not "conversion")
  const over = { displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: 6, producedQty: 234.0036 }
  assert.equal(expectedUnits(over), null)
  assert.equal(unitProblem(over), 'fractional')
  // a passed precision narrows the tolerance like wo.precision() on the server
  assert.equal(expectedUnits(lot, 6), null)
  // missing/invalid conversion stays its own problem class (alternate UOM only)
  assert.equal(unitProblem({ displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: 6, producedQty: 234 }), null)
  assert.equal(unitProblem({ displayUom: 'Pack', stockUom: 'Pcs', qtyInPack: 0, producedQty: 234 }), 'conversion')
  assert.equal(unitProblem(null), 'conversion')
})

test('validateBoxAllocation flags negative Box 2 kg client-side', () => {
  // previously a negative kg with 0 count slipped through as "empty Box 2";
  // the server rejects it, so the dialog must flag it before submit
  const errors = validateBoxAllocation(
    { box1: '12.5', box1Qty: '39', box2: '-3', box2Qty: '0' }, 39, 'Pack'
  )
  assert.match(errors.box2, /non-negatif/)
  // Box 1 negative is still flagged as missing/positive
  assert.ok(validateBoxAllocation(
    { box1: '-1', box1Qty: '39', box2: '0', box2Qty: '0' }, 39, 'Pack'
  ).box1)
})

test('unitLabel resolves the dialog label unit from the lot (T39 dynamic labels)', () => {
  assert.equal(unitLabel({ displayUom: 'Pack', stockUom: 'Pcs' }), 'Pack')
  assert.equal(unitLabel({ displayUom: 'Pcs', stockUom: 'Pcs' }), 'Pcs')
  assert.equal(unitLabel({ displayUom: null, stockUom: 'Pcs' }), 'Pcs')
  assert.equal(unitLabel(null), '')
})

test('groupBoxText renders the shared-box group summary (W19)', () => {
  // the ruling's example shape: plan, live member count, box count, kg, unit
  assert.equal(
    groupBoxText({
      boxPlan: 'HBP-00012', groupSize: 10, unit: 'Pack',
      groupBoxes: [{ kg: 20.5, qty: 36 }, { kg: 15, qty: 40 }, { kg: 10, qty: 20 }]
    }),
    'Group HBP-00012 (10 WO): 3 box · 45.5 kg · 96 Pack'
  )
  // unit follows the row's display UOM (Pcs path)
  assert.equal(
    groupBoxText({ boxPlan: 'HBP-00013', groupSize: 2, unit: 'Pcs', groupBoxes: [{ kg: 7.5, qty: 22 }] }),
    'Group HBP-00013 (2 WO): 1 box · 7.5 kg · 22 Pcs'
  )
  // a live count of 0 members (all shipped/cancelled elsewhere) stays rendered
  assert.equal(
    groupBoxText({ boxPlan: 'HBP-00014', groupSize: 0, unit: 'Pack', groupBoxes: [{ kg: 5, qty: 10 }] }),
    'Group HBP-00014 (0 WO): 1 box · 5 kg · 10 Pack'
  )
})

test('groupBoxText stays empty for normal rows and malformed groups (W19)', () => {
  // non-group rows: the caller falls back to boxAllocationText
  assert.equal(groupBoxText({ box1: 12.5, box1Qty: 20, unit: 'Pack' }), '')
  assert.equal(groupBoxText(null), '')
  assert.equal(groupBoxText({}), '')
  // a plan without physical rows renders nothing (never "0 box · 0 kg")
  assert.equal(groupBoxText({ boxPlan: 'HBP-00015', groupSize: 2, groupBoxes: [] }), '')
})
