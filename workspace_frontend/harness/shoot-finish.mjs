// DEV-ONLY: focused shot of the Finish panel (StageFinish markup) at real width.
import { mkdirSync } from 'node:fs'
const pw = await import(process.env.PW_PATH || 'playwright')
const chromium = (pw.chromium || pw.default?.chromium)
const OUT = new URL('./shots/', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })
const PAGE_URL = 'http://localhost:5199/assets/production_app/workspace/layout-harness.html'
const browser = await chromium.launch()
for (const w of [1400, 900]) {
  const page = await browser.newPage({ viewport: { width: w, height: 1400 }, deviceScaleFactor: 2 })
  await page.goto(PAGE_URL, { waitUntil: 'networkidle' })
  await page.waitForSelector('.fin-kpis', { timeout: 15000 })
  await page.waitForTimeout(300)
  const panel = page.locator('.panel').last()
  await panel.screenshot({ path: `${OUT}finish-${w}.png` })
  // report block widths + label/value gap for the first detail row
  const geo = await page.evaluate(() => {
    const row = document.querySelector('.fin-row')
    const k = row.querySelector('.k').getBoundingClientRect()
    const v = row.querySelector('.v').getBoundingClientRect()
    const block = document.querySelector('.fin-block').getBoundingClientRect()
    const kpi = document.querySelector('.fin-kpi').getBoundingClientRect()
    return { gapLabelToValue: Math.round(v.left - k.right), blockWidth: Math.round(block.width), kpiWidth: Math.round(kpi.width) }
  })
  console.log(`width ${w}:`, JSON.stringify(geo))
  await page.close()
}
await browser.close()
console.log('finish shots written')
