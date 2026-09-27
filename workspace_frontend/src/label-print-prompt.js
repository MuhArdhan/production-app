import { reactive } from 'vue'
import { hasAlternate } from './format.js'

export function labelCountForWorkOrder(workOrder) {
  const goodQty = Number(workOrder.prepacking?.goodQty)
  const factor = hasAlternate(workOrder) ? workOrder.qtyInPack : 1
  return Math.max(1, Math.ceil(goodQty / factor - 1e-8))
}

export const labelPrintPrompt = reactive({ workOrder: '', labelCount: 0, afterSave: false })

export function offerLabelPrint(workOrder, labelCount, afterSave = false) {
  labelPrintPrompt.workOrder = workOrder
  labelPrintPrompt.labelCount = labelCount
  labelPrintPrompt.afterSave = afterSave
}
