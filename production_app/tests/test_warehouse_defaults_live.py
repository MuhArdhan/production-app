# FU58/FU61 — warehouse defaults LIVE: propagasi ke WO berjalan (gate company
# remediasi (docs/superpowers/specs/2026-09-20-fu58-live-warehouse-settings-design.md §10.1).
#
# 16 skenario dieksekusi pada runtime terpasang. Fixtures ber-prefix FU58;
# IntegrationTestCase v16 = SATU TRANSAKSI PER CLASS — tiap test membangun
# state settings-nya sendiri dulu (baseline save menyerap transisi apa pun)
# dan hanya meng-assert terhadap nama WO fixture-nya sendiri. Skenario yang
# butuh asersi ketat (daftar kosong) memakai rantai company kedua (OUTLET
# TRAINING) sehingga scan company-gated hanya melihat fixture-nya sendiri.
# Semua tulisan ke data live bersifat in-transaction dan di-rollback framework.

import unittest.mock as mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now, random_string

from erpnext.manufacturing.doctype.work_order.work_order import (
	make_stock_entry as make_wo_stock_entry,
)

from production_app.api.work_order import (
	_apply_warehouse_changes,
	prepare,
	sync_warehouse_defaults_to_running_work_orders,
	warehouse_defaults,
	warehouse_defaults_save,
)

DOCTYPE = "Work Order"
PREFIX = "FU58"


def _existing_wo_config():
	"""Read-only: konfigurasi company/gudang dari WO submitted yang sudah ada."""
	name = frappe.db.get_value(
		"Work Order",
		{"docstatus": 1, "fg_warehouse": ("is", "set"), "wip_warehouse": ("is", "set")},
		"name",
		order_by="creation desc",
	)
	if not name:
		frappe.throw("No submitted Work Order exists to derive a test configuration from")
	return frappe.db.get_value(
		"Work Order",
		name,
		["company", "fg_warehouse", "wip_warehouse", "stock_uom"],
		as_dict=True,
	)


def _make_item(code, group, uom):
	if frappe.db.exists("Item", code):
		frappe.delete_doc("Item", code, force=True)
	return (
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": group,
				"stock_uom": uom,
				"is_stock_item": 1,
				"is_purchase_item": 0,
				"is_sales_item": 0,
				"standard_rate": 100,
				"is_fixed_asset": 0,
				"opening_stock": 0,
			}
		)
		.insert()
		.name
	)


def _make_warehouse(name, company=None, parent=None):
	return (
		frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": name,
				"company": company,
				"parent_warehouse": parent,
				"is_group": 0,
			}
		)
		.insert()
		.name
	)


def _make_user(local):
	suffix = random_string(6).upper()
	user = (
		frappe.get_doc(
			{
				"doctype": "User",
				"email": f"fu58.{local}.{suffix.lower()}@prodapp.example.com",
				"first_name": f"FU58 {local}",
				"send_welcome_email": 0,
			}
		)
		.insert()
	)
	return user.name


