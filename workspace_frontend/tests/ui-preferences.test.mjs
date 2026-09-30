import assert from 'node:assert/strict'
import test from 'node:test'

import {
  FONT_SCALE_DEFAULT,
  FONT_SCALE_MAX,
  FONT_SCALE_MIN,
  clampFontScale
} from '../src/ui-preferences.js'

test('konstanta batas sesuai kontrak FU70', () => {
  assert.equal(FONT_SCALE_MIN, 90)
  assert.equal(FONT_SCALE_MAX, 125)
  assert.equal(FONT_SCALE_DEFAULT, 100)
})

test('clamp membatasi ke rentang 90–125', () => {
  assert.equal(clampFontScale(50), 90)
  assert.equal(clampFontScale(300), 125)
  assert.equal(clampFontScale(110), 110)
  assert.equal(clampFontScale(89.4), 90)
  assert.equal(clampFontScale(125.6), 125)
})

test('nilai kotor jatuh ke default, bukan ke batas', () => {
  assert.equal(clampFontScale(null), 100)
  assert.equal(clampFontScale(undefined), 100)
  assert.equal(clampFontScale(''), 100)
  assert.equal(clampFontScale('bukan angka'), 100)
})

test('desimal dibulatkan ke bilangan bulat terdekat', () => {
  assert.equal(clampFontScale(107.4), 107)
  assert.equal(clampFontScale(106.5), 107)
})
