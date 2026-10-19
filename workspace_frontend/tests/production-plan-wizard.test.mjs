import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const source = readFileSync(new URL('../../production_app/public/js/production_plan.js', import.meta.url), 'utf8')

function wizard() {
  const dialogs = []
  const buttons = []
  const element = () => {
    const node = { length: 1, html: () => node, find: () => element(), addClass: () => node,
      removeClass: () => node, css: () => node, appendTo: () => node, prepend: () => node,
      on: (event, handler) => { node.handler = handler; return node } }
    return node
  }
  let events
  class Dialog {
    constructor(options) {
      this.options = options
      this.values = {}
      this.fields_dict = {}
      for (const field of options.fields) {
        if (!field.fieldname) continue
        this.values[field.fieldname] = field.default
        this.fields_dict[field.fieldname] = { df: field, $input: element(), $wrapper: element() }
      }
      this.$wrapper = element()
      dialogs.push(this)
    }
    show() {}
    hide() {}
    get_value(key) { return this.values[key] }
    set_df_property(key, property, value) { this.fields_dict[key].df[property] = value }
    get_values() { return this.values }
    set_value(key, value) {
      this.values[key] = value
      this.fields_dict[key]?.df.onchange?.()
    }
  }
  const frm = {
    doc: { docstatus: 0, po_items: [] },
    add_custom_button(label, handler) { this.open = handler; return element() },
    add_child() { const row = {}; this.doc.po_items.push(row); return row },
    refresh_field() {}, dirty() {},
  }
  vm.runInNewContext(source, {
    frappe: { ui: { Dialog, form: { on: (name, handlers) => { events = handlers } } },
      call: ({ method, args }) => Promise.resolve({ message: method.endsWith('.item_plan_info') ? {
        item_name: ['KRIM-KOPI', 'PJ260016', 'PJ260008'].includes(args.item_code) ? 'Krim Kopi' : 'Dough',
        stock_uom: 'Kg', default_uom: 'Kg', conversion_factor: 1, found: true,
        skip_transfer_allowed: ['KRIM-KOPI', 'PJ260016', 'PJ260008'].includes(args.item_code),
        bom: { name: 'BOM', quantity: 5 },
      } : {} }), confirm: (message, yes) => yes(),
      show_alert() {}, msgprint() {} },
    __: (text) => text, flt: value => Number(value) || 0, cint: value => parseInt(value, 10) || 0,
    format_number: String, localStorage: { getItem() {}, setItem() {} }, setTimeout: () => {},
    document: { getElementById: () => ({}) },
    $: (html) => { const node = element(); if (String(html).includes('Tambah & Input Lagi')) buttons.push(node); return node },
  })
  events.refresh(frm)
  frm.open()
  const current = () => dialogs.at(-1)
  function fillItem() {
    Object.assign(current().values, { item_code: 'KRIM-KOPI', item_name: 'Krim Kopi',
      bom_no: 'BOM-KRIM', stock_uom: 'Kg', uom: 'Kg', qty_in_uom: 5, conversion_factor: 1,
      skip_transfer_allowed: 1 })
  }
  return { frm, current, fillItem, buttons }
}

test('checked survives back/next and is copied to every allocated row', () => {
  const w = wizard()
  w.fillItem()
  assert.equal(w.current().get_value('custom_skip_transfer'), 1)
  w.current().set_value('custom_skip_transfer', 1)
  w.current().options.primary_action()
  w.current().options.secondary_action()
  assert.equal(w.current().get_value('custom_skip_transfer'), 1)
  w.current().options.primary_action()
  w.current().set_value('num_wo', 3)
  w.current().options.primary_action()
  assert.deepEqual(w.frm.doc.po_items.map(row => row.custom_skip_transfer), [1, 1, 1])
  assert.deepEqual(w.frm.doc.po_items.map(row => row.planned_qty), [5, 5, 5])
})

test('add and input again defaults checked and preserves an explicit unchecked choice', () => {
  const w = wizard()
  w.fillItem()
  w.current().set_value('custom_skip_transfer', 1)
  w.current().options.primary_action()
  w.buttons.at(-1).handler()
  assert.equal(w.current().get_value('custom_skip_transfer'), 1)
  w.fillItem()
  w.current().set_value('custom_skip_transfer', 0)
  w.current().options.primary_action()
  w.current().options.primary_action()
  assert.deepEqual(w.frm.doc.po_items.map(row => row.custom_skip_transfer), [1, 0])
})

test('changing Item immediately hides and resets the choice while waiting for metadata', () => {
  const w = wizard()
  w.fillItem()
  w.current().set_value('custom_skip_transfer', 0)
  w.current().set_value('item_code', 'ITEM-LAIN')
  assert.equal(w.current().get_value('custom_skip_transfer'), 0)
  assert.equal(w.current().fields_dict.custom_skip_transfer.df.hidden, true)
})

test('only Krim Kopi reveals a checked checkbox; another item stays hidden and writes zero', async () => {
  const w = wizard()
  assert.equal(w.current().fields_dict.custom_skip_transfer.df.hidden, 1)
  w.current().set_value('item_code', 'KRIM-KOPI')
  await Promise.resolve()
  assert.equal(w.current().fields_dict.custom_skip_transfer.df.hidden, false)
  assert.equal(w.current().get_value('custom_skip_transfer'), 1)
  w.current().set_value('item_code', 'DOUGH')
  await Promise.resolve()
  assert.equal(w.current().fields_dict.custom_skip_transfer.df.hidden, true)
  assert.equal(w.current().get_value('custom_skip_transfer'), 0)
  // Even a stale hidden value must never enable skip for another item.
  w.current().values.custom_skip_transfer = 1
  w.current().options.primary_action()
  w.current().options.primary_action()
  assert.equal(w.frm.doc.po_items[0].custom_skip_transfer, 0)
})

test('checkbox is directly below conversion factor in the right column with no description', () => {
  const w = wizard()
  const fields = w.current().options.fields
  const index = fields.findIndex(field => field.fieldname === 'custom_skip_transfer')
  assert.equal(fields[index - 1].fieldname, 'conversion_factor')
  assert.ok(fields.slice(0, index).some(field => field.fieldtype === 'Column Break'))
  assert.equal(fields[index].description, undefined)
})

test('real Krim Kopi codes show immediately and a delayed response preserves manual uncheck', async () => {
  for (const code of ['PJ260016', 'PJ260008']) {
    const w = wizard()
    w.current().set_value('item_code', code)
    // Before the metadata promise resolves.
    assert.equal(w.current().fields_dict.custom_skip_transfer.df.hidden, false)
    assert.equal(w.current().get_value('custom_skip_transfer'), 1)
    w.current().set_value('custom_skip_transfer', 0)
    await Promise.resolve()
    assert.equal(w.current().get_value('custom_skip_transfer'), 0)
  }
})
