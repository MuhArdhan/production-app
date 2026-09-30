# FU70 — preferensi tampilan per-user: ukuran font (skala %)
#
# Pengaturan font size harus per-user (bukan global Manufacturing Settings)
# dan berbatas — di luar 90–125% layout bisa pecah. Endpoint mengikuti pola
# list_preferences (get_user_default/set_user_default, tanpa gate role: yang
# disimpan hanya preferensi pemanggil sendiri).

import frappe
from frappe.tests import IntegrationTestCase


class TestUiPreferences(IntegrationTestCase):
	def test_default_and_roundtrip(self):
		from production_app.api.work_order import ui_preferences, ui_preferences_save

		# user sementara: default 100, tersimpan per-user (rollback per class)
		user = frappe.get_doc(
			{"doctype": "User", "email": "testfu70@prodapp.example.com", "first_name": "FU70"}
		).insert().name
		frappe.set_user(user)
		try:
			self.assertEqual(ui_preferences(), {"font_scale": 100})
			self.assertEqual(ui_preferences_save(110), {"font_scale": 110})
			self.assertEqual(ui_preferences(), {"font_scale": 110})
		finally:
			frappe.set_user("Administrator")

	def test_reject_out_of_bounds(self):
		from production_app.api.work_order import ui_preferences_save

		with self.assertRaises(frappe.ValidationError):
			ui_preferences_save(89)
		with self.assertRaises(frappe.ValidationError):
			ui_preferences_save(126)
		with self.assertRaises(frappe.ValidationError):
			ui_preferences_save("bukan angka")
		# batas inklusif diterima
		self.assertEqual(ui_preferences_save(90), {"font_scale": 90})
		self.assertEqual(ui_preferences_save(125), {"font_scale": 125})
