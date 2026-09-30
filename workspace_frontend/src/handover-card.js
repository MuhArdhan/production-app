import { fmtStampShort, qtyMain } from './format.js'

export function handoverCard(values) {
  return {
    workOrder: values.workOrder || '',
    document: values.document || '',
    batch: values.batch || '',
    adonan: values.adonan ? String(values.adonan) : '', // FU69: Int — 0 = kosong
    quantityLabel: values.quantityLabel || '',
    quantity: qtyMain(values.quantity, values.units),
    timestampLabel: values.timestampLabel || '',
    timestamp: fmtStampShort(values.timestamp).replace(' · ', ', '),
    box: values.box === undefined ? 'Box kosong' : (values.box || '')
  }
}
