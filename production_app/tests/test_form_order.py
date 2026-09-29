# FO Form Order suite (TASKS.md section I, 2026-09-18)
#
# Proves by execution on the installed runtime:
# - upgrade.apply() is idempotent for the FO steps and lands the FO permission
#   matrix: Manufacturing User creates MRs (Form Order), Manufacturing Manager
#   gets full MR rights, Gudang Barang Jadi can create/submit Stock Entry to
#   fulfill; the marker custom_is_form_order + 2 Pengaturan fields exist;
# - create_form_order validates BEFORE any write (qty, item stock non-batch,
#   missing Pengaturan route = clear error, zero MR rows written);
# - form_order_list scoping: gudang/manager see all, plain produksi only own;
# - cancel_form_order: owner/manager only, refused once an SE exists;
# - fulfill_form_order: gudang only, moves stock once (duplicate refused
#   through the locking SE re-check), status derives to terkirim;
# - free-form FO MRs never touch the Work Order handover mirror (doc_events
#   no-op, no Error Log entries).
# - FU48a→FU57: the anti-orphan-MR guard Property Setter (Material Request
#   Item custom_work_order mandatory_depends_on) was RETIRED by FU57 (user
#   ruling 2026-09-20: native Desk MRs must never be forced through a Work
#   Order). The retirement is idempotent, leaves no meta/Property Setter
#   trace, and the two open paths stay open (create_form_order — parent
#   custom_is_form_order=1; MR of other types, e.g. Purchase, without a
#   Work Order). Desk-side evaluation was client-side by design
#   (verified frappe v16.33.1: mandatory_depends_on is JS-only).
#
# Test-only records carry the FO prefix; the Frappe test framework rolls each
# run back (class fixtures are purged in tearDownClass — apply() commits).

import frappe
from frappe.defaults import get_user_default, set_user_default
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, now, random_string

from production_app import upgrade
from production_app.api import form_order
from production_app.api.handover import SYNC_ERROR_TITLE, _roles
from production_app.api.work_order import warehouse_defaults, warehouse_defaults_save

PREFIX = "FO"
ROLE_GUDANG = "Gudang Barang Jadi"


def _company_and_group():
	name = frappe.db.get_value("Work Order", {"docstatus": 1}, "name", order_by="creation desc")
	company = frappe.db.get_value("Work Order", name, "company")
	parent = frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")
	return company, parent


def _make_user(local, role=None):
	"""role: satu nama atau list nama (FU64 persona gabungan)."""
	suffix = random_string(6).upper()
	user = (
		frappe.get_doc(
			{
				"doctype": "User",
				"email": f"fo.{local}.{suffix.lower()}@prodapp.example.com",
				"first_name": f"FO {local}",
				"send_welcome_email": 0,
			}
		)
		.insert()
	)
	if role:
		for name in role if isinstance(role, (list, tuple)) else [role]:
			user.append("roles", {"role": name})
		user.save()
	return user.name


