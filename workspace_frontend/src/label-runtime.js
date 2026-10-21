// Runtime boundary only: no label components, quantities or printer logic here.
export async function loadLabelRuntime() {
  const pending = []
  const collect = event => pending.push(event.detail)
  window.addEventListener('production:packing-saved', collect)
  try {
    const base = '/assets/product_qr/wo_labels/'
    const response = await fetch(`${base}runtime-version.json`, { cache: 'no-store' })
    if (!response.ok) throw new Error('Product QR runtime manifest unavailable')
    const { version } = await response.json()
    if (!version || typeof version !== 'string') throw new Error('Invalid Product QR runtime version')
    const url = `${base}runtime.js?v=${encodeURIComponent(version)}`
    const runtime = await import(/* @vite-ignore */ url)
    runtime.start()
    window.removeEventListener('production:packing-saved', collect)
    for (const detail of pending) {
      window.dispatchEvent(new CustomEvent('production:packing-saved', { detail }))
    }
  } catch (error) {
    window.removeEventListener('production:packing-saved', collect)
    // Loading an optional label app must not disable production operations.
    const notice = document.createElement('p')
    notice.className = 'err'
    notice.setAttribute('role', 'status')
    notice.textContent = 'Fitur label Product QR gagal dimuat. Muat ulang halaman untuk mencoba lagi.'
    document.body.appendChild(notice)
    console.warn('Product QR runtime unavailable', error)
  }
}
