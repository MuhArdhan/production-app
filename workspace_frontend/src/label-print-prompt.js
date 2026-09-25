import { reactive } from 'vue'

export const labelPrintPrompt = reactive({ workOrder: '', labelCount: 0 })

export function offerLabelPrint(workOrder, labelCount) {
  labelPrintPrompt.workOrder = workOrder
  labelPrintPrompt.labelCount = labelCount
}