class TestWarehouseDefaultsLive(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cfg = _existing_wo_config()
		cls.company = cfg.company
		cls.wip_wh = cfg.wip_warehouse
		cls.fg_wh = cfg.fg_warehouse
		cls.uom = cfg.stock_uom
		cls.group = frappe.db.get_value("Item Group", {}, "name")
		cls.parent_wh = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "is_group": 1}, "name"
		)
		suffix = random_string(6).upper()

		# Company kedua NYATA (bukan ROPI) + salah satu gudang leaf-nya —
		# untuk gate company (§6.2) dan mismatch warehouse-company (§6.3).
		cls.other_company_wh = frappe.get_all(
			"Warehouse",
			filters={"company": ("!=", cls.company), "is_group": 0},
			pluck="name",
			order_by="name",
			limit=1,
		)[0]
		cls.other_company = frappe.db.get_value("Warehouse", cls.other_company_wh, "company")
		assert frappe.db.exists("Company", cls.other_company)

		# Gudang fixture milik company utama
		cls.src_old = _make_warehouse(f"{PREFIX} Src Old {suffix}", cls.company, cls.parent_wh)
		cls.src_new = _make_warehouse(f"{PREFIX} Src New {suffix}", cls.company, cls.parent_wh)
		cls.manual_wh = _make_warehouse(f"{PREFIX} Manual {suffix}", cls.company, cls.parent_wh)
		cls.bom_row_wh = _make_warehouse(f"{PREFIX} BomRow {suffix}", cls.company, cls.parent_wh)
		cls.item_src_wh = _make_warehouse(f"{PREFIX} ItemSrc {suffix}", cls.company, cls.parent_wh)
		# Gudang milik company kedua — Warehouse.company WAJIB (reqd=1) di
		# erpnext v16 jadi gudang companyless tidak mungkin; rantai company
		# kedua memakai gudang-gudang ini.
		cls.shared_a = _make_warehouse(f"{PREFIX} Shared A {suffix}", cls.other_company)
		cls.shared_b = _make_warehouse(f"{PREFIX} Shared B {suffix}", cls.other_company)
		cls.shared_row = _make_warehouse(f"{PREFIX} Shared Row {suffix}", cls.other_company)
		cls.shared_wip = _make_warehouse(f"{PREFIX} Shared WIP {suffix}", cls.other_company)
		cls.shared_fg = _make_warehouse(f"{PREFIX} Shared FG {suffix}", cls.other_company)

		cls.rm1 = _make_item(f"{PREFIX}-RM1-{suffix}", cls.group, cls.uom)
		cls.rm2 = _make_item(f"{PREFIX}-RM2-{suffix}", cls.group, cls.uom)
		cls.fg = _make_item(f"{PREFIX}-FG-{suffix}", cls.group, cls.uom)
		cls.fg_ot = _make_item(f"{PREFIX}-FGOT-{suffix}", cls.group, cls.uom)

		cls.currency = frappe.db.get_value("Company", cls.company, "default_currency")
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cls.fg,
				"company": cls.company,
				"currency": cls.currency,
				"quantity": 1,
				"items": [
					{"item_code": cls.rm1, "qty": 2, "rate": 10, "uom": cls.uom, "stock_uom": cls.uom, "source_warehouse": cls.src_old},
					{"item_code": cls.rm2, "qty": 1, "rate": 10, "uom": cls.uom, "stock_uom": cls.uom, "source_warehouse": cls.bom_row_wh},
				],
			}
		)
		bom.insert()
		bom.submit()
		cls.bom = bom.name

		bom_ot = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cls.fg_ot,
				"company": cls.other_company,
				"currency": frappe.db.get_value("Company", cls.other_company, "default_currency"),
				"quantity": 1,
				"items": [
					{"item_code": cls.rm1, "qty": 2, "rate": 10, "uom": cls.uom, "stock_uom": cls.uom, "source_warehouse": cls.shared_a},
					{"item_code": cls.rm2, "qty": 1, "rate": 10, "uom": cls.uom, "stock_uom": cls.uom, "source_warehouse": cls.shared_row},
				],
			}
		)
		bom_ot.insert()
		bom_ot.submit()
		cls.bom_ot = bom_ot.name

	# ------------------------------------------------------------- helpers

	def _save_defaults(self, **overrides):
		"""Simpan state settings LENGKAP (field lain dikosongkan) — tiap test
		membangun state-nya sendiri (satu transaksi per class)."""
		payload = {
			"source_warehouse": None,
			"wip_warehouse": None,
			"fg_warehouse": None,
			"scrap_warehouse": None,
			"handover_warehouse": None,
			"handover_source_warehouse": None,
			"form_order_source_warehouse": None,
			"form_order_target_warehouse": None,
		}
		payload.update(overrides)
		return warehouse_defaults_save(**payload)

	def _make_wo(
		self, *, bom, company, source=None, wip=None, fg=None, scrap=None,
		submit=True, skip_transfer=False,
	):
		"""WO native pola test_wo_transaction_proof. Header source diisi
		SETELAH get_items_and_operations_from_bom supaya baris mempertahankan
		source BOM-nya sendiri (set_required_items: header menang atas BOM —
		dengan ini baris rm2 tetap beda dari header, kasus §4.3)."""
		wo = frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"production_item": frappe.db.get_value("BOM", bom, "item"),
				"bom_no": bom,
				"qty": 10,
				"company": company,
				"fg_warehouse": fg,
				"wip_warehouse": wip,
				"source_warehouse": None,
				"scrap_warehouse": scrap,
				"stock_uom": self.uom,
				"planned_start_date": now(),
				"transfer_material_against": "Work Order",
				"use_multi_level_bom": 0,
				"skip_transfer": 1 if skip_transfer else 0,
			}
		)
		wo.get_items_and_operations_from_bom()
		wo.source_warehouse = source
		wo.insert()
		if submit:
			wo.submit()
		return wo

	def _header(self, name):
		return frappe.db.get_value(
			DOCTYPE, name, ["source_warehouse", "wip_warehouse", "fg_warehouse"], as_dict=True
		)

	def _rows(self, name):
		return {
			r.item_code: r.source_warehouse
			for r in frappe.get_all(
				"Work Order Item",
				filters={"parent": name},
				fields=["item_code", "source_warehouse"],
			)
		}

	@staticmethod
	def _entry_for(entries, name):
		return next(e for e in entries if e["name"] == name)

	# ------------------------------------------------- skenario 1: draft
	def test_fu58_01_draft_propagation_header_and_rows(self):
		self._save_defaults(source_warehouse=self.src_old)  # baseline
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.src_old, wip=self.wip_wh, fg=self.fg_wh, submit=False)
		self.assertEqual(self._rows(wo.name)[self.rm1], self.src_old)
		# baris rm2 dikosongkan via db (bypass) — terisi lagi dari header BARU
		# oleh set_warehouses() saat save() native; itu bukti validate jalan
		# (Version tidak dibuat di bawah run-tests: flags.ignore_version =
		# frappe.in_test — Document._save, frappe model/document.py:577)
		rm2_row = frappe.db.get_value(
			"Work Order Item", {"parent": wo.name, "item_code": self.rm2}, "name"
		)
		frappe.db.set_value("Work Order Item", rm2_row, "source_warehouse", None, update_modified=False)
		saved = self._save_defaults(source_warehouse=self.src_new)
		names = [e["name"] for e in saved["propagated"]]
		self.assertIn(wo.name, names)
		entry = self._entry_for(saved["propagated"], wo.name)
		self.assertEqual(entry["changes"], {"source_warehouse": [self.src_old, self.src_new]})
		self.assertEqual(entry["docstatus"], 0)
		self.assertEqual(entry["rows_updated"], 1)  # rm1 ikut, rm2 (kosong) di luar hitungan loop
		self.assertEqual(self._header(wo.name).source_warehouse, self.src_new)
		rows = self._rows(wo.name)
		self.assertEqual(rows[self.rm1], self.src_new)
		# rm2 yang kosong terisi dari header baru oleh validate native (save() jalan)
		self.assertEqual(rows[self.rm2], self.src_new)

	# -------------------------------------------- skenario 2: submitted
	def test_fu58_02_submitted_propagation_db_set_and_rows(self):
		self._save_defaults(source_warehouse=self.src_old)
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.src_old, wip=self.wip_wh, fg=self.fg_wh, submit=True)
		done = self._make_wo(bom=self.bom, company=self.company, source=self.src_old, wip=self.wip_wh, fg=self.fg_wh, submit=True)
		# fixture Completed (terminal) — db-level scan membaca kolom status
		frappe.db.set_value(DOCTYPE, done.name, "status", "Completed", update_modified=False)
		saved = self._save_defaults(source_warehouse=self.src_new)
		names = [e["name"] for e in saved["propagated"]]
		self.assertIn(wo.name, names)
		self.assertNotIn(done.name, names)  # WO Completed tidak tersentuh
		entry = self._entry_for(saved["propagated"], wo.name)
		self.assertEqual(entry["docstatus"], 1)
		self.assertEqual(entry["rows_updated"], 1)
		self.assertEqual(self._header(wo.name).source_warehouse, self.src_new)
		rows = self._rows(wo.name)
		self.assertEqual(rows[self.rm1], self.src_new)
		self.assertEqual(rows[self.rm2], self.bom_row_wh)
		# WO Completed tetap memegang nilai lama
		self.assertEqual(self._header(done.name).source_warehouse, self.src_old)
		self.assertEqual(self._rows(done.name)[self.rm1], self.src_old)

	# ------------------------------------------ skenario 3: nilai manual
	def test_fu58_03_manual_value_never_overwritten(self):
		self._save_defaults(source_warehouse=self.src_old)
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.manual_wh, wip=self.wip_wh, fg=self.fg_wh, submit=True)
		saved = self._save_defaults(source_warehouse=self.src_new)
		self.assertNotIn(wo.name, [e["name"] for e in saved["propagated"]])
		self.assertEqual(self._header(wo.name).source_warehouse, self.manual_wh)
		self.assertEqual(self._rows(wo.name)[self.rm1], self.src_old)  # baris tak tersentuh

	# ------------------------------------------- skenario 4: settings-only
	def test_fu58_04_settings_only_fields_do_not_touch_work_orders(self):
		self._save_defaults(source_warehouse=self.src_old)  # baseline
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.src_old, wip=self.wip_wh, fg=self.fg_wh, submit=True)
		saved = self._save_defaults(
			source_warehouse=self.src_old,  # TIDAK berubah
			handover_warehouse=self.fg_wh,  # berubah — settings-only
			form_order_source_warehouse=self.manual_wh,  # berubah — settings-only
		)
		self.assertEqual(saved["propagated"], [])
		self.assertEqual(saved["handover_warehouse"], self.fg_wh)
		self.assertEqual(self._header(wo.name).source_warehouse, self.src_old)
		self.assertEqual(self._rows(wo.name)[self.rm1], self.src_old)

	# ------------------------------------ skenario 5: pengosongan (clear)
	def test_fu58_05_clearing_default_does_not_propagate(self):
		self._save_defaults(source_warehouse=self.src_old)
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.src_old, wip=self.wip_wh, fg=self.fg_wh, submit=True)
		saved = self._save_defaults(source_warehouse=None)  # old -> kosong
		self.assertEqual(saved["propagated"], [])
		self.assertIsNone(saved["source_warehouse"])
		self.assertEqual(self._header(wo.name).source_warehouse, self.src_old)
		self.assertEqual(self._rows(wo.name)[self.rm1], self.src_old)

	# ------------------------------------------------ skenario 8: remediasi
	def test_fu58_08_remediation_dry_run_idempotent_and_permission(self):
		self._save_defaults()
		wo_sub = self._make_wo(bom=self.bom_ot, company=self.other_company, source=self.shared_a, wip=self.shared_wip, fg=self.shared_fg, submit=True)
		wo_draft = self._make_wo(bom=self.bom_ot, company=self.other_company, source=self.shared_a, wip=self.shared_wip, fg=self.shared_fg, submit=False)
		# default diisi DARI kondisi kosong (pola gap §4.4 — tanpa propagasi)
		self._save_defaults(
			source_warehouse=self.shared_b,
			wip_warehouse=self.shared_wip,
			fg_warehouse=self.shared_fg,
		)

		plan = sync_warehouse_defaults_to_running_work_orders(dry_run=1)
		self.assertTrue(plan["dry_run"])
		self.assertIn(wo_sub.name, [e["name"] for e in plan["updated"]])
		self.assertIn(wo_draft.name, [e["name"] for e in plan["updated"]])
		# dry run tidak menulis apa pun
		self.assertEqual(self._header(wo_sub.name).source_warehouse, self.shared_a)
		self.assertEqual(self._header(wo_draft.name).source_warehouse, self.shared_a)

		result = sync_warehouse_defaults_to_running_work_orders(dry_run=0)
		self.assertFalse(result["dry_run"])
		for wo_name in (wo_sub.name, wo_draft.name):
			entry = self._entry_for(result["updated"], wo_name)
			self.assertEqual(entry["changes"], {"source_warehouse": [self.shared_a, self.shared_b]})
			self.assertEqual(entry["rows_updated"], 1)
			self.assertEqual(self._header(wo_name).source_warehouse, self.shared_b)
			self.assertEqual(self._rows(wo_name)[self.rm1], self.shared_b)
			self.assertEqual(self._rows(wo_name)[self.rm2], self.shared_row)

		# idempoten: eksekusi kedua tidak menulis apa pun (run pertama sudah
		# mengonvergensi SEMUA WO berjalan in-transaction)
		second = sync_warehouse_defaults_to_running_work_orders(dry_run=0)
		self.assertEqual(second["updated"], [])

		# gate permission: user tanpa write Manufacturing Settings ditolak
		bare = _make_user("bare")
		try:
			frappe.set_user(bare)
			with self.assertRaises(frappe.PermissionError):
				sync_warehouse_defaults_to_running_work_orders(dry_run=1)
		finally:
			frappe.set_user("Administrator")

	# -------------------------------------------- skenario 9: validasi simpan
	def test_fu58_09_save_validation_throws(self):
		self._save_defaults(source_warehouse=self.src_old)  # baseline
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._save_defaults(source_warehouse=f"{PREFIX}-Gudang-Tidak-Ada")
		self.assertIn("Gudang tidak ditemukan", str(ctx.exception))
		# throw terjadi SEBELUM simpanan — settings tidak berubah
		values = warehouse_defaults()
		self.assertEqual(values["source_warehouse"], self.src_old)

	# --------------------------------------- skenario 10: baris BOM tetap
	def test_fu58_10_bom_rows_preserved(self):
		self._save_defaults(source_warehouse=self.src_old)
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.src_old, wip=self.wip_wh, fg=self.fg_wh, submit=True)
		self.assertEqual(self._rows(wo.name)[self.rm2], self.bom_row_wh)  # precondition
		saved = self._save_defaults(source_warehouse=self.src_new)
		entry = self._entry_for(saved["propagated"], wo.name)
		self.assertEqual(entry["rows_updated"], 1)
		rows = self._rows(wo.name)
		self.assertEqual(rows[self.rm1], self.src_new)
		self.assertEqual(rows[self.rm2], self.bom_row_wh)  # baris BOM dipertahankan

	# ----------------------------------------- skenario 11: race draft lock
	def test_fu58_11_draft_lock_and_adonan_preserved(self):
		self._save_defaults(source_warehouse=self.src_old)
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.src_old, wip=self.wip_wh, fg=self.fg_wh, submit=False)
		prepare(wo.name, {"adonan_ke": "3", "leader": "Budi FU58"})

		# bukti lock: get_value(..., "name", for_update=True) dipanggil helper
		real_get_value = frappe.db.get_value
		calls = []

		def spy(*args, **kwargs):
			calls.append((args, kwargs))
			return real_get_value(*args, **kwargs)

		with mock.patch.object(frappe.db, "get_value", side_effect=spy):
			outcome = _apply_warehouse_changes(
				wo.name, {"source_warehouse": (self.src_old, self.src_new)}
			)
		self.assertTrue(
			any(
				args[:3] == (DOCTYPE, wo.name, "name") and kwargs.get("for_update")
				for args, kwargs in calls
			),
			"helper harus lock baris (for_update) sebelum menulis",
		)
		self.assertEqual(outcome["written"]["name"], wo.name)
		header = frappe.db.get_value(
			DOCTYPE, wo.name, ["source_warehouse", "custom_adonan_ke", "custom_leader_produksi"], as_dict=True
		)
		# hanya kolom gudang yang berubah — Data Adonan utuh
		self.assertEqual(header.source_warehouse, self.src_new)
		self.assertEqual(header.custom_adonan_ke, 3)  # FU69: Int
		self.assertEqual(header.custom_leader_produksi, "Budi FU58")
		self.assertEqual(self._rows(wo.name)[self.rm1], self.src_new)

	# --------------------------------- skenario 12: builder baca default baru
	def test_fu58_12_material_transfer_builder_uses_new_default(self):
		self._save_defaults()
		wo = self._make_wo(bom=self.bom_ot, company=self.other_company, source=self.shared_a, wip=self.shared_wip, fg=self.shared_fg, submit=True)
		self._save_defaults(source_warehouse=self.shared_b)
		result = sync_warehouse_defaults_to_running_work_orders(dry_run=0)
		self.assertIn(wo.name, [e["name"] for e in result["updated"]])

		se = make_wo_stock_entry(wo.name, "Material Transfer for Manufacture")
		rows = se["items"]  # dict dari builder — .items adalah method dict
		froms = {item["item_code"]: item["s_warehouse"] for item in rows}
		self.assertEqual(froms[self.rm1], self.shared_b)  # default baru
		self.assertEqual(froms[self.rm2], self.shared_row)  # baris BOM tetap
		tos = {item["t_warehouse"] for item in rows}
		self.assertEqual(tos, {self.shared_wip})

	# --------------------------------- skenario 13: backward-compat return
	def test_fu58_13_flat_return_keys_plus_new_keys(self):
		self._save_defaults(source_warehouse=self.src_old)
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.src_old, wip=self.wip_wh, fg=self.fg_wh, submit=False)
		saved = self._save_defaults(source_warehouse=self.src_new)
		for key in (
			"source_warehouse", "wip_warehouse", "fg_warehouse", "scrap_warehouse",
			"handover_warehouse", "handover_source_warehouse",
			"form_order_source_warehouse", "form_order_target_warehouse",
		):
			self.assertIn(key, saved)
		self.assertEqual(saved["fg_warehouse"], None)  # kunci flat tetap valid dibaca
		self.assertEqual(saved["source_warehouse"], self.src_new)
		self.assertIn("propagated", saved)
		self.assertIn("skipped", saved)
		self.assertIn("failed", saved)
		self.assertIn(wo.name, [e["name"] for e in saved["propagated"]])

	# ------------------------------------- skenario 14: gap clear -> set
	def test_fu58_14_clear_then_set_gap_requires_remediation(self):
		self._save_defaults()
		wo = self._make_wo(bom=self.bom_ot, company=self.other_company, source=self.shared_a, wip=self.shared_wip, fg=self.shared_fg, submit=True)
		# simpan #1: kosongkan (old -> kosong: tidak propagasi)
		first = self._save_defaults(source_warehouse=None)
		self.assertEqual(first["propagated"], [])
		# simpan #2: isi nilai baru (old kosong: tidak propagasi — gap §4.4)
		second = self._save_defaults(source_warehouse=self.shared_b)
		self.assertEqual(second["propagated"], [])
		self.assertEqual(self._header(wo.name).source_warehouse, self.shared_a)  # terkunci di nilai lama
		# jalur pemulihan: remediasi
		result = sync_warehouse_defaults_to_running_work_orders(dry_run=0)
		self.assertIn(wo.name, [e["name"] for e in result["updated"]])
		self.assertEqual(self._header(wo.name).source_warehouse, self.shared_b)
		self.assertEqual(self._rows(wo.name)[self.rm1], self.shared_b)

	# ------------------------------------ skenario 15: TOCTOU jalur tulis
	def test_fu58_15_stale_expectation_skipped_without_write(self):
		self._save_defaults()  # normalisasi: company kosong agar mismatch kolom menjadi cabang yang diuji
		wo = self._make_wo(bom=self.bom, company=self.company, source=self.manual_wh, wip=self.wip_wh, fg=self.fg_wh, submit=True)
		# helper dipanggil dengan ekspektasi old basi (WO sudah berubah pasca-scan)
		outcome = _apply_warehouse_changes(
			wo.name, {"source_warehouse": (self.src_old, self.src_new)}
		)
		self.assertIsNone(outcome["written"])
		self.assertEqual(len(outcome["skipped"]), 1)
		self.assertIn("kolom source_warehouse", outcome["skipped"][0])
		self.assertEqual(outcome["failed"], [])
		# tidak ada tulisan apa pun
		self.assertEqual(self._header(wo.name).source_warehouse, self.manual_wh)
		self.assertEqual(self._rows(wo.name)[self.rm1], self.src_old)

	# -------------------------------- skenario 16: guard skip_transfer (wip)
	def test_fu58_16_skip_transfer_wip_guarded_source_still_processed(self):
		self._save_defaults()
		wo = self._make_wo(
			bom=self.bom_ot, company=self.other_company, source=self.shared_a,
			wip=self.shared_wip, fg=self.shared_fg, submit=True, skip_transfer=True,
		)
		# validate native me-reset wip None pada WO skip_transfer
		self.assertIsNone(self._header(wo.name).wip_warehouse)
		self._save_defaults(
			source_warehouse=self.shared_b,
			wip_warehouse=self.shared_wip,
			fg_warehouse=self.shared_fg,
		)
		result = sync_warehouse_defaults_to_running_work_orders(dry_run=0)
		# wip tidak ditulis (guard) dan tercatat alasannya
		skipped_for_wo = [s for s in result["skipped"] if s["name"] == wo.name]
		self.assertTrue(any("skip_transfer" in s["reason"] for s in skipped_for_wo))
		self.assertIsNone(self._header(wo.name).wip_warehouse)
		# source tetap diproses
		entry = self._entry_for(result["updated"], wo.name)
		self.assertEqual(entry["changes"], {"source_warehouse": [self.shared_a, self.shared_b]})
		self.assertEqual(self._header(wo.name).source_warehouse, self.shared_b)
		self.assertEqual(self._rows(wo.name)[self.rm1], self.shared_b)

	# ---------- skenario 18 (review FU58): preview dry_run jujur soal baris
	def test_fu58_18_dry_run_rows_only_via_source_change(self):
		# header source sudah == default; hanya fg yang beda -> WO masuk
		# rencana TANPA perubahan source_warehouse
		self._save_defaults(
			source_warehouse=self.shared_b,
			fg_warehouse=self.shared_fg,
		)
		wo = self._make_wo(bom=self.bom_ot, company=self.other_company, source=self.shared_b, wip=self.shared_wip, fg=self.shared_fg, submit=True)
		frappe.db.set_value(DOCTYPE, wo.name, "fg_warehouse", self.shared_a, update_modified=False)
		# ada baris bersource kosong — kode lama salah menghitungnya sebagai
		# rows_updated padahal eksekusi tidak menulis baris tanpa perubahan source
		rm1_row = frappe.db.get_value(
			"Work Order Item", {"parent": wo.name, "item_code": self.rm1}, "name"
		)
		frappe.db.set_value("Work Order Item", rm1_row, "source_warehouse", None, update_modified=False)

		plan = sync_warehouse_defaults_to_running_work_orders(dry_run=1)
		entry = self._entry_for(plan["updated"], wo.name)
		self.assertNotIn("source_warehouse", entry["changes"])
		self.assertEqual(entry["rows_updated"], 0)  # preview jujur: 0 baris akan ditulis

		result = sync_warehouse_defaults_to_running_work_orders(dry_run=0)
		entry = self._entry_for(result["updated"], wo.name)
		self.assertEqual(entry["changes"], {"fg_warehouse": [self.shared_a, self.shared_fg]})
		self.assertEqual(entry["rows_updated"], 0)
		self.assertEqual(self._header(wo.name).fg_warehouse, self.shared_fg)
		self.assertIsNone(self._rows(wo.name)[self.rm1])  # baris kosong tidak disentuh


