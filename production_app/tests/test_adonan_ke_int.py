# FU69 — custom_adonan_ke: Data → Int
#
# "Adonan ke" semantiknya nomor urut: input SPA sudah type=number min=1 dan
# frontend memvalidasi >= 1, sedangkan storage masih Data (varchar 140).
# Storage diganti Int lewat reconcile idempoten (update Custom Field +
# updatedb alter kolom + clear cache) — saat perubahan, produksi 8081 punya
# 0 WO dan 8082 kosong semua, jadi tidak ada nilai yang perlu dikonversi.
# Kosong (0) dinormalkan ke None/'' di sumber lot dan kartu papan supaya UI
# tidak menampilkan "Adonan ke 0".

import frappe
from frappe.tests import IntegrationTestCase

DOCTYPE = "Work Order"
FIELDNAME = "custom_adonan_ke"


class TestAdonanKeInt(IntegrationTestCase):
	def test_reconcile_converts_data_to_int_idempotent(self):
		from production_app.upgrade import reconcile_adonan_ke_int

		# paksa kondisi pra-migrasi (situs lama masih Data)
		frappe.db.set_value(
			"Custom Field", {"dt": DOCTYPE, "fieldname": FIELDNAME}, "fieldtype", "Data"
		)
		frappe.clear_cache(doctype=DOCTYPE)
		self.assertEqual(
			frappe.db.get_value("Custom Field", {"dt": DOCTYPE, "fieldname": FIELDNAME}, "fieldtype"),
			"Data",
		)

		out = reconcile_adonan_ke_int()
		self.assertEqual(out[FIELDNAME], "Data -> Int")
		self.assertEqual(
			frappe.db.get_value("Custom Field", {"dt": DOCTYPE, "fieldname": FIELDNAME}, "fieldtype"),
			"Int",
		)
		self.assertEqual(frappe.get_meta(DOCTYPE).get_field(FIELDNAME).fieldtype, "Int")
		col = frappe.db.sql(
			f"SHOW COLUMNS FROM `tab{DOCTYPE}` LIKE '{FIELDNAME}'", as_dict=True
		)[0].Type
		self.assertIn("int", col)

		# idempoten: run kedua tidak mengubah apa pun
		self.assertEqual(reconcile_adonan_ke_int()[FIELDNAME], "unchanged")

	def test_validate_prep_adonan_ke_int_min_one(self):
		from production_app.api.work_order import _validate_prep_values

		cleaned = _validate_prep_values({"adonan_ke": "3"})
		self.assertEqual(cleaned[FIELDNAME], 3)
		self.assertIsInstance(cleaned[FIELDNAME], int)
		with self.assertRaises(frappe.ValidationError):
			_validate_prep_values({"adonan_ke": "0"})
		with self.assertRaises(frappe.ValidationError):
			_validate_prep_values({"adonan_ke": "-2"})
