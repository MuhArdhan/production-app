// Pure box-allocation helpers (T37; T39 universal unit). UX checks only —
// server validation (production_app.api.handover.create_request, T35/T39)
// remains authoritative.
import { fmtNum } from './format.js'

// Server default for the whole-count integral tolerance:
// handover._expected_unit_count uses wo.precision('produced_qty') or 3
// -> tolerance = 0.5 * 10^-precision.
const DEFAULT_PRECISION = 3

// Why the count is unavailable: 'conversion' = the item's warehouse display
// UOM is an alternate UOM without a valid conversion (qtyInPack), 'fractional'
// = valid conversion but the Work Order output does not form whole units,
// null = count available. The count unit itself is universal (T39): the
// display UOM equals the stock UOM -> exact factor 1, no conversion needed.
export function unitProblem(lot, precision = DEFAULT_PRECISION) {
  if (!lot) return 'conversion'
  const stock = lot.stockUom
  const unit = lot.displayUom || stock
  let factor = 1
  if (unit !== stock) {
    factor = Number(lot.qtyInPack)
    if (!Number.isFinite(factor) || factor <= 0) return 'conversion'
  }
  const raw = Number(lot.producedQty) / factor
  if (!Number.isFinite(raw) || raw < 0) return 'fractional'
  const tolerance = 0.5 * 10 ** -precision
  if (Math.abs(raw - Math.round(raw)) >= tolerance) return 'fractional'
  return null
}

// Whole count the WO's full produced qty allocates into, in the item's
// warehouse display UOM, or null when unitProblem says conversion/fractional —
// never a factor=1 fallback for a genuinely unconverted alternate UOM
// (mirrors handover._expected_unit_count).
export function expectedUnits(lot, precision = DEFAULT_PRECISION) {
  if (unitProblem(lot, precision)) return null
  const stock = lot.stockUom
  const unit = lot.displayUom || stock
  const factor = unit === stock ? 1 : Number(lot.qtyInPack)
  return Math.round(Number(lot.producedQty) / factor)
}

// The count unit label for the dialog inputs and card pills.
export function unitLabel(lot) {
  if (!lot) return ''
  return lot.displayUom || lot.stockUom || ''
}

// Mirror of handover._validate_box_allocation: Box 1 kg + count positive;
// Box 2/3 exactly 0/0 or positive/positive; the count sum must equal `expected`
// exactly. Returns field-keyed error strings ('' -> untouched, invalid text
// -> rejected); expected=null (invalid conversion) skips only the sum check.
export function validateBoxAllocation(form, expected, unit = 'Pack') {
  const errors = {}
  const num = (v) => {
    const s = String(v ?? '').trim()
    if (s === '') return null
    const n = Number(s)
    return Number.isFinite(n) ? n : NaN
  }
  const kg1 = num(form.box1)
  const qtys1 = num(form.box1Qty)
  const kg2 = num(form.box2)
  const qtys2 = num(form.box2Qty)
  const kg3 = num(form.box3)
  const qtys3 = num(form.box3Qty)
  if (kg1 == null || kg1 <= 0) errors.box1 = 'Box 1: berat kg wajib diisi dan positif.'
  if (qtys1 == null || qtys1 <= 0 || !Number.isInteger(qtys1))
    errors.box1Qty = `Box 1: jumlah ${unit} wajib bilangan bulat positif.`
  for (const [n, kg, qtys] of [[2, kg2, qtys2], [3, kg3, qtys3]]) {
    const kgFilled = kg != null && kg > 0
    const qtysFilled = qtys != null && qtys > 0
    if (kg != null && kg < 0)
      errors[`box${n}`] = `Box ${n}: berat kg harus angka non-negatif yang valid.`
    else if (kgFilled !== qtysFilled)
      errors[`box${n}`] = `Box ${n} harus kosong (0 kg / 0 jumlah) atau terisi keduanya.`
    else if (qtysFilled && !Number.isInteger(qtys))
      errors[`box${n}Qty`] = `Box ${n}: jumlah ${unit} wajib bilangan bulat.`
  }
  if (
    expected != null &&
    Number.isInteger(qtys1) && qtys1 > 0 &&
    (qtys2 == null || Number.isInteger(qtys2)) &&
    (qtys3 == null || Number.isInteger(qtys3)) &&
    qtys1 + (qtys2 ?? 0) + (qtys3 ?? 0) !== expected
  )
    errors.total = `Jumlah ${unit} Box 1 + Box 2 + Box 3 (${qtys1 + (qtys2 ?? 0) + (qtys3 ?? 0)}) harus tepat ${expected} ${unit}.`
  return errors
}

// kg + count summary for Request/Terkirim cards and the send confirmation,
// e.g. "Box 1: 12.5 kg · 20 Pack\nBox 2: 8 kg · 19 Pack" ('' = no
// allocation). The count unit comes from the row (T39); pre-cutover rows
// carry kg without counts — shown honestly, never invented.
export function boxAllocationText(row) {
  if (!row) return ''
  const lines = []
  const unit = row.unit ? ` ${row.unit}` : ''
  for (const [n, kg, qtys] of [
    [1, row.box1, row.box1Qty], [2, row.box2, row.box2Qty], [3, row.box3, row.box3Qty]
  ]) {
    if (kg == null && qtys == null) continue
    const parts = [
      kg == null ? null : `${fmtNum(kg)} kg`,
      qtys == null ? null : `${fmtNum(qtys)}${unit}`
    ].filter(Boolean)
    lines.push(`Box ${n}: ${parts.join(' · ')}`)
  }
  return lines.join('\n')
}

// W19 shared-box group: one summary line INSTEAD of the per-box chips — the
// physical boxes are shared by the member WOs, so per-WO-per-box attribution
// does not exist. E.g. "Group HBP-00012 (10 WO): 3 box · 45.5 kg · 96 Pack".
// '' = not a group row (callers fall back to boxAllocationText).
export function groupBoxText(row) {
  if (!row || !row.boxPlan) return ''
  const boxes = row.groupBoxes || []
  if (!boxes.length) return ''
  const unit = row.unit ? ` ${row.unit}` : ''
  const kg = boxes.reduce((s, b) => s + (Number(b.kg) || 0), 0)
  const qty = boxes.reduce((s, b) => s + (Number(b.qty) || 0), 0)
  const size = row.groupSize != null ? ` (${row.groupSize} WO)` : ''
  return `Group ${row.boxPlan}${size}: ${boxes.length} box · ${fmtNum(kg)} kg · ${fmtNum(qty)}${unit}`
}
