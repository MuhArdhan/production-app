// Visible steps only; the active stage remains authoritative server data.
export function workOrderStages(wo) {
  return [
    'persiapan',
    ...(wo?.skipTransfer ? [] : ['material']),
    ...(wo?.hasOperations ? ['operasi'] : []),
    ...(wo?.skipPrepacking ? [] : ['prepacking']),
    'postpacking',
    'finish'
  ]
}