class TestSettingsFieldsSelfHeal(IntegrationTestCase):
	"""FU60/FU61 — regresi insiden produksi 22 Sep: update kode tanpa migrate
	meninggalkan field settings tak terpasang; bacaan kini harus konvergen
	sendiri — memasang yang kurang DAN memensiunkan field company FU61."""

	def test_warehouse_defaults_converges_fields(self):
		# (a) field paket hilang -> dipasang ulang oleh bacaan
		target = frappe.db.get_value(
			"Custom Field",
			{"dt": "Manufacturing Settings", "fieldname": "custom_default_form_order_target_warehouse"},
			"name",
		)
		self.assertTrue(target, "precondition: field terpasang oleh migrate")
		frappe.delete_doc("Custom Field", target, force=True)
		frappe.clear_cache(doctype="Manufacturing Settings")
		self.assertFalse(
			frappe.get_meta("Manufacturing Settings").get_field("custom_default_form_order_target_warehouse")
		)

		values = warehouse_defaults()  # dulu: UnknownFieldError 500

		self.assertTrue(
			frappe.db.get_value(
				"Custom Field",
				{"dt": "Manufacturing Settings", "fieldname": "custom_default_form_order_target_warehouse"},
				"name",
			),
			"field harus terpasang ulang oleh konvergensi",
		)
		# (b) field company FU61 yang masih ada -> dipensiunkan oleh bacaan
		frappe.get_doc({
			"doctype": "Custom Field",
			"dt": "Manufacturing Settings",
			"fieldname": "custom_default_company",
			"label": "Default Company (Production App)",
			"fieldtype": "Link",
			"options": "Company",
			"insert_after": "custom_default_form_order_target_warehouse",
		}).insert(ignore_permissions=True)
		frappe.clear_cache(doctype="Manufacturing Settings")
		warehouse_defaults()
		self.assertFalse(
			frappe.db.get_value(
				"Custom Field",
				{"dt": "Manufacturing Settings", "fieldname": "custom_default_company"},
				"name",
			),
			"field company FU61 harus terpensiunkan",
		)
		self.assertNotIn("company", values)
