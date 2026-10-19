import assert from 'node:assert/strict'
import test from 'node:test'
import { mapDetail } from '../src/store.js'
import { workOrderStages } from '../src/work-order-stages.js'

test('normal WO retains Material; native skip_transfer hides it', () => {
  for (const value of [0, '0', undefined, 1, '1']) {
    const wo = mapDetail({ skip_transfer: value })
    assert.equal(workOrderStages(wo).includes('material'), Number(value) !== 1)
  }
})

test('skipping material and prepacking retains required Operations and Post-Packing', () => {
  const wo = mapDetail({ skip_transfer: 1, skip_prepacking: true, operations: [{ name: 'OP-1' }] })
  assert.deepEqual(workOrderStages(wo), ['persiapan', 'operasi', 'postpacking', 'finish'])
  assert.deepEqual(workOrderStages({ ...wo, hasOperations: false }), ['persiapan', 'postpacking', 'finish'])
})

test('skipping material alone does not skip Pre-Packing or alter the server stage', () => {
  const wo = mapDetail({ skip_transfer: 1, stage: 'pre_packing' })
  assert.deepEqual(workOrderStages(wo), ['persiapan', 'prepacking', 'postpacking', 'finish'])
  assert.equal(wo.stage, 'prepacking')
})
