// FU70: ukuran font per-user — skala % yang di-clamp agar layout tak pecah.
// CSS app berbasis px, jadi skala diterapkan sebagai zoom pada #app:
// seluruh tampilan membesar/mengecil proporsional (setara zoom browser),
// bukan mengubah ukuran font satuan-satuan.
export const FONT_SCALE_MIN = 90
export const FONT_SCALE_MAX = 125
export const FONT_SCALE_DEFAULT = 100

export function clampFontScale(value) {
  if (value === null || value === undefined || value === '') return FONT_SCALE_DEFAULT
  const n = Math.round(Number(value))
  if (!Number.isFinite(n)) return FONT_SCALE_DEFAULT
  return Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, n))
}

export function applyFontScale(scale) {
  const s = clampFontScale(scale)
  if (typeof document !== 'undefined') {
    const target = document.getElementById('app') || document.body
    target.style.zoom = String(s / 100)
  }
  return s
}