class TestFormOrder(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company, cls.parent_wh = _company_and_group()
		suffix = random_string(6).upper()

		def wh(label):
			return (
				frappe.get_doc(
					{
						"doctype": "Warehouse",
						"warehouse_name": f"{PREFIX} {label} {suffix}",
						"company": cls.company,
						"parent_warehouse": cls.parent_wh,
						"is_group": 0,
					}
				)
				.insert()
				.name
			)

		cls.src_wh = wh("Source")
		cls.dst_wh = wh("Target")

		def item(label, extra=None):
			return (
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": f"{PREFIX}-{label}-{suffix}",
						"item_name": f"{PREFIX} {label} {suffix}",
						"item_group": frappe.db.get_value("Item Group", {}, "name"),
						"stock_uom": frappe.db.get_value(
							"Work Order", {"docstatus": 1}, "stock_uom", order_by="creation desc"
						),
						"is_stock_item": 1,
						"is_purchase_item": 0,
						"is_sales_item": 0,
						"is_fixed_asset": 0,
						**(extra or {}),
					}
				)
				.insert()
				.name
			)

		cls.item = item("Item")
		cls.item_batch = item("Batch", {"has_batch_no": 1, "create_new_batch": 0})
		cls.item_service = item("Service", {"is_stock_item": 0})

		# FU48c: pilihan satuan — item dengan konversi (enabled + disabled) dan
		# item dengan faktor 0/negatif; UOM fixture dibuang di tearDownClass.
		cls.uom_pack = frappe.get_doc(
			{"doctype": "UOM", "uom_name": f"{PREFIX} Pak {suffix}"}
		).insert().name
		cls.uom_gram = frappe.get_doc(
			{"doctype": "UOM", "uom_name": f"{PREFIX} Gram {suffix}"}
		).insert().name
		cls.uom_off = frappe.get_doc(
			{"doctype": "UOM", "uom_name": f"{PREFIX} Off {suffix}", "enabled": 0}
		).insert().name

		def item_with_uoms(label, rows):
			doc = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": f"{PREFIX}-{label}-{suffix}",
					"item_name": f"{PREFIX} {label} {suffix}",
					"item_group": frappe.db.get_value("Item Group", {}, "name"),
					"stock_uom": frappe.db.get_value(
						"Work Order", {"docstatus": 1}, "stock_uom", order_by="creation desc"
					),
					"is_stock_item": 1,
					"is_purchase_item": 0,
					"is_sales_item": 0,
					"is_fixed_asset": 0,
				}
			)
			for uom, factor in rows:
				doc.append("uoms", {"uom": uom, "conversion_factor": factor})
			return doc.insert().name

		cls.item_uom = item_with_uoms(
			"Uom", [(cls.uom_pack, 10), (cls.uom_off, 5)]
		)
		# faktor 0 tidak ikut; faktor negatif TIDAK BISA dibuat — field
		# conversion_factor native non_negative (ORM menolak, terbukti saat
		# fixture -1 ditolak NonNegativeError) → guard >0 di server tetap ada.
		cls.item_conv_bad = item_with_uoms(
			"ConvBad", [(cls.uom_pack, 0), (cls.uom_gram, 0)]
		)

	@classmethod
	def tearDownClass(cls):
		"""apply() commits mid-class (metadata) which persists the class
		fixtures past the framework rollback — purge them explicitly (they
		carry no ledger entries once the per-test receipts rolled back)."""
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for doctype, field, pattern in (
			("Warehouse", "warehouse_name", f"{PREFIX} %"),
			("Item", "item_code", f"{PREFIX}-%"),
			("Item", "item_name", f"{PREFIX} %"),  # site autoname recodes item_code -> ITEM#####
			("UOM", "uom_name", f"{PREFIX} %"),
		):
			for name in frappe.get_all(doctype, filters={field: ("like", pattern)}, pluck="name"):
				frappe.delete_doc(doctype, name, force=True)
		frappe.db.commit()
		super().tearDownClass()

	# ------------------------------------------------------------- helpers

	def _set_route(self):
		"""Pengaturan route utk fixture warehouses (in-transaction, rolled back)."""
		frappe.set_user("Administrator")
		try:
			warehouse_defaults_save(
				form_order_source_warehouse=self.src_wh,
				form_order_target_warehouse=self.dst_wh,
			)
		finally:
			frappe.set_user("Administrator")

	def _fo_count(self):
		return frappe.db.count("Material Request", {"custom_is_form_order": 1})

	def _receipt(self, qty=20):
		uom = frappe.db.get_value("Item", self.item, "stock_uom")
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{
						"item_code": self.item,
						"qty": qty,
						"uom": uom,
						"stock_uom": uom,
						"conversion_factor": 1,
						"basic_rate": 10,
						"t_warehouse": self.src_wh,
					}
				],
			}
		)
		se.insert()
		se.submit()
		return se

	def _bin_qty(self, warehouse):
		return frappe.db.get_value("Bin", {"item_code": self.item, "warehouse": warehouse}, "actual_qty") or 0

	# ------------------------------------------------------ idempotency

	def test_fo_apply_idempotent_and_matrix(self):
		r1 = upgrade.apply()
		r2 = upgrade.apply()
		for key in ("form_order_fields", "docperm_matrix", "mr_guard"):
			value = r2[key]
			if isinstance(value, str):
				self.assertEqual(value, "unchanged", f"{key} not idempotent: {value}")
				continue
			entries = value.values() if isinstance(value, dict) else value
			self.assertTrue(
				all(entry.endswith(": unchanged") for entry in entries),
				f"{key} not idempotent: {value}",
			)
		self.assertTrue(frappe.get_meta("Material Request").has_field("custom_is_form_order"))
		for fieldname in (
			"custom_default_form_order_source_warehouse",
			"custom_default_form_order_target_warehouse",
		):
			self.assertTrue(frappe.get_meta("Manufacturing Settings").has_field(fieldname))

		RIGHTS = ("read", "write", "create", "submit", "cancel", "amend", "delete", "if_owner")

		def flags(doctype, role):
			name = frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": role}, "name")
			if not name:
				return None
			row = frappe.db.get_value("Custom DocPerm", name, list(RIGHTS), as_dict=True)
			return {k: int(row.get(k) or 0) for k in RIGHTS}

		def flags_row(doctype, role):
			return flags(doctype, role)

		# FO raises Manufacturing User to the full MR lifecycle incl. delete
		# (FU64: delete=1 mengikuti konvensi native — semua role MR native
		# punya delete)
		self.assertEqual(
			flags("Material Request", "Manufacturing User"),
			{"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "amend": 0, "delete": 1, "if_owner": 0},
		)
		# Manufacturing Manager: full MR sama dengan Manufacturing User
		self.assertEqual(
			flags("Material Request", "Manufacturing Manager"),
			{"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "amend": 0, "delete": 1, "if_owner": 0},
		)
		# FU64: Stock Entry & Material Request Item TANPA Custom DocPerm —
		# SE kembali ke set native 4 role; MR Item child sepenuhnya
		# parent-governed (baris custom lama tidak pernah berpengaruh).
		self.assertIsNone(flags_row("Stock Entry", ROLE_GUDANG))
		self.assertIsNone(flags_row("Material Request Item", ROLE_GUDANG))
		self.assertIsNone(flags_row("Material Request Item", "Manufacturing User"))
		self.assertIsNone(flags_row("Material Request Item", "Manufacturing Manager"))
		# FU48c: gudang menghapus DRAFT MR-nya di Desk. if_owner sengaja 0 —
		# terbukti merusak scope baca gudang (flag berlaku satu baris penuh;
		# lihat komentar DOCPERM_MATRIX di upgrade.py)
		self.assertEqual(
			flags("Material Request", ROLE_GUDANG),
			{"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "amend": 1, "delete": 1, "if_owner": 0},
		)
		self.assertTrue(frappe.get_meta("Material Request").get_field("custom_is_form_order").read_only)

	def test_fo_settings_roundtrip_and_role_flag(self):
		defaults = warehouse_defaults()
		self.assertIn("form_order_source_warehouse", defaults)
		self.assertIn("form_order_target_warehouse", defaults)

		frappe.set_user("Administrator")
		try:
			saved = warehouse_defaults_save(
				form_order_source_warehouse=self.src_wh,
				form_order_target_warehouse=self.dst_wh,
			)
			self.assertEqual(saved["form_order_source_warehouse"], self.src_wh)
			self.assertEqual(saved["form_order_target_warehouse"], self.dst_wh)
			self.assertEqual(
				frappe.db.get_single_value(
					"Manufacturing Settings", "custom_default_form_order_source_warehouse"
				),
				self.src_wh,
			)
		finally:
			frappe.set_user("Administrator")

		bare = _make_user("bare", "Manufacturing User")
		frappe.set_user(bare)
		try:
			self.assertFalse(_roles()["is_manajer_produksi"])  # flag baru: tidak melebar
			self.assertTrue(_roles()["is_produksi"])
			with self.assertRaises(frappe.PermissionError):
				warehouse_defaults_save(form_order_source_warehouse=self.src_wh)
		finally:
			frappe.set_user("Administrator")
		manager = _make_user("mgr", "Manufacturing Manager")
		frappe.set_user(manager)
		try:
			self.assertTrue(_roles()["is_manajer_produksi"])
			self.assertFalse(_roles()["is_produksi"])  # tidak otomatis produksi
		finally:
			frappe.set_user("Administrator")

	# ------------------------------------------- create: zero-write contract

	def test_fo_item_info(self):
		"""FO-8: info tampilan grid — nama/satuan/penanda; tanpa izin baca Item
		→ None (bukan error); item tak dikenal → None."""
		info = form_order.item_info(self.item)
		self.assertTrue(info.item_name.startswith("FO Item"))
		self.assertTrue(info.stock_uom)
		self.assertEqual(int(info.is_stock_item), 1)
		self.assertEqual(int(info.has_batch_no), 0)
		self.assertEqual(int(form_order.item_info(self.item_batch).has_batch_no), 1)
		self.assertIsNone(form_order.item_info("TIDAK-ADA-ITEM"))

		bare = _make_user("bareinfo")
		frappe.set_user(bare)
		try:
			self.assertIsNone(form_order.item_info(self.item))  # tanpa Item read
		finally:
			frappe.set_user("Administrator")

	def test_fo_create_validations_write_nothing(self):
		self._set_route()
		prod = _make_user("prod", "Manufacturing User")
		frappe.set_user(prod)
		try:
			before = self._fo_count()

			def expect_throw(**kwargs):
				with self.assertRaises(frappe.ValidationError):
					form_order.create_form_order(**kwargs)
				self.assertEqual(self._fo_count(), before)  # zero-write

			expect_throw(items=[])  # minimal satu baris
			expect_throw(items=[{"item_code": self.item, "qty": 0}])
			expect_throw(items=[{"item_code": self.item, "qty": -5}])
			expect_throw(items=[{"item_code": self.item, "qty": "abc"}])
			expect_throw(items=[{"item_code": "TIDAK-ADA", "qty": 1}])
			expect_throw(items=[{"item_code": self.item_service, "qty": 1}])  # bukan item stok
			expect_throw(items=[{"item_code": self.item_batch, "qty": 1}])  # ber-batch v1
			expect_throw(items=[{"item_code": self.item, "qty": 1}], schedule_date="bukan-tanggal")

			# rute Pengaturan kosong = error jelas, tetap zero-write
			frappe.db.set_single_value(
				"Manufacturing Settings", "custom_default_form_order_source_warehouse", None
			)
			with self.assertRaises(frappe.ValidationError):
				form_order.create_form_order(items=[{"item_code": self.item, "qty": 1}])
			self.assertEqual(self._fo_count(), before)
		finally:
			frappe.set_user("Administrator")

	def test_fo_gudang_cannot_create_produksi_cannot_fulfill(self):
		self._set_route()
		gudang = _make_user("gd", [ROLE_GUDANG, "Stock User"])
		frappe.set_user(gudang)
		try:
			with self.assertRaises(frappe.PermissionError):
				form_order.create_form_order(items=[{"item_code": self.item, "qty": 1}])
		finally:
			frappe.set_user("Administrator")

	# ------------------------------------- create + list scope + sync no-op

	def test_fo_create_list_scope_and_mirror_noop(self):
		self._set_route()
		prod = _make_user("prod", "Manufacturing User")
		prod2 = _make_user("prod2", "Manufacturing User")
		manager = _make_user("mgr", "Manufacturing Manager")
		gudang = _make_user("gd", [ROLE_GUDANG, "Stock User"])

		err_before = frappe.db.count("Error Log", {"method": SYNC_ERROR_TITLE})
		frappe.set_user(prod)
		try:
			res = form_order.create_form_order(
				items=[{"item_code": self.item, "qty": 12}], note="  uji FO  "
			)
			mr_name = res["material_request"]
		finally:
			frappe.set_user("Administrator")

		mr = frappe.get_doc("Material Request", mr_name)
		self.assertEqual(mr.docstatus, 1)
		self.assertEqual(mr.material_request_type, "Material Transfer")
		self.assertEqual(mr.custom_is_form_order, 1)
		self.assertEqual(mr.custom_note, "uji FO")
		self.assertEqual(mr.set_from_warehouse, self.src_wh)
		self.assertEqual(mr.set_warehouse, self.dst_wh)
		# default besok (log_error-style: seluruh class SATU transaksi — jangan
		# andalkan tanggal bergeser antar test)
		self.assertEqual(str(mr.schedule_date), str(getdate(add_days(now(), 1))))
		for row in mr.items:  # free-form: TIDAK pernah terikat Work Order
			self.assertFalse(row.custom_work_order)
		self.assertIn(mr.company, (self.company,))  # company dari warehouse asal

		def list_as(user):
			frappe.set_user(user)
			try:
				return form_order.form_order_list()["orders"]
			finally:
				frappe.set_user("Administrator")

		for user in (prod, manager, gudang):
			rows = list_as(user)
			hit = [o for o in rows if o["mr"] == mr_name]
			self.assertEqual(len(hit), 1, f"{user} harus melihat {mr_name}")
			self.assertEqual(hit[0]["status"], "menunggu")
		self.assertNotIn(mr_name, [o["mr"] for o in list_as(prod2)])  # produksi lain: hanya miliknya

		# mirror serah terima tidak tersentuh MR free-form (doc_events no-op)
		self.assertEqual(frappe.db.count("Error Log", {"method": SYNC_ERROR_TITLE}), err_before)

		# pembuat membatalkan request yang belum diproses
		frappe.set_user(prod)
		try:
			form_order.cancel_form_order(mr_name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Material Request", mr_name, "docstatus"), 2)
		rows = list_as(prod)
		self.assertEqual([o["status"] for o in rows if o["mr"] == mr_name], ["batal"])
		self.assertEqual(frappe.db.count("Error Log", {"method": SYNC_ERROR_TITLE}), err_before)

	def test_fo_cancel_guards_owner_and_processed(self):
		self._set_route()
		prod = _make_user("prod", "Manufacturing User")
		prod2 = _make_user("prod2", "Manufacturing User")
		manager = _make_user("mgr", "Manufacturing Manager")

		frappe.set_user(prod)
		try:
			mr_name = form_order.create_form_order(
				items=[{"item_code": self.item, "qty": 3}]
			)["material_request"]
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(prod2)  # produksi lain: tolak (bukan owner, bukan manager)
		try:
			with self.assertRaises(frappe.PermissionError):
				form_order.cancel_form_order(mr_name)
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(manager)  # manager boleh
		try:
			form_order.cancel_form_order(mr_name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Material Request", mr_name, "docstatus"), 2)

		# sudah diproses (ada SE) → tolak walau owner
		frappe.set_user(prod)
		try:
			mr_name = form_order.create_form_order(
				items=[{"item_code": self.item, "qty": 2}]
			)["material_request"]
		finally:
			frappe.set_user("Administrator")
		self._receipt(qty=5)
		gudang = _make_user("gd", [ROLE_GUDANG, "Stock User"])
		frappe.set_user(gudang)
		try:
			form_order.fulfill_form_order(mr_name)
		finally:
			frappe.set_user("Administrator")
		frappe.set_user(prod)
		try:
			with self.assertRaises(frappe.ValidationError):
				form_order.cancel_form_order(mr_name)
		finally:
			frappe.set_user("Administrator")

	def test_fo_fulfill_moves_stock_once(self):
		self._set_route()
		prod = _make_user("prod", "Manufacturing User")
		gudang = _make_user("gd", [ROLE_GUDANG, "Stock User"])

		# seluruh class berbagi SATU transaksi (pola suite handover) — bin
		# ditegaskan DELTA terhadap kondisi awal test ini, bukan nilai absolut
		src_before = self._bin_qty(self.src_wh)
		dst_before = self._bin_qty(self.dst_wh)
		self._receipt(qty=20)
		frappe.set_user(prod)
		try:
			mr_name = form_order.create_form_order(
				items=[{"item_code": self.item, "qty": 15}], schedule_date="2026-10-01"
			)["material_request"]
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(prod)  # produksi tidak bisa memproses sendiri
		try:
			with self.assertRaises(frappe.PermissionError):
				form_order.fulfill_form_order(mr_name)
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(gudang)
		try:
			res = form_order.fulfill_form_order(mr_name)
			se_name = res["stock_entry"]
			# anti-dobel: SE yang sudah ada (recheck di bawah lock) menolak ulang
			with self.assertRaises(frappe.ValidationError):
				form_order.fulfill_form_order(mr_name)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(frappe.db.get_value("Stock Entry", se_name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Stock Entry", se_name, "stock_entry_type"), "Material Transfer")
		self.assertEqual(self._bin_qty(self.src_wh) - src_before, 5)  # 20 - 15
		self.assertEqual(self._bin_qty(self.dst_wh) - dst_before, 15)

		frappe.set_user(prod)
		try:
			rows = form_order.form_order_list()["orders"]
		finally:
			frappe.set_user("Administrator")
		hit = [o for o in rows if o["mr"] == mr_name]
		self.assertEqual(hit[0]["status"], "terkirim")
		self.assertEqual(hit[0]["stock_entry"], se_name)
		self.assertTrue(hit[0]["sent_at"])

	# ------------------------------------------------------------ FU48c satuan

	def test_fo_valid_uoms(self):
		"""Pilihan satuan dari UOM Conversion Detail: faktor finite > 0, hanya
		UOM enabled; item tanpa konversi aktif hanya punya stock UOM."""
		valid = {v["uom"]: float(v["conversion_factor"]) for v in form_order._valid_uoms(self.item_uom)}
		self.assertEqual(valid[self.uom_pack], 10.0)
		self.assertIn(frappe.db.get_value("Item", self.item_uom, "stock_uom"), valid)
		self.assertNotIn(self.uom_off, valid)  # UOM disabled difilter

		# faktor 0 / negatif tidak ikut (hanya sisa baris stock_uom faktor 1)
		bad = {v["uom"] for v in form_order._valid_uoms(self.item_conv_bad)}
		self.assertNotIn(self.uom_pack, bad)
		self.assertNotIn(self.uom_gram, bad)
		stock = frappe.db.get_value("Item", self.item_conv_bad, "stock_uom")
		self.assertEqual(bad, {stock})

		# tanpa baris konversi sama sekali (item tak dikenal) → kosong
		self.assertEqual(form_order._valid_uoms("TIDAK-ADA-ITEM"), [])

	def test_fo_item_info_uoms_and_last_uom(self):
		"""item_info membawa uoms + last_uom (valid); last_uom basi → null."""
		info = form_order.item_info(self.item_uom)
		self.assertIn(self.uom_pack, [v["uom"] for v in info["uoms"]])
		self.assertIsNone(info["last_uom"])  # belum ada default user

		user = _make_user("uominfo", "Manufacturing User")
		stock = frappe.db.get_value("Item", self.item_uom, "stock_uom")
		frappe.set_user(user)
		try:
			set_user_default(form_order.UOM_DEFAULT_KEY, frappe.as_json({self.item_uom: self.uom_pack}))
			self.assertEqual(form_order.item_info(self.item_uom)["last_uom"], self.uom_pack)
			# basi: tersimpan tapi bukan lagi pilihan item → null
			set_user_default(
				form_order.UOM_DEFAULT_KEY, frappe.as_json({self.item_uom: "SATUAN-PALING-TAK-ADA"})
			)
			self.assertIsNone(form_order.item_info(self.item_uom)["last_uom"])
			# stock_uom selalu anggota pilihan → valid
			set_user_default(form_order.UOM_DEFAULT_KEY, frappe.as_json({self.item_uom: stock}))
			self.assertEqual(form_order.item_info(self.item_uom)["last_uom"], stock)
		finally:
			frappe.set_user("Administrator")

	def test_fo_create_alternative_uom(self):
		"""Satuan alternatif: baris MR benar-benar membawa uom terpilih, faktor
		baris, stock_qty = qty × faktor; uom tak dikenal/disabled → tolak,
		zero-write."""
		self._set_route()
		prod = _make_user("produom", "Manufacturing User")
		stock_uom = frappe.db.get_value("Item", self.item_uom, "stock_uom")
		frappe.set_user(prod)
		try:
			res = form_order.create_form_order(
				items=[{"item_code": self.item_uom, "qty": 2, "uom": self.uom_pack}]
			)
			row = frappe.get_value(
				"Material Request Item",
				{"parent": res["material_request"]},
				["qty", "uom", "stock_uom", "conversion_factor", "stock_qty"],
				as_dict=True,
			)
			self.assertEqual(row.uom, self.uom_pack)
			self.assertEqual(row.stock_uom, stock_uom)
			self.assertEqual(float(row.conversion_factor), 10.0)
			self.assertEqual(float(row.qty), 2.0)
			self.assertEqual(float(row.stock_qty), 20.0)
			# peta last_uom pembuat terisi satuan terpilih
			prefs = frappe.parse_json(get_user_default(form_order.UOM_DEFAULT_KEY) or "{}") or {}
			self.assertEqual(prefs.get(self.item_uom), self.uom_pack)

			before = self._fo_count()
			for bad in ("SATUAN-PALING-TAK-ADA", self.uom_off):  # disabled = tak valid
				with self.assertRaises(frappe.ValidationError):
					form_order.create_form_order(items=[{"item_code": self.item_uom, "qty": 1, "uom": bad}])
				self.assertEqual(self._fo_count(), before)  # zero-write
		finally:
			frappe.set_user("Administrator")

	def test_fo_create_without_uom_regression(self):
		"""Tanpa `uom` di payload → perilaku lama: stock_uom, faktor 1."""
		self._set_route()
		prod = _make_user("prodnouom", "Manufacturing User")
		stock_uom = frappe.db.get_value("Item", self.item_uom, "stock_uom")
		frappe.set_user(prod)
		try:
			res = form_order.create_form_order(items=[{"item_code": self.item_uom, "qty": 3}])
			row = frappe.get_value(
				"Material Request Item",
				{"parent": res["material_request"]},
				["qty", "uom", "stock_uom", "conversion_factor", "stock_qty"],
				as_dict=True,
			)
			self.assertEqual(row.uom, stock_uom)
			self.assertEqual(row.stock_uom, stock_uom)
			self.assertEqual(float(row.conversion_factor), 1.0)
			self.assertEqual(float(row.stock_qty), 3.0)
		finally:
			frappe.set_user("Administrator")

	def test_fo_last_uom_map_per_user_and_cap(self):
		"""Peta last_uom per user: A terisi, B tak terpengaruh; cap 200 — entri
		tertua (urutan sisip pertama) dibuang."""
		self._set_route()
		user_a = _make_user("uoma", "Manufacturing User")
		user_b = _make_user("uomb", "Manufacturing User")
		key = form_order.UOM_DEFAULT_KEY

		frappe.set_user(user_a)
		try:
			form_order.create_form_order(items=[{"item_code": self.item_uom, "qty": 1, "uom": self.uom_pack}])
			prefs = frappe.parse_json(get_user_default(key) or "{}") or {}
			self.assertEqual(prefs.get(self.item_uom), self.uom_pack)
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(user_b)
		try:
			self.assertEqual(frappe.parse_json(get_user_default(key) or "{}") or {}, {})
		finally:
			frappe.set_user("Administrator")

		# cap 200: 201 entri → sisipan baru masuk, entri posisi pertama terbuang
		frappe.set_user(user_a)
		try:
			set_user_default(key, frappe.as_json({f"ITEM-{i}": "U" for i in range(200)}))
			form_order._remember_last_uoms([{"item_code": "ITEM-BARU", "uom": "U"}])
			prefs = frappe.parse_json(get_user_default(key) or "{}") or {}
			self.assertEqual(len(prefs), 200)
			self.assertNotIn("ITEM-0", prefs)
			self.assertIn("ITEM-BARU", prefs)
		finally:
			frappe.set_user("Administrator")

	# -------------------------------------------- FU48a anti-orphan MR guard
	# Named fo_z48a so these run LAST: the apply() calls in test_fo_apply...
	# (first test, before any MR exists) commit the class transaction, and
	# anything created earlier would be persisted with them — a Purchase MR
	# there leaves a committed Bin.ordered_qty that blocks the warehouse purge.

	def test_fo_z57_mr_guard_retired(self):
		"""FU57: the FU48a guard is RETIRED — native Desk MRs must never be
		forced through a Work Order. Meta carries no mandatory_depends_on and
		no Property Setter row remains (retirement idempotency itself is
		asserted in test_fo_apply... via the 'mr_guard' key)."""
		df = frappe.get_meta("Material Request Item").get_field("custom_work_order")
		self.assertIsNotNone(df)
		self.assertFalse(df.mandatory_depends_on)
		self.assertFalse(
			frappe.db.get_value(
				"Property Setter",
				{
					"doc_type": "Material Request Item",
					"field_name": "custom_work_order",
					"property": "mandatory_depends_on",
				},
				"name",
			)
		)
		# retirement is idempotent: apply() converges to "unchanged"
		self.assertEqual(upgrade.retire_mr_guard(), "unchanged")

	def test_fo_z48a_guard_leaves_form_order_and_other_types_open(self):
		"""Post-FU57 (guard retired) the open paths stay open: create_form_order
		succeeds with rows carrying no work order (custom_is_form_order=1) and a
		sibling MR of another type without custom_work_order submits untouched."""
		self._set_route()
		prod = _make_user("prod48a", "Manufacturing User")
		frappe.set_user(prod)
		try:
			# (b) produksi path: FO MR submits with rows that carry NO work order
			mr_name = form_order.create_form_order(
				items=[{"item_code": self.item, "qty": 4}]
			)["material_request"]
			mr = frappe.get_doc("Material Request", mr_name)
			self.assertEqual(mr.docstatus, 1)
			self.assertEqual(mr.material_request_type, "Material Transfer")
			self.assertEqual(mr.custom_is_form_order, 1)
			for row in mr.items:
				self.assertFalse(row.custom_work_order)
		finally:
			frappe.set_user("Administrator")

		# (d) other MR type: Purchase without a Work Order is not guard scope
		purchase = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Purchase",
				"company": self.company,
				"transaction_date": now(),
				"schedule_date": add_days(now(), 1),
				"items": [
					{
						"item_code": self.item,
						"qty": 2,
						"uom": frappe.db.get_value("Item", self.item, "stock_uom"),
						"warehouse": self.dst_wh,
						"schedule_date": add_days(now(), 1),
					}
				],
			}
		)
		purchase.insert()
		purchase.submit()
		self.assertEqual(purchase.docstatus, 1)
		for row in purchase.items:
			self.assertFalse(row.custom_work_order)
