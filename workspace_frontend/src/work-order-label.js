export function workOrderLabelUrl(workOrder, extraLabels = 0) {
  const url = `/work_order_label?name=${encodeURIComponent(workOrder)}`
  return extraLabels > 0 ? `${url}&extra=${extraLabels}` : url
}
