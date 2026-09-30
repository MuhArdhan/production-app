# FU67 — guest deep-link: redirect login harus mempertahankan hash URL
#
# Fragment URL (#/wo/<id>) tidak pernah terkirim ke server (spesifikasi HTTP).
# Guard guest lama me-redirect server-side ke "/login?redirect-to=<path>"
# sehingga hash selalu hilang — setelah login user mendarat di daftar, bukan
# di Work Order yang ditautkan (laporan user: "copy URL WO ke tab lain,
# WO hilang"). Perbaikan: cabang guest me-render interstitial kecil yang
# me-redirect dari sisi klien dengan encodeURIComponent(path + hash).
#
# Tanpa record database; IntegrationTestCase dipakai untuk konsistensi suit lain.

import os

import frappe
from frappe.tests import IntegrationTestCase


def _template_source():
	import production_app

	path = os.path.join(
		os.path.dirname(production_app.__file__), "www", "production_workspace.html"
	)
	with open(path, encoding="utf-8") as fh:
		return fh.read()


class TestGuestDeepLink(IntegrationTestCase):
	def test_guest_gets_interstitial_context_not_server_redirect(self):
		from production_app.www.production_workspace import get_context

		frappe.set_user("Guest")
		try:
			context = frappe._dict()
			try:
				get_context(context)
			except frappe.Redirect:
				self.fail(
					"guard guest masih redirect server-side — fragment #/wo/<id> pasti hilang lewat login"
				)
			self.assertTrue(context.get("guest_login_redirect"))
			self.assertTrue(context.get("no_cache"))
		finally:
			frappe.set_user("Administrator")

	def test_interstitial_redirect_carries_path_and_hash(self):
		try:
			html = frappe.get_jenv().from_string(_template_source()).render(
				{"guest_login_redirect": True}
			)
		except Exception as e:
			self.fail(f"template belum punya cabang interstitial guest (render gagal: {type(e).__name__})")
		# mekanisme yang terbukti di browser: redirect-to ter-encode memuat hash
		self.assertIn("encodeURIComponent", html)
		self.assertIn("location.pathname", html)
		self.assertIn("location.hash", html)
		# shell SPA tidak ikut ter-render untuk guest
		self.assertNotIn('id="app"', html)

	def test_logged_in_still_renders_full_shell(self):
		html = frappe.get_jenv().from_string(_template_source()).render(
			{"guest_login_redirect": False, "csrf_token": "tok", "workspace_user": "Pengguna"}
		)
		self.assertIn('id="app"', html)
		self.assertNotIn("location.hash", html)
