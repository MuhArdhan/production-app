import test from 'node:test'
import assert from 'node:assert/strict'
import qz from 'qz-tray'
import { connectQz } from '../src/qz-tray.js'

test('QZ uses site certificate and server SHA512 signing for selected Work Order', async () => {
  let certificateCallback
  let signatureCallback
  let algorithm
  const original = {
    certificate: qz.security.setCertificatePromise,
    signature: qz.security.setSignaturePromise,
    algorithm: qz.security.setSignatureAlgorithm,
    active: qz.websocket.isActive,
    fetch: globalThis.fetch,
    window: globalThis.window
  }
  const calls = []
  try {
    qz.security.setCertificatePromise = fn => { certificateCallback = fn }
    qz.security.setSignaturePromise = fn => { signatureCallback = fn }
    qz.security.setSignatureAlgorithm = value => { algorithm = value }
    qz.websocket.isActive = () => true
    globalThis.window = { csrf_token: 'csrf-test' }
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options })
      return { ok: true, json: async () => ({ message: calls.length === 1 ? 'PUBLIC CERT' : 'BASE64 SIGNATURE' }) }
    }

    await connectQz('WO / 1')
    assert.equal(algorithm, 'SHA512')
    assert.equal(await certificateCallback(), 'PUBLIC CERT')
    assert.equal(await signatureCallback('a'.repeat(64)), 'BASE64 SIGNATURE')
    assert.match(calls[0].url, /certificate\?work_order=WO%20%2F%201$/)
    assert.equal(calls[1].url, '/api/method/production_app.api.qz_signing.sign')
    assert.equal(calls[1].options.method, 'POST')
    assert.equal(calls[1].options.headers['X-Frappe-CSRF-Token'], 'csrf-test')
    assert.deepEqual(JSON.parse(calls[1].options.body), { work_order: 'WO / 1', request: 'a'.repeat(64) })
  } finally {
    qz.security.setCertificatePromise = original.certificate
    qz.security.setSignaturePromise = original.signature
    qz.security.setSignatureAlgorithm = original.algorithm
    qz.websocket.isActive = original.active
    globalThis.fetch = original.fetch
    globalThis.window = original.window
  }
})
