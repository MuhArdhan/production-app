export function workOrderLabelUrl(workOrder) {
  return `/work_order_label?name=${encodeURIComponent(workOrder)}`
}
