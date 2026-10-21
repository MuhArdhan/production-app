import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'

const source = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('WO exposes runtime slots while keeping the general action-error dialog', () => {
  const app = source('App.vue')
  assert.doesNotMatch(app, /labelPrintPrompt|labelPrintDialog|confirmLabelPrint|extraLabelCount|qz-tray/)
  assert.match(app, /state\.actionError/)
  assert.match(app, /ref="errorDialog"/)
  assert.match(app, /function closeError\(/)
  assert.doesNotMatch(app, /@product-qr|WorkOrderLabelDialogs|LabelLayoutSettings/)
  assert.match(app, /<template v-if="section === 'settings'">\s*<WarehouseSettings \/>\s*<div data-product-qr-slot="settings">/)
  assert.match(source('Workspace.vue'), /data-product-qr-slot="actions" :data-work-order="JSON.stringify\(wo\)"/)
  assert.doesNotMatch(source('Workspace.vue'), /offerLabelPrint|ws-label|labelSource|Cetak Label/)
})

test('packing actions retain thin after-save adapters to Product QR', () => {
  const pre = source('stages/StagePacking.vue')
  const post = source('stages/StagePostPacking.vue')
  assert.match(pre, /await savePrePacking\(props\.wo, \{/)
  assert.match(post, /await confirmPostPacking\(props\.wo, \{/)
  for (const code of [pre, post]) {
    assert.doesNotMatch(code, /from ['"]\.\.?\/.*label-print-prompt/)
    assert.doesNotMatch(code, /@product-qr|offerLabelPrint|labelCountForWorkOrder/)
    assert.match(code, /CustomEvent\('production:packing-saved'/)
    assert.match(code, /if \(!canSave\.value\) return/)
    for (const key of ['goodQty', 'rejectQty', 'trialQty', 'sisaQty', 'jam', 'qc']) assert.match(code, new RegExp(`${key}:`))
  }
  assert.match(pre, /workOrder: saved, stage: 'prepacking'/)
  assert.match(post, /workOrder: saved, stage: 'postpacking'/)
})

test('label clients and QZ dependency are no longer shipped by Production App', () => {
  for (const file of ['qz-tray.js', 'label-print-prompt.js', 'work-order-label.js']) {
    assert.equal(existsSync(new URL(`../src/${file}`, import.meta.url)), false)
  }
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url)))
  const lock = JSON.parse(readFileSync(new URL('../package-lock.json', import.meta.url)))
  assert.equal(pkg.dependencies['qz-tray'], undefined)
  assert.equal(pkg.dependencies['@product-qr/wo-labels'], undefined)
  assert.equal(lock.packages[''].dependencies['qz-tray'], undefined)
  assert.equal(lock.packages['node_modules/@product-qr/wo-labels'], undefined)
  assert.match(source('label-runtime.js'), /import\(\/\* @vite-ignore \*\/ url\)/)
})
