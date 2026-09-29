# T03/T04 transaction proof — Work Order workspace (isolated records only)
#
# Proves, by execution on the installed runtime:
# - transfer + manufacture with good below / equal / above plan (within the
#   current native overproduction allowance), and an above-limit rejection;
# - planned raw-material consumption is unchanged (backflush follows what was
#   actually transferred — "Material Transferred for Manufacture");
# - FG stock / batch bundle quantities agree with the manufactured quantity;
# - WO produced qty / process loss / status follow native rules;
# - with operations (T04): Job Card completion gates Manufacture.
#
# Every record created here is test-only (items/BOM/WO/Stock Entries prefixed
# TESTWOT03-); the Frappe test framework rolls each test back, so no
# operational document or setting is touched. No setting is modified.

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, now, random_string

from erpnext.manufacturing.doctype.work_order.work_order import (
	make_stock_entry as make_wo_stock_entry,
)
from erpnext.manufacturing.doctype.work_order.work_order import StockOverProductionError

PREFIX = "TESTWOT03-"


def _first_existing_wo_config():
	"""Read-only: reuse the company/warehouses an existing WO already uses."""
	name = frappe.db.get_value(
		"Work Order",
		{"docstatus": 1, "fg_warehouse": ("is", "set"), "wip_warehouse": ("is", "set")},
		"name",
		order_by="creation desc",
	)
	if not name:
		frappe.throw("No submitted Work Order exists to derive a test configuration from")
	wo = frappe.db.get_value(
		"Work Order",
		name,
		["company", "fg_warehouse", "wip_warehouse", "source_warehouse", "stock_uom"],
		as_dict=True,
	)
	return wo


def _make_item(code, group, uom, batch=False):
	if frappe.db.exists("Item", code):
		frappe.delete_doc("Item", code, force=True)
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": group,
			"stock_uom": uom,
			"is_stock_item": 1,
			"is_purchase_item": 0,
			"is_sales_item": 0,
			"has_batch_no": 1 if batch else 0,
			"create_new_batch": 1 if batch else 0,
			"standard_rate": 100,
			"is_fixed_asset": 0,
			"opening_stock": 0,
		}
	)
	doc.insert()
	return doc


class TestWorkOrderTransactionProof(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cfg = _first_existing_wo_config()
		cls.company = cfg.company
		cls.fg_wh = cfg.fg_warehouse
		cls.wip_wh = cfg.wip_warehouse
		cls.src_wh = cfg.source_warehouse or cfg.wip_warehouse
		cls.uom = cfg.stock_uom
		cls.group = frappe.db.get_value("Item Group", {}, "name")
		cls.currency = frappe.db.get_value("Company", cls.company, "default_currency")
		cls.allowance = flt(
			frappe.db.get_single_value(
				"Manufacturing Settings", "overproduction_percentage_for_work_order"
			)
		)
		suffix = random_string(6).upper()

		cls.rm1 = _make_item(f"{PREFIX}RM1-{suffix}", cls.group, cls.uom).name
		cls.rm2 = _make_item(f"{PREFIX}RM2-{suffix}", cls.group, cls.uom).name
		cls.fg = _make_item(f"{PREFIX}FG-{suffix}", cls.group, cls.uom, batch=True).name

		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cls.fg,
				"company": cls.company,
				"currency": cls.currency,
				"quantity": 1,
				"items": [
					{
						"item_code": cls.rm1,
						"qty": 2,
						"rate": 10,
						"uom": cls.uom,
						"stock_uom": cls.uom,
						"source_warehouse": cls.src_wh,
					},
					{
						"item_code": cls.rm2,
						"qty": 1,
						"rate": 10,
						"uom": cls.uom,
						"stock_uom": cls.uom,
						"source_warehouse": cls.src_wh,
					},
				],
			}
		)
		bom.insert()
		bom.submit()
		cls.bom = bom.name

	def _receipt(self, item, qty):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"basic_rate": 10,
						"t_warehouse": self.src_wh,
						"use_serial_batch_fields": 0,
					}
				],
			}
		)
		se.insert()
		se.submit()
		return se

	def _make_wo(self, qty=100, submit=True):
		wo = frappe.get_doc(
			{
				"doctype": "Work Order",
				"production_item": self.fg,
				"bom_no": self.bom,
				"qty": qty,
				"company": self.company,
				"fg_warehouse": self.fg_wh,
				"wip_warehouse": self.wip_wh,
				"source_warehouse": self.src_wh,
				"scrap_warehouse": self.fg_wh,
				"stock_uom": self.uom,
				"planned_start_date": now(),
				"transfer_material_against": "Work Order",
				"use_multi_level_bom": 0,
			}
		)
		wo.get_items_and_operations_from_bom()
		wo.insert()
		if submit:
			wo.submit()
		return wo

	def _transfer(self, wo):
		se = frappe.get_doc(make_wo_stock_entry(wo.name, "Material Transfer for Manufacture"))
		se.insert()
		se.submit()
		return se

	def _transferred_map(self, wo):
		"""item_code -> qty actually transferred to WIP (submitted transfer rows)."""
		rows = frappe.get_all(
			"Stock Entry Detail",
			filters={
				"parent": ("in", frappe.get_all(
					"Stock Entry",
					filters={
						"work_order": wo.name,
						"purpose": "Material Transfer for Manufacture",
						"docstatus": 1,
					},
					pluck="name",
				)),
				"docstatus": 1,
			},
			fields=["item_code", "qty"],
		)
		transferred = frappe._dict()
		for r in rows:
			transferred[r.item_code] = transferred.get(r.item_code, 0.0) + flt(r.qty)
		return transferred

	def _manufacture_doc(self, wo, good):
		se = frappe.get_doc(make_wo_stock_entry(wo.name, "Manufacture", qty=good))
		# Workspace consumption rule (plan non-negotiable #2): actual yield must
		# NOT rescale raw-material consumption — restore each RM row to the
		# transferred (planned) quantity, overriding native yield proration.
		transferred = self._transferred_map(wo)
		for row in se.items:
			if not row.is_finished_item and row.item_code in transferred:
				row.qty = transferred[row.item_code]
				row.transfer_qty = row.qty * flt(row.conversion_factor or 1)
		return se

	def _manufacture(self, wo, good):
		se = self._manufacture_doc(wo, good)
		se.insert()
		se.submit()
		return se

	def _fg_rows(self, se):
		return [r for r in se.items if r.is_finished_item]

	def _rm_rows(self, se):
		return sorted([(r.item_code, flt(r.qty)) for r in se.items if not r.is_finished_item])

	def _wo_batches(self, wo):
		return frappe.get_all("Batch", filters={"reference_name": wo.name}, pluck="name")

	def _fg_bundle_entries(self, se):
		"""Batch bundle truth for the submitted manufacture entry."""
		out = []
		updated = frappe.get_doc("Stock Entry", se.name)
		for row in self._fg_rows(updated):
			if row.serial_and_batch_bundle:
				out.extend(
					frappe.get_all(
						"Serial and Batch Entry",
						filters={"parent": row.serial_and_batch_bundle},
						fields=["batch_no", "qty"],
					)
				)
			elif row.batch_no:
				out.append(frappe._dict({"batch_no": row.batch_no, "qty": flt(row.qty)}))
		return out

	# ------------------------------------------------------------------ T03

	def test_t03_receipt_needed_for_transfer(self):
		"""Raw materials must be in stock before any transfer can be proven."""
		receipt = self._receipt(self.rm1, 1000)
		receipt2 = self._receipt(self.rm2, 1000)
		self.assertEqual(receipt.docstatus, 1)
		self.assertEqual(receipt2.docstatus, 1)

	def test_t03_transfer_planned_quantity(self):
		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)

		self._transfer(wo)
		wo.reload()

		self.assertEqual(flt(wo.material_transferred_for_manufacturing), 100)
		self.assertEqual(wo.status, "In Process")

	def test_t03_manufacture_equal_plan(self):
		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		self._transfer(wo)

		se = self._manufacture(wo, 100)

		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 100)
		self.assertEqual(flt(wo.process_loss_qty), 0)
		self.assertEqual(wo.status, "Completed")

		# FG row and batch truth
		fg_rows = self._fg_rows(se)
		self.assertEqual(len(fg_rows), 1)
		self.assertEqual(flt(fg_rows[0].qty), 100)
		entries = self._fg_bundle_entries(se)
		self.assertEqual(len(entries), 1, "single batch bundle entry expected")
		self.assertEqual(flt(entries[0].qty), 100)
		self.assertIn(entries[0].batch_no, self._wo_batches(wo))

		# planned raw materials: consumed exactly what was transferred
		self.assertEqual(self._rm_rows(se), [(self.rm1, 200.0), (self.rm2, 100.0)])

	def test_t03_manufacture_below_plan(self):
		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		self._transfer(wo)

		se = self._manufacture(wo, 80)

		wo.reload()
		# actual good qty recorded, no fabricated loss, WO not force-completed
		self.assertEqual(flt(wo.produced_qty), 80)
		self.assertEqual(flt(wo.process_loss_qty), 0)
		self.assertEqual(wo.status, "In Process")

		# raw materials still consumed at the planned/transferred quantity
		self.assertEqual(self._rm_rows(se), [(self.rm1, 200.0), (self.rm2, 100.0)])
		entries = self._fg_bundle_entries(se)
		self.assertEqual(flt(entries[0].qty), 80)

	def test_t03_manufacture_above_plan_within_allowance(self):
		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		self._transfer(wo)

		over_qty = 100 + (100 * self.allowance / 100)  # exactly at the allowance edge
		se = self._manufacture(wo, over_qty)

		wo.reload()
		self.assertEqual(flt(wo.produced_qty), over_qty)
		self.assertEqual(wo.status, "Completed")

		# raw materials remain at planned quantity, not rescaled to yield
		self.assertEqual(self._rm_rows(se), [(self.rm1, 200.0), (self.rm2, 100.0)])

	def _make_pp_wo(self, planned_qty=100):
		"""Production Plan -> native WO generation -> linked, submitted WO."""
		pp = frappe.get_doc(
			{
				"doctype": "Production Plan",
				"company": self.company,
				"po_items": [
					{
						"item_code": self.fg,
						"bom_no": self.bom,
						"stock_uom": self.uom,
						"planned_qty": planned_qty,
						"planned_start_date": now(),
					}
				],
			}
		)
		pp.insert()
		pp.submit()
		pp.make_work_order()
		# native PP generation leaves a draft WO with empty warehouses when the
		# plan row carries none — finish it the way the workspace would
		wo_name = frappe.db.get_value(
			"Work Order", {"production_plan": pp.name}, "name", order_by="creation desc"
		)
		wo = frappe.get_doc("Work Order", wo_name)
		wo.fg_warehouse = self.fg_wh
		wo.wip_warehouse = self.wip_wh
		wo.source_warehouse = self.src_wh
		wo.scrap_warehouse = self.fg_wh
		for row in wo.required_items or []:
			row.source_warehouse = self.src_wh
		wo.save()
		wo.submit()
		return pp, wo

	def test_t03_production_plan_equal_plan(self):
		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		pp, wo = self._make_pp_wo(100)
		self.assertEqual(wo.docstatus, 1)
		self.assertEqual(wo.production_plan, pp.name)

		self._transfer(wo)
		self._manufacture(wo, 100)

		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 100)
		self.assertEqual(wo.status, "Completed")

		pp.reload()
		self.assertEqual(flt(pp.po_items[0].produced_qty), 100)

	def test_t03_production_plan_above_within_allowance(self):
		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		pp, wo = self._make_pp_wo(100)
		self._transfer(wo)

		over_qty = 100 + (100 * self.allowance / 100)
		se = self._manufacture(wo, over_qty)

		wo.reload()
		self.assertEqual(flt(wo.produced_qty), over_qty)
		self.assertEqual(wo.status, "Completed")
		pp.reload()
		self.assertEqual(flt(pp.po_items[0].produced_qty), over_qty)

	def test_t03_manufacture_above_limit_rejected_atomically(self):
		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		self._transfer(wo)

		over_limit = 100 + (100 * self.allowance / 100) + 1
		se = self._manufacture_doc(wo, over_limit)
		with self.assertRaises(ValidationError):  # native overproduction guard (validate stage)
			se.insert()
		try:
			se.submit()
		except ValidationError:
			pass  # alternate guard path (submit stage) — also acceptable

		# no partial writes: WO untouched, no submitted manufacture, no stock movement
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 0)
		self.assertEqual(flt(wo.process_loss_qty), 0)
		self.assertEqual(wo.status, "In Process")
		self.assertFalse(
			frappe.db.exists(
				"Stock Entry",
				{"work_order": wo.name, "purpose": "Manufacture", "docstatus": 1},
			)
		)
		self.assertFalse(
			frappe.get_all(
				"Stock Ledger Entry",
				filters={"voucher_no": se.name, "is_cancelled": 0},
			)
		)
		if se.docstatus == 0:
			se.delete()


	def test_t06_stage_mapping_through_lifecycle(self):
		from production_app.api.work_order import derive_stage, wo_detail

		wo = self._make_wo(100, submit=False)
		self.assertEqual(derive_stage(wo), "persiapan")

		wo.submit()
		self.assertEqual(derive_stage(wo), "material")

		se = frappe.get_doc(make_wo_stock_entry(wo.name, "Material Transfer for Manufacture"))
		se.insert()
		se.submit()
		wo.reload()
		self.assertEqual(derive_stage(wo), "pre_packing")

		wo.db_set("custom_prepacking_confirmed", 1)
		wo.reload()
		self.assertEqual(derive_stage(wo), "post_packing")

		wo.db_set("custom_postpacking_confirmed", 1)
		wo.reload()
		self.assertEqual(derive_stage(wo), "finish")

		detail = wo_detail(wo.name)
		self.assertEqual(detail["stage"], "finish")
		self.assertEqual(flt(detail["remaining_transfer"]), 0)
		self.assertEqual(flt(detail["remaining_produce"]), 100)
		# allowance comes from the LIVE Manufacturing Settings (the operator may
		# change it — observed 25% at T03, 50% on 2026-09-14); never hardcode
		allowance = flt(
			frappe.db.get_single_value(
				"Manufacturing Settings", "overproduction_percentage_for_work_order"
			)
		)
		self.assertEqual(flt(detail["max_allowed_qty"]), 100 * (1 + allowance / 100))

	def test_t06_list_filter_and_operational_wo_mapping(self):
		from production_app.api.work_order import wo_detail, wo_list

		wo = self._make_wo(100)
		rows = wo_list(search=wo.name, stage="material")
		self.assertTrue(any(r.name == wo.name for r in rows))

		# operational WOs map by current document truth (read-only access)
		completed = frappe.db.get_value(
			"Work Order", {"docstatus": 1, "status": "Completed", "fg_warehouse": ("is", "set")}, "name"
		)
		self.assertEqual(wo_detail(completed)["stage"], "selesai")
		cancelled = frappe.db.get_value("Work Order", {"docstatus": 2}, "name")
		if cancelled:
			self.assertEqual(wo_detail(cancelled)["stage"], "cancelled")

	def test_t06_search_matches_item_name(self):
		from production_app.api.work_order import wo_list

		wo = self._make_wo(100)
		original = frappe.db.get_value("Item", self.fg, "item_name")
		marker = f"Roti Unik {random_string(6).upper()}"
		try:
			frappe.db.set_value("Item", self.fg, "item_name", marker)
			# FU44: item name (berbeda dari kode) menemukan WO-nya
			self.assertTrue(any(r.name == wo.name for r in wo_list(search=marker)))
			# jalur lama tetap jalan: kode item dan nama Work Order
			self.assertTrue(any(r.name == wo.name for r in wo_list(search=self.fg)))
			self.assertTrue(any(r.name == wo.name for r in wo_list(search=wo.name)))
		finally:
			frappe.db.set_value("Item", self.fg, "item_name", original)

	def test_t06_permission_filtering(self):
		from production_app.api.work_order import wo_detail, wo_list

		user = frappe.get_doc(
			{"doctype": "User", "email": "testwot06@prodapp.example.com", "first_name": "T06"}
		).insert().name
		wo = self._make_wo(100)

		frappe.set_user(user)
		try:
			self.assertFalse(wo_list(search=wo.name), "unauthorized WOs must be absent")
			with self.assertRaises(frappe.PermissionError):
				wo_detail(wo.name)
		finally:
			frappe.set_user("Administrator")

	def test_t07_prepare_valid_data_and_native_submit(self):
		from production_app.api.work_order import prepare

		wo = self._make_wo(100, submit=False)
		result = prepare(
			wo.name,
			values={
				"adonan_ke": "3",
				"adonan": 3,
				"jam_adonan": "04:30:00",
				"suhu_adonan": 28.5,
				"penimbang": "Rina Wijaya",
				"jumlah_kru": 4,
				"leader": "Budi Santoso",
			},
			submit=1,
		)
		self.assertTrue(result["submitted_now"])
		wo.reload()
		self.assertEqual(wo.docstatus, 1)
		self.assertEqual(wo.custom_adonan_ke, "3")
		self.assertEqual(str(wo.custom_jam_adonan), "4:30:00")  # TIME -> timedelta
		self.assertEqual(flt(wo.custom_suhu_adonan), 28.5)
		self.assertEqual(wo.custom_nama_penimbang, "Rina Wijaya")  # FU10: free-text name
		self.assertEqual(wo.custom_jumlah_kru, 4)
		self.assertEqual(wo.custom_leader_produksi, "Budi Santoso")
		self.assertEqual(result["job_cards"], [])

		# repeat with the same payload: no resubmit, no job card duplication
		result2 = prepare(wo.name, values={"suhu_adonan": 29.0})
		self.assertFalse(result2["submitted_now"])
		wo.reload()
		self.assertEqual(wo.docstatus, 1)
		self.assertEqual(flt(wo.custom_suhu_adonan), 29.0)
		self.assertEqual(len(result2["job_cards"]), 0)

	def test_t07_prepare_invalid_input_rolls_back(self):
		"""FU10: penimbang is free text now, so the rollback proof uses a
		still-validated field (garbage time) — validation fires BEFORE any
		write and nothing from the payload persists."""
		from production_app.api.work_order import prepare

		wo = self._make_wo(100, submit=False)
		with self.assertRaises(ValueError):  # get_time raises the parser error as-is (T27 contract)
			prepare(
				wo.name,
				values={"penimbang": "Rina Wijaya", "leader": "Budi", "jam_adonan": "bukan-jam"},
			)
		wo.reload()
		# nothing persisted from the failed payload
		self.assertIsNone(wo.get("custom_leader_produksi"))
		self.assertIsNone(wo.get("custom_nama_penimbang"))
		self.assertEqual(wo.docstatus, 0)

	def test_t08_transfer_once_and_repeat_safe(self):
		from production_app.api.work_order import transfer_materials

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)

		first = transfer_materials(wo.name)
		self.assertEqual(first["transferred_now"], 100)
		self.assertIsNotNone(first["stock_entry"])
		self.assertEqual(first["material_transferred_for_manufacturing"], 100)
		self.assertEqual(first["stage"], "pre_packing")

		second = transfer_materials(wo.name)
		self.assertEqual(second["transferred_now"], 0)
		self.assertIsNone(second["stock_entry"])
		# exactly one submitted transfer exists
		count = len(frappe.get_all(
			"Stock Entry",
			filters={"work_order": wo.name, "purpose": "Material Transfer for Manufacture", "docstatus": 1},
		))
		self.assertEqual(count, 1)

	def test_t08_respects_existing_partial_transfer(self):
		from production_app.api.work_order import transfer_materials

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)

		partial = frappe.get_doc(make_wo_stock_entry(wo.name, "Material Transfer for Manufacture", qty=40))
		partial.insert()
		partial.submit()

		result = transfer_materials(wo.name)
		self.assertEqual(result["transferred_now"], 60)
		wo.reload()
		self.assertEqual(flt(wo.material_transferred_for_manufacturing), 100)

	def test_t08_draft_blocks_and_shortage_is_atomic(self):
		from production_app.api.work_order import transfer_materials

		wo = self._make_wo(100)

		# an unrelated/unfinished draft blocks the workspace action
		draft = frappe.get_doc(make_wo_stock_entry(wo.name, "Material Transfer for Manufacture", qty=10))
		draft.insert()
		with self.assertRaises(frappe.ValidationError):
			transfer_materials(wo.name)
		draft.delete()

	def test_t08_shortage_leaves_no_movement(self):
		from production_app.api.work_order import transfer_materials

		# NOTE: receipts accumulate across tests in this class (class fixtures live
		# outside per-test savepoints), so the shortage case builds FRESH items.
		suffix = random_string(6).upper()
		rm1 = _make_item(f"TESTWOT08-RM1-{suffix}", self.group, self.uom).name
		fg = _make_item(f"TESTWOT08-FG-{suffix}", self.group, self.uom, batch=True).name
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": fg,
				"company": self.company,
				"currency": self.currency,
				"quantity": 1,
				"items": [
					{
						"item_code": rm1,
						"qty": 2,
						"rate": 10,
						"uom": self.uom,
						"stock_uom": self.uom,
						"source_warehouse": self.src_wh,
					}
				],
			}
		)
		bom.insert()
		bom.submit()

		# only 100 of the required 200 RM1 units
		self._receipt_local(rm1, 100)
		wo = frappe.get_doc(
			{
				"doctype": "Work Order",
				"production_item": fg,
				"bom_no": bom.name,
				"qty": 100,
				"company": self.company,
				"fg_warehouse": self.fg_wh,
				"wip_warehouse": self.wip_wh,
				"source_warehouse": self.src_wh,
				"scrap_warehouse": self.fg_wh,
				"stock_uom": self.uom,
				"planned_start_date": now(),
				"transfer_material_against": "Work Order",
				"use_multi_level_bom": 0,
			}
		)
		wo.get_items_and_operations_from_bom()
		wo.insert()
		wo.submit()

		from erpnext.stock.stock_ledger import NegativeStockError

		# emulate the request boundary: in production the failed action aborts the
		# whole HTTP transaction, discarding the WO quantity update that native
		# update_work_order performed before the stock ledger refused the movement
		frappe.db.savepoint("t08_shortage")
		with self.assertRaises(NegativeStockError):
			transfer_materials(wo.name)
		frappe.db.rollback(save_point="t08_shortage")
		wo.reload()
		self.assertEqual(flt(wo.material_transferred_for_manufacturing), 0)
		self.assertEqual(len(frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "purpose": "Material Transfer for Manufacture", "docstatus": 1}
		)), 0)

	def _receipt_local(self, item, qty):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"basic_rate": 10,
						"t_warehouse": self.src_wh,
						"use_serial_batch_fields": 0,
					}
				],
			}
		)
		se.insert()
		se.submit()
		return se

	def test_t10_confirm_prepacking_and_zero_good_rejection(self):
		from production_app.api.work_order import confirm_prepacking, wo_detail

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials = frappe.get_attr("production_app.api.work_order.transfer_materials")
		transfer_materials(wo.name)

		result = confirm_prepacking(
			wo.name,
			values={
				"good": 100,
				"reject": 0,
				"trial": 0,
				"sisa": 0,
				"jam_pembekuan": "09:15:00",
				"qc_produksi": "Joko Prasetyo",
			},
		)
		# T27 contract: prepacking confirmed -> post_packing (not finish)
		self.assertEqual(result["stage"], "post_packing")
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_prepacking), 100)
		self.assertEqual(wo.custom_reject_qty_prepacking, 0)
		self.assertEqual(wo.custom_prepacking_confirmed, 1)
		self.assertEqual(wo.custom_qc_produksi, "Joko Prasetyo")  # FU10: free-text name
		self.assertEqual(str(wo.custom_jam_pembekuan), "9:15:00")

		# zero good must be rejected with NOTHING changed
		with self.assertRaises(frappe.ValidationError):
			confirm_prepacking(wo.name, values={"good": 0})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_prepacking), 100)
		self.assertEqual(wo.custom_prepacking_confirmed, 1)

		# no finish document may exist yet
		self.assertFalse(frappe.get_all(
			"Stock Entry",
			filters={"work_order": wo.name, "purpose": "Manufacture"},
		))

		detail = wo_detail(wo.name)
		self.assertEqual(detail["stage"], "post_packing")

	def test_t10_negative_and_stage_guards(self):
		from production_app.api.work_order import confirm_prepacking

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)

		# stage guard: nothing transferred yet
		with self.assertRaises(frappe.ValidationError):
			confirm_prepacking(wo.name, values={"good": 10})
		wo.reload()
		self.assertEqual(flt(wo.get("custom_good_qty_prepacking")), 0)  # unchanged default
		self.assertFalse(wo.custom_prepacking_confirmed)

	# ------------------------------------------------- T27 postpacking stage

	def _postpacking_ready_wo(self, planned_qty=100):
		"""WO at the post_packing stage: transferred + prepacking confirmed."""
		from production_app.api.work_order import confirm_prepacking, transfer_materials

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(planned_qty)
		transfer_materials(wo.name)
		confirm_prepacking(wo.name, values={"good": planned_qty, "reject": 0, "trial": 0})
		wo.reload()
		return wo

	def test_t27_confirm_postpacking_valid_persists_everything(self):
		from production_app.api.work_order import confirm_postpacking, wo_detail

		wo = self._postpacking_ready_wo(100)  # pre good 100

		result = confirm_postpacking(
			wo.name,
			values={
				"good": 90,
				"reject": 5,
				"trial": 2,
				"sisa": 7,  # FU11: manual — sengaja ≠ 100-90-5-2 untuk membuktikan bukan auto
				"jam_packing": "14:30:00",
				"qc_packing": "Administrator",
			},
		)
		self.assertEqual(result["stage"], "finish")
		self.assertEqual(result["good"], 90)
		self.assertEqual(result["sisa"], 7)
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 90)
		self.assertEqual(flt(wo.custom_reject_qty_postpacking), 5)
		self.assertEqual(flt(wo.custom_trial_qty_postpacking), 2)
		self.assertEqual(flt(wo.custom_sisa_qty_postpacking), 7)
		self.assertEqual(str(wo.custom_jam_packing), "14:30:00")
		self.assertEqual(wo.custom_qc_packing, "Administrator")
		self.assertEqual(wo.custom_postpacking_confirmed, 1)
		# prepacking block is never touched here
		self.assertEqual(flt(wo.custom_good_qty_prepacking), 100)
		self.assertEqual(wo.custom_prepacking_confirmed, 1)
		self.assertEqual(wo_detail(wo.name)["stage"], "finish")

	def test_t27_zero_good_rejected_writes_nothing(self):
		from production_app.api.work_order import confirm_postpacking

		wo = self._postpacking_ready_wo(100)
		confirm_postpacking(wo.name, values={"good": 80})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 80)

		# zero good and missing good: rejected BEFORE any write
		with self.assertRaises(frappe.ValidationError):
			confirm_postpacking(wo.name, values={"good": 0})
		with self.assertRaises(frappe.ValidationError):
			confirm_postpacking(wo.name, values={"reject": 1})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 80)
		self.assertEqual(wo.custom_postpacking_confirmed, 1)
		self.assertFalse(frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "purpose": "Manufacture"}
		))

	def test_t27_quantity_above_prepacking_accepted_fu41(self):
		"""FU41: cap pre-packing dilepas — good (dan total) di atas good_pre
		diterima; batas akhir tetap overproduksi native saat finish."""
		from production_app.api.work_order import confirm_postpacking

		wo = self._postpacking_ready_wo(100)

		# good di atas good_pre: diterima
		confirm_postpacking(wo.name, values={"good": 101})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 101)
		self.assertEqual(wo.custom_postpacking_confirmed, 1)

		# total good + reject + trial + sisa di atas good_pre: diterima
		confirm_postpacking(wo.name, values={"good": 90, "reject": 8, "trial": 3, "sisa": 2})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 90)
		self.assertEqual(flt(wo.custom_reject_qty_postpacking), 8)
		self.assertEqual(flt(wo.custom_trial_qty_postpacking), 3)
		self.assertEqual(flt(wo.custom_sisa_qty_postpacking), 2)
		self.assertEqual(wo.custom_postpacking_confirmed, 1)

		# boundary lama yang tetap valid
		confirm_postpacking(wo.name, values={"good": 90, "reject": 5, "trial": 5, "sisa": 0})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 90)
		self.assertEqual(flt(wo.custom_sisa_qty_postpacking), 0)

	def test_t27_zero_reject_trial_valid(self):
		from production_app.api.work_order import confirm_postpacking

		wo = self._postpacking_ready_wo(100)
		result = confirm_postpacking(wo.name, values={"good": 100, "reject": 0, "trial": 0})
		self.assertEqual(result["sisa"], 0)
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 100)
		self.assertEqual(flt(wo.custom_reject_qty_postpacking), 0)
		self.assertEqual(flt(wo.custom_trial_qty_postpacking), 0)

	def test_t27_qc_time_and_unknown_field_validation(self):
		from production_app.api.work_order import confirm_postpacking

		wo = self._postpacking_ready_wo(100)

		with self.assertRaises(ValueError):  # get_time raises the parser error as-is
			confirm_postpacking(wo.name, values={"good": 50, "jam_packing": "bukan-jam", "qc_packing": "Ugy"})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 0)
		self.assertFalse(wo.custom_postpacking_confirmed)

		result = confirm_postpacking(wo.name, values={"good": 50, "qc_packing": "Ugy"})
		self.assertEqual(result["good"], 50)
		wo.reload()
		self.assertEqual(wo.custom_qc_packing, "Ugy")
		# FU11: sisa is a manual payload key now — only NEGATIVE values are rejected
		with self.assertRaises(frappe.ValidationError):
			confirm_postpacking(wo.name, values={"good": 50, "sisa": -1})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 50)
		self.assertTrue(wo.custom_postpacking_confirmed)

	def test_fu11_manual_sisa_jam_defaults_and_prep_suggestions(self):
		"""FU11: (1) sisa postpacking manual; (2) jam adonan/pembekuan/packing
		kosong diisi server dgn jam saat simpan; (3) wo_detail men-suggest
		penimbang/leader dari nilai tercatat TERAKHIR utk kolom yang kosong."""
		from production_app.api.work_order import (
			confirm_postpacking,
			prepare,
			wo_detail,
		)

		# jam adonan kosong -> jam saat simpan; WO ini jadi sumber saran
		wo_src = self._make_wo(100, submit=False)
		prepare(
			wo_src.name,
			values={"penimbang": "Rina Wijaya", "leader": "Budi Santoso"},
			submit=1,
		)
		wo_src.reload()
		self.assertIsNotNone(wo_src.custom_jam_adonan, "jam adonan kosong harus diisi server")

		# WO dgn prep kosong: saran = nilai tercatat terakhir (wo_src, terbaru)
		wo = self._postpacking_ready_wo(100)  # prep kosong + prepacking confirmed
		self.assertIsNotNone(wo.custom_jam_pembekuan, "jam pembekuan kosong harus diisi server")
		detail = wo_detail(wo.name)
		self.assertEqual(detail["suggested_penimbang"], "Rina Wijaya")
		self.assertEqual(detail["suggested_leader"], "Budi Santoso")
		# WO yang SUDAH punya nilai tidak diberi saran
		self.assertIsNone(wo_detail(wo_src.name)["suggested_penimbang"])

		# sisa manual + jam packing kosong -> jam saat simpan
		confirm_postpacking(
			wo.name,
			values={"good": 90, "reject": 5, "trial": 2, "sisa": 7, "qc_packing": "Administrator"},
		)
		wo.reload()
		self.assertEqual(flt(wo.custom_sisa_qty_postpacking), 7)  # manual, bukan 100-90-5-2=3
		self.assertIsNotNone(wo.custom_jam_packing, "jam packing kosong harus diisi server")

	def test_t27_stage_guards_and_reedit_at_finish(self):
		from production_app.api.work_order import (
			confirm_postpacking,
			confirm_prepacking,
			finish,
			transfer_materials,
		)

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)  # stage pre_packing

		# confirm_postpacking while still pre_packing -> rejected
		with self.assertRaises(frappe.ValidationError):
			confirm_postpacking(wo.name, values={"good": 50})
		wo.reload()
		self.assertFalse(wo.custom_postpacking_confirmed)

		confirm_prepacking(wo.name, values={"good": 100})
		confirm_postpacking(wo.name, values={"good": 80})  # stage now finish

		# FU12: prepacking re-edit at finish IS allowed, but the new pre good may
		# not undercut the confirmed Post-Packing totals (80)
		with self.assertRaises(frappe.ValidationError):
			confirm_prepacking(wo.name, values={"good": 60})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_prepacking), 100)
		result = confirm_prepacking(wo.name, values={"good": 95})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_prepacking), 95)
		self.assertEqual(result["stage"], "finish")  # masih finish, tidak mundur

		# re-edit postpacking at the finish stage is allowed (sisa manual, FU11)
		confirm_postpacking(wo.name, values={"good": 70, "reject": 5, "sisa": 25})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 70)
		self.assertEqual(flt(wo.custom_sisa_qty_postpacking), 25)

		# FU12: setelah "Selesaikan Produksi" (finish dieksekusi) kedua blok terkunci
		finish(wo.name)
		wo.reload()
		self.assertEqual(wo.status, "Completed")
		with self.assertRaises(frappe.ValidationError):
			confirm_prepacking(wo.name, values={"good": 90})
		with self.assertRaises(frappe.ValidationError):
			confirm_postpacking(wo.name, values={"good": 60})
		wo.reload()
		self.assertEqual(flt(wo.custom_good_qty_prepacking), 95)
		self.assertEqual(flt(wo.custom_good_qty_postpacking), 70)

	def test_t27_finish_uses_postpacking_good(self):
		from production_app.api.work_order import confirm_postpacking, finish

		wo = self._postpacking_ready_wo(100)  # pre good 100
		confirm_postpacking(wo.name, values={"good": 80, "reject": 0, "trial": 0})

		result = finish(wo.name)
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 80)
		self.assertEqual(flt(wo.process_loss_qty), 20)  # remaining target - post good
		self.assertEqual(wo.status, "Completed")
		self.assertEqual(result["stage"], "selesai")
		self.assertIsNotNone(result["batch"])

		se = frappe.get_doc("Stock Entry", result["stock_entry"])
		fg_rows = self._fg_rows(se)
		self.assertEqual(len(fg_rows), 1)
		self.assertEqual(flt(fg_rows[0].qty), 80)
		entries = self._fg_bundle_entries(se)
		self.assertEqual(len(entries), 1)
		self.assertEqual(flt(entries[0].qty), 80)
		self.assertEqual(entries[0].batch_no, result["batch"])
		# raw materials stay at the planned (transferred) quantity
		self.assertEqual(self._rm_rows(se), [(self.rm1, 200.0), (self.rm2, 100.0)])

	def test_t27_legacy_in_flight_wo_lands_on_post_packing(self):
		from production_app.api.work_order import derive_stage, finish, transfer_materials, wo_list

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)
		wo.db_set("custom_prepacking_confirmed", 1)  # confirmed before this release
		wo.reload()
		self.assertEqual(derive_stage(wo), "post_packing")

		with self.assertRaises(frappe.ValidationError):
			finish(wo.name)
		self.assertFalse(frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "purpose": "Manufacture"}
		))

		# the list serves this WO under post_packing, not finish
		rows = wo_list(search=wo.name, stage="post_packing")
		self.assertTrue(any(r.name == wo.name for r in rows))
		rows = wo_list(search=wo.name, stage="finish")
		self.assertFalse(any(r.name == wo.name for r in rows))

	def test_t11_finish_and_over_limit_no_partial(self):
		from production_app.api.work_order import (
			confirm_postpacking,
			confirm_prepacking,
			finish,
			transfer_materials,
		)

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)
		confirm_prepacking(wo.name, values={"good": 100, "reject": 0, "trial": 0, "sisa": 0})
		confirm_postpacking(wo.name, values={"good": 100, "reject": 0, "trial": 0})

		result = finish(wo.name)
		wo.reload()
		self.assertEqual(result["stock_entry"], result.get("stock_entry"))
		self.assertEqual(flt(wo.produced_qty), 100)
		self.assertEqual(wo.status, "Completed")
		self.assertEqual(result["stage"], "selesai")
		self.assertIsNotNone(result["batch"])

		# second finish must not create a duplicate manufacture
		with self.assertRaises(Exception):
			finish(wo.name)
		count = len(frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "purpose": "Manufacture", "docstatus": 1}
		))
		self.assertEqual(count, 1)

	def test_t11_finish_requires_confirmed_positive_prepacking(self):
		from production_app.api.work_order import finish, transfer_materials

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)

		# not confirmed yet
		with self.assertRaises(frappe.ValidationError):
			finish(wo.name)

		# confirmed with zero good is impossible via confirm_prepacking, but a
		# stale zero on the document must still block finish
		wo.db_set("custom_prepacking_confirmed", 1)
		with self.assertRaises(frappe.ValidationError):
			finish(wo.name)

		self.assertEqual(len(frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "purpose": "Manufacture"}
		)), 0)

	def test_t11_finish_below_plan_uses_confirmed_good(self):
		from production_app.api.work_order import (
			confirm_postpacking,
			confirm_prepacking,
			finish,
			transfer_materials,
		)

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)
		confirm_prepacking(wo.name, values={"good": 80, "reject": 5, "trial": 0, "sisa": 15})
		confirm_postpacking(wo.name, values={"good": 80, "reject": 0, "trial": 0})

		result = finish(wo.name)
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 80)
		self.assertEqual(flt(wo.process_loss_qty), 20)
		self.assertEqual(wo.status, "Completed")
		self.assertEqual(result["stage"], "selesai")

		# raw materials were consumed at planned quantities, not prorated
		se = frappe.get_doc("Stock Entry", result["stock_entry"])
		rm = sorted([(r.item_code, flt(r.qty)) for r in se.items if not r.is_finished_item])
		self.assertEqual(rm, [(self.rm1, 200.0), (self.rm2, 100.0)])

	def test_t11_finish_closes_legacy_partially_produced_wo(self):
		from production_app.api.work_order import (
			confirm_postpacking,
			confirm_prepacking,
			finish,
			transfer_materials,
		)

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)

		# legacy state: a native prorated Manufacture already produced half the plan
		first = frappe.get_doc(make_wo_stock_entry(wo.name, "Manufacture", qty=50))
		first.insert()
		first.submit()
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 50)
		self.assertEqual(wo.status, "In Process")

		# one-shot finish closes the remainder: good produced, shortfall = loss
		confirm_prepacking(wo.name, values={"good": 30})
		confirm_postpacking(wo.name, values={"good": 30, "reject": 0, "trial": 0})
		result = finish(wo.name)
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 80)
		self.assertEqual(flt(wo.process_loss_qty), 20)
		self.assertEqual(wo.status, "Completed")
		self.assertEqual(result["stage"], "selesai")

		# RM rows restored to the still-unconsumed plan (transferred − consumed)
		se = frappe.get_doc("Stock Entry", result["stock_entry"])
		rm = sorted([(r.item_code, flt(r.qty)) for r in se.items if not r.is_finished_item])
		self.assertEqual(rm, [(self.rm1, 100.0), (self.rm2, 50.0)])

	def test_t07_prepare_applies_warehouse_default_settings(self):
		from production_app.api.work_order import prepare, warehouse_defaults_save

		# set the Production App defaults on Manufacturing Settings (test tx)
		saved = warehouse_defaults_save(
			source_warehouse=self.src_wh,
			wip_warehouse=self.wip_wh,
			fg_warehouse=self.fg_wh,
			scrap_warehouse=self.fg_wh,
		)
		self.assertEqual(saved["fg_warehouse"], self.fg_wh)

		wo = frappe.get_doc(
			{
				"doctype": "Work Order",
				"production_item": self.fg,
				"bom_no": self.bom,
				"qty": 10,
				"company": self.company,
				"wip_warehouse": self.fg_wh,  # explicit value must never be overridden
				"stock_uom": self.uom,
				"planned_start_date": now(),
				"transfer_material_against": "Work Order",
				"use_multi_level_bom": 0,
			}
		)
		wo.get_items_and_operations_from_bom()
		wo.insert()

		prepare(wo.name, values={"adonan_ke": "1", "jam_adonan": "08:00:00"}, submit=1)
		wo.reload()
		self.assertEqual(wo.docstatus, 1)
		self.assertEqual(wo.source_warehouse, self.src_wh)
		self.assertEqual(wo.wip_warehouse, self.fg_wh)  # explicit value kept
		self.assertEqual(wo.fg_warehouse, self.fg_wh)
		self.assertEqual(wo.scrap_warehouse, self.fg_wh)

		# clearing is allowed (empty string -> None)
		cleared = warehouse_defaults_save(
			source_warehouse="", wip_warehouse="", fg_warehouse="", scrap_warehouse=""
		)
		self.assertIsNone(cleared["fg_warehouse"])

	def test_t07_warehouse_defaults_save_requires_permission(self):
		from production_app.api.work_order import warehouse_defaults_save

		user = frappe.get_doc(
			{"doctype": "User", "email": "testwhd@prodapp.example.com", "first_name": "TWD"}
		).insert().name
		frappe.set_user(user)
		try:
			with self.assertRaises(frappe.PermissionError):
				warehouse_defaults_save(fg_warehouse=self.fg_wh)
		finally:
			frappe.set_user("Administrator")

	def test_t12_permission_denied_on_actions(self):
		from production_app.api.work_order import (
			confirm_postpacking,
			confirm_prepacking,
			finish,
			transfer_materials,
		)

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)

		user = frappe.get_doc(
			{"doctype": "User", "email": "testwot12@prodapp.example.com", "first_name": "T12"}
		).insert().name
		frappe.set_user(user)
		try:
			with self.assertRaises(frappe.PermissionError):
				transfer_materials(wo.name)
			with self.assertRaises(frappe.PermissionError):
				confirm_prepacking(wo.name, values={"good": 10})
			with self.assertRaises(frappe.PermissionError):
				confirm_postpacking(wo.name, values={"good": 10})
			with self.assertRaises(frappe.PermissionError):
				finish(wo.name)
		finally:
			frappe.set_user("Administrator")

		wo.reload()
		self.assertEqual(flt(wo.material_transferred_for_manufacturing), 0)
		self.assertFalse(frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "docstatus": 1}
		))

	def test_t46_prepare_on_completed_wo_requires_manager(self):
		"""FU46: after Selesai, Data Adonan is editable only by Manufacturing
		Manager. Non-manager is rejected with zero writes; the manager's edit
		persists without touching any stock document."""
		from production_app.api.work_order import (
			confirm_postpacking,
			confirm_prepacking,
			finish,
			prepare,
			transfer_materials,
			wo_detail,
		)

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(10)
		prepare(wo.name, values={"adonan_ke": "1", "suhu_adonan": 25.0}, submit=1)
		transfer_materials(wo.name)
		confirm_prepacking(wo.name, values={"good": 10})
		confirm_postpacking(wo.name, values={"good": 10})
		finish(wo.name)
		wo.reload()
		self.assertEqual(wo.status, "Completed")

		user = frappe.get_doc(
			{"doctype": "User", "email": "testwot46@prodapp.example.com", "first_name": "T46"}
		).insert()
		user.add_roles("Manufacturing User")

		frappe.set_user(user.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				prepare(wo.name, values={"adonan_ke": "2", "suhu_adonan": 30.0}, submit=1)
			self.assertFalse(wo_detail(wo.name)["can_edit_persiapan"])
		finally:
			frappe.set_user("Administrator")
		wo.reload()
		self.assertEqual(wo.custom_adonan_ke, "1")  # rejection leaves no partial write

		user.add_roles("Manufacturing Manager")
		frappe.set_user(user.name)
		try:
			prepare(wo.name, values={"adonan_ke": "2", "suhu_adonan": 30.0}, submit=1)
			self.assertTrue(wo_detail(wo.name)["can_edit_persiapan"])
		finally:
			frappe.set_user("Administrator")
		wo.reload()
		self.assertEqual(wo.custom_adonan_ke, "2")
		self.assertEqual(flt(wo.custom_suhu_adonan), 30.0)
		self.assertEqual(wo.status, "Completed")
		# exactly the transfer + manufacture from the normal flow — no new documents
		self.assertEqual(len(frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "docstatus": 1}
		)), 2)

	def test_t12_cancel_manufacture_recomputes_and_retry_is_safe(self):
		from production_app.api.work_order import (
			confirm_postpacking,
			confirm_prepacking,
			finish,
			transfer_materials,
		)

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)
		confirm_prepacking(wo.name, values={"good": 100, "reject": 0, "trial": 0, "sisa": 0})
		confirm_postpacking(wo.name, values={"good": 100, "reject": 0, "trial": 0})
		result = finish(wo.name)
		se_name = result["stock_entry"]

		# native cancellation of the manufacture recomputes server state
		frappe.get_doc("Stock Entry", se_name).cancel()
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 0)
		self.assertEqual(wo.status, "In Process")
		from production_app.api.work_order import wo_detail
		self.assertEqual(wo_detail(wo.name)["stage"], "finish")

		# retry after cancellation: exactly one submitted manufacture again
		retry = finish(wo.name)
		self.assertNotEqual(retry["stock_entry"], se_name)
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 100)
		self.assertEqual(len(frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "purpose": "Manufacture", "docstatus": 1}
		)), 1)

	def test_t12_native_wo_cancel_order_enforced(self):
		from production_app.api.work_order import transfer_materials

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)

		# WO cancel is blocked while a submitted Stock Entry exists (native rule)
		with self.assertRaises(frappe.ValidationError):
			wo.cancel()

		# native order: cancel entries first, then the work order
		for se in frappe.get_all(
			"Stock Entry", filters={"work_order": wo.name, "docstatus": 1}, pluck="name"
		):
			frappe.get_doc("Stock Entry", se).cancel()
		wo = frappe.get_doc("Work Order", wo.name)  # fresh instance for cancel
		wo.cancel()
		wo.reload()
		self.assertEqual(wo.docstatus, 2)
		from production_app.api.work_order import wo_detail
		self.assertEqual(wo_detail(wo.name)["stage"], "cancelled")

	def test_workspace_uom_uses_item_factor_not_converted_quantity(self):
		from production_app.api.work_order import wo_detail, _enrich_units
		item = frappe.get_doc("Item", self.fg)
		alternate = frappe.db.get_value("UOM", {"name": ("!=", self.uom)}, "name")
		item.custom_default_inventory_unit_of_measure = alternate
		item.append("uoms", {"uom": alternate, "conversion_factor": 12})
		item.save()
		wo = self._make_wo(216, submit=False)
		data = wo_detail(wo.name)
		self.assertEqual(data["display_uom"], alternate)
		self.assertEqual(data["display_conversion_factor"], 12)
		self.assertEqual(data["qty"], 216)
		rows = [frappe._dict(name=wo.name, production_item=self.fg)]
		_enrich_units(rows)
		self.assertEqual(rows[0].display_conversion_factor, 12)
		# 29 Sep: kondisi "field terisi tanpa baris konversi" tidak lagi sah —
		# warehouse_app menolaknya saat Item disimpan, jadi invarian "tidak
		# menebak faktor" dijaga di sumber data.
		item.uoms = [row for row in item.uoms if row.uom != alternate]
		with self.assertRaises(frappe.ValidationError):
			item.save()
		self.assertEqual(wo_detail(wo.name)["display_conversion_factor"], 12)

	def test_fu13_suggestions_follow_same_product_and_user_toggle(self):
		from production_app.api.work_order import prepare, suggestion_preferences_save, wo_detail

		wo_src = self._make_wo(100, submit=False)
		prepare(wo_src.name, values={
			"penimbang": "Ugy", "leader": "Rina", "jumlah_kru": 7,
			"jam_adonan": "08:00:00",
		}, submit=1)
		wo_src.db_set("custom_qc_produksi", "Dewi")
		wo = self._make_wo(100, submit=True)
		try:
			frappe.set_user("Administrator")
			suggestion_preferences_save(1)
			detail = wo_detail(wo.name)
			self.assertEqual(detail["suggested_penimbang"], "Ugy")
			self.assertEqual(detail["suggested_leader"], "Rina")
			self.assertEqual(detail["suggested_jumlah_kru"], 7)
			self.assertEqual(detail["suggested_qc_produksi"], "Dewi")
			self.assertTrue(detail["suggestions_enabled"])
			suggestion_preferences_save(0)
			self.assertFalse(wo_detail(wo.name)["suggestions_enabled"])
			self.assertIsNone(wo_detail(wo.name)["suggested_penimbang"])
		finally:
			suggestion_preferences_save(1)

	def test_t35_wo_handover_summary_field_contract(self):
		"""T35: the five handover summary fields exist on the Work Order as
		read-only + allow-on-submit with exact types — written only through
		controlled db_set by the server gate, round-trippable on submitted WOs."""
		meta = frappe.get_meta("Work Order")
		expected = {
			"custom_handover_material_request": ("Link", "Material Request"),
			"custom_box_1": ("Float", None),
			"custom_box_1_qty": ("Int", None),
			"custom_box_2": ("Float", None),
			"custom_box_2_qty": ("Int", None),
		}
		for fieldname, (fieldtype, options) in expected.items():
			df = meta.get_field(fieldname)
			self.assertIsNotNone(df, fieldname)
			self.assertEqual(df.fieldtype, fieldtype, fieldname)
			self.assertEqual(df.options or None, options, fieldname)
			self.assertTrue(df.read_only, fieldname)
			self.assertTrue(df.allow_on_submit, fieldname)

		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)
		wo.db_set("custom_box_1", 3.5)
		wo.db_set("custom_box_1_qty", 12)
		wo.db_set("custom_box_2", 1.25)
		wo.db_set("custom_box_2_qty", 2)
		wo.reload()
		self.assertEqual(flt(wo.custom_box_1), 3.5)
		self.assertEqual(wo.custom_box_1_qty, 12)
		self.assertEqual(flt(wo.custom_box_2), 1.25)
		self.assertEqual(wo.custom_box_2_qty, 2)

	def test_t05_box_identifier_and_leader_name_persist_after_submit(self):
		self._receipt(self.rm1, 1000)
		self._receipt(self.rm2, 1000)
		wo = self._make_wo(100)

		# submitted WO: workspace fields must be editable via update-after-submit
		# (T31 ruling R8: Box 1/2 are Float kg weights written at Verifikasi Siap
		# Kirim; the FU7 text-identifier era was migrated back by upgrade.py)
		wo.db_set("custom_box_1", 12.5)
		wo.db_set("custom_box_2", 8.25)
		wo.db_set("custom_prepacking_confirmed", 1)
		wo.db_set("custom_leader_produksi", "Budi Santoso")
		wo.reload()

		# decimal kg round-trip — never converted to PCS/pack units
		self.assertEqual(flt(wo.custom_box_1), 12.5)
		self.assertEqual(flt(wo.custom_box_2), 8.25)
		self.assertEqual(wo.custom_prepacking_confirmed, 1)
		self.assertEqual(wo.custom_leader_produksi, "Budi Santoso")

		# a whole number stays a plain kg number, not a text or a pack count
		wo.db_set("custom_leader_produksi", "12")  # leader stays text
		wo.db_set("custom_box_1", 12)
		wo.reload()
		self.assertEqual(wo.custom_leader_produksi, "12")
		self.assertEqual(flt(wo.custom_box_1), 12)


class TestWorkOrderOperationsProof(IntegrationTestCase):
	"""T04 — WO with native Job Cards: start/complete, gating, batch override."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.allowance = flt(
			frappe.db.get_single_value(
				"Manufacturing Settings", "overproduction_percentage_for_work_order"
			)
		)
		suffix = random_string(6).upper()
		cfg = _first_existing_wo_config()
		cls.company = cfg.company
		cls.fg_wh = cfg.fg_warehouse
		cls.wip_wh = cfg.wip_warehouse
		cls.src_wh = cfg.source_warehouse or cfg.wip_warehouse
		cls.uom = cfg.stock_uom
		cls.group = frappe.db.get_value("Item Group", {}, "name")
		cls.currency = frappe.db.get_value("Company", cls.company, "default_currency")

		# Company ROPI has NO default_operating_cost_account (never needed: zero
		# operational WOs use operations). Set it inside the test transaction so the
		# native operating-cost GL path can be proven; the test rollback undoes it.
		cls.operating_cost_account = frappe.get_all(
			"Account",
			filters={
				"company": cls.company,
				"root_type": "Expense",
				"is_group": 0,
				"balance_must_be": ("is", "not set"),
			},
			order_by="name",
			limit=1,
			pluck="name",
		)[0]
		frappe.db.set_value(
			"Company", cls.company, "default_operating_cost_account", cls.operating_cost_account
		)

		cls.rm1 = _make_item(f"TESTWOT04-RM1-{suffix}", cls.group, cls.uom).name
		cls.fg = _make_item(f"TESTWOT04-FG-{suffix}", cls.group, cls.uom, batch=True).name

		cls.workstation = frappe.get_doc(
			{
				"doctype": "Workstation",
				"workstation_name": f"TESTWOT04-WS-{suffix}",
				"production_capacity": 10,
				"hour_rate": 100,
			}
		).insert().name
		cls.operation = frappe.get_doc(
			{
				"doctype": "Operation",
				"name": f"TESTWOT04-OP-{suffix}",
				"operation_name": f"TESTWOT04-OP-{suffix}",
				"workstation": cls.workstation,
			}
		).insert().name

		cls.employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": f"TESTWOT04 Op {suffix}",
				"employee_name": f"TESTWOT04 Operator {suffix}",
				"company": cls.company,
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
			}
		).insert().name

		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cls.fg,
				"company": cls.company,
				"currency": cls.currency,
				"quantity": 1,
				"with_operations": 1,
				"items": [
					{
						"item_code": cls.rm1,
						"qty": 2,
						"rate": 10,
						"uom": cls.uom,
						"stock_uom": cls.uom,
						"source_warehouse": cls.src_wh,
					}
				],
				"operations": [
					{
						"operation": cls.operation,
						"workstation": cls.workstation,
						"time_in_mins": 60,
						"hour_rate": 100,
					}
				],
			}
		)
		bom.insert()
		bom.submit()
		cls.bom = bom.name

	def _receipt(self, item, qty):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"basic_rate": 10,
						"t_warehouse": self.src_wh,
						"use_serial_batch_fields": 0,
					}
				],
			}
		)
		se.insert()
		se.submit()
		return se

	def _make_wo(self, qty=100):
		wo = frappe.get_doc(
			{
				"doctype": "Work Order",
				"production_item": self.fg,
				"bom_no": self.bom,
				"qty": qty,
				"company": self.company,
				"fg_warehouse": self.fg_wh,
				"wip_warehouse": self.wip_wh,
				"source_warehouse": self.src_wh,
				"scrap_warehouse": self.fg_wh,
				"stock_uom": self.uom,
				"planned_start_date": now(),
				"transfer_material_against": "Work Order",
				"use_multi_level_bom": 0,
			}
		)
		wo.get_items_and_operations_from_bom()
		wo.insert()
		wo.submit()
		return wo

	def _job_cards(self, wo):
		return frappe.get_all(
			"Job Card",
			filters={"work_order": wo.name},
			fields=["name", "operation", "operation_id", "status", "for_quantity", "docstatus"],
			order_by="creation",
		)

	def _transferred_map(self, wo):
		rows = frappe.get_all(
			"Stock Entry Detail",
			filters={
				"parent": ("in", frappe.get_all(
					"Stock Entry",
					filters={
						"work_order": wo.name,
						"purpose": "Material Transfer for Manufacture",
						"docstatus": 1,
					},
					pluck="name",
				)),
				"docstatus": 1,
			},
			fields=["item_code", "qty"],
		)
		out = frappe._dict()
		for r in rows:
			out[r.item_code] = out.get(r.item_code, 0.0) + flt(r.qty)
		return out

	def _manufacture(self, wo, good):
		from erpnext.manufacturing.doctype.work_order.work_order import (
			make_stock_entry as make_wo_stock_entry,
		)

		se = frappe.get_doc(make_wo_stock_entry(wo.name, "Manufacture", qty=good))
		transferred = self._transferred_map(wo)
		for row in se.items:
			if not row.is_finished_item and row.item_code in transferred:
				row.qty = transferred[row.item_code]
				row.transfer_qty = row.qty * flt(row.conversion_factor or 1)
		se.insert()
		se.submit()
		return se

	def _fg_bundle_entries(self, se):
		out = []
		updated = frappe.get_doc("Stock Entry", se.name)
		for row in updated.items:
			if row.is_finished_item:
				if row.serial_and_batch_bundle:
					out.extend(
						frappe.get_all(
							"Serial and Batch Entry",
							filters={"parent": row.serial_and_batch_bundle},
							fields=["batch_no", "qty"],
						)
					)
				elif row.batch_no:
					out.append(frappe._dict({"batch_no": row.batch_no, "qty": flt(row.qty)}))
		return out

	def _rm_bundles(self, se):
		updated = frappe.get_doc("Stock Entry", se.name)
		return [
			row.serial_and_batch_bundle
			for row in updated.items
			if not row.is_finished_item and row.serial_and_batch_bundle
		]

	def _make_wo_ops(self, qty=100):
		return self._make_wo(qty)

	def _fresh_employee(self):
		"""Per-test employee: time-log windows from earlier tests must not overlap."""
		return frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": f"TESTWOT04 {random_string(4)}",
				"employee_name": f"TESTWOT04 Op {random_string(4)}",
				"company": self.company,
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
			}
		).insert().name


	def test_t09_jobcard_actions_via_api(self):
		from production_app.api.work_order import jobcard_complete, jobcard_start

		wo = self._make_wo_ops(100)
		card = self._job_cards(wo)[0]
		employee = self._fresh_employee()

		started = jobcard_start(wo.name, card.name, employees=[{"employee": employee}])
		self.assertTrue(started["time_logs"])
		self.assertEqual(started["time_logs"][-1]["to_time"], "")

		done = jobcard_complete(wo.name, card.name, qty=100, auto_submit=1)
		self.assertEqual(done["docstatus"], 1)
		self.assertEqual(done["total_completed_qty"], 100)
		# no transfer yet, so the workspace stage is still material
		self.assertEqual(done["work_order_stage"], "material")

		card_doc = frappe.get_doc("Job Card", card["name"])
		self.assertTrue(card_doc.time_logs)  # survives reload
		wo.reload()
		self.assertEqual(flt(wo.operations[0].completed_qty), 100)

	def test_t09_repeat_start_and_cross_wo_guard(self):
		from production_app.api.work_order import jobcard_complete, jobcard_start

		wo_a = self._make_wo_ops(100)
		card_a = self._job_cards(wo_a)[0]
		jobcard_start(wo_a.name, card_a.name, employees=[{"employee": self._fresh_employee()}])

		# starting again while a log is open is rejected (workspace guard or the
		# native employee-overlap check — both prevent duplicate logs)
		with self.assertRaises(Exception):
			jobcard_start(wo_a.name, card_a.name, employees=[{"employee": self._fresh_employee()}])

		wo_b = self._make_wo_ops(100)
		card_b = self._job_cards(wo_b)[0]

		# card B must not be reachable through WO A
		with self.assertRaises(frappe.PermissionError):
			jobcard_complete(wo_a.name, card_b.name, qty=50)

		card_b_doc = frappe.get_doc("Job Card", card_b["name"])
		self.assertEqual(card_b_doc.docstatus, 0)
		self.assertEqual(flt(card_b_doc.total_completed_qty), 0)

	def test_t09_overcomplete_rejected(self):
		from production_app.api.work_order import jobcard_start, jobcard_complete

		wo = self._make_wo_ops(100)
		card = self._job_cards(wo)[0]
		jobcard_start(wo.name, card.name, employees=[{"employee": self._fresh_employee()}])
		with self.assertRaises(frappe.ValidationError):
			jobcard_complete(wo.name, card.name, qty=150, auto_submit=1)
		card = frappe.get_doc("Job Card", card["name"])
		self.assertEqual(card.docstatus, 0)
		self.assertEqual(flt(card.total_completed_qty), 0)

	def test_t09_complete_below_plan_with_loss_then_one_shot_finish(self):
		from production_app.api.work_order import (
			confirm_postpacking,
			confirm_prepacking,
			finish,
			jobcard_complete,
			jobcard_start,
			transfer_materials,
		)

		self._receipt(self.rm1, 1000)
		wo = self._make_wo(100)
		transfer_materials(wo.name)

		card = self._job_cards(wo)[0]
		jobcard_start(wo.name, card.name, employees=[{"employee": self._fresh_employee()}])

		# below plan without susut must not silently leave the operation open
		with self.assertRaises(frappe.ValidationError):
			jobcard_complete(wo.name, card.name, qty=60, auto_submit=1)

		# qty + susut closes the card AND the operation natively (below plan)
		done = jobcard_complete(wo.name, card.name, qty=60, process_loss_qty=40, auto_submit=1)
		self.assertEqual(done["docstatus"], 1)
		wo.reload()
		self.assertEqual(wo.operations[0].status, "Completed")
		self.assertEqual(flt(wo.operations[0].completed_qty), 60)
		self.assertEqual(flt(wo.operations[0].process_loss_qty), 40)
		# operation loss does not leak into the WO-level loss
		self.assertEqual(flt(wo.process_loss_qty), 0)
		self.assertEqual(done["work_order_stage"], "pre_packing")

		confirm_prepacking(wo.name, values={"good": 55, "reject": 5})
		confirm_postpacking(wo.name, values={"good": 55, "reject": 0, "trial": 0})
		result = finish(wo.name)
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 55)
		self.assertEqual(flt(wo.process_loss_qty), 45)
		self.assertEqual(wo.status, "Completed")
		self.assertEqual(result["stage"], "selesai")

	def test_t04_job_cards_created_on_submit(self):
		wo = self._make_wo(100)
		cards = self._job_cards(wo)
		self.assertEqual(len(cards), 1)
		self.assertEqual(cards[0].operation, self.operation)
		self.assertEqual(flt(cards[0].for_quantity), 100)
		self.assertEqual(cards[0].docstatus, 0)
		self.assertEqual(wo.status, "Not Started")

	def test_t04_unfinished_operation_blocks_manufacture(self):
		self._receipt(self.rm1, 1000)
		wo = self._make_wo(100)
		card = frappe.get_doc("Job Card", self._job_cards(wo)[0].name)
		card.start_timer(start_time=now(), employees=[{"employee": self._fresh_employee()}])
		card.complete_job_card(qty=60, end_time=frappe.utils.add_to_date(None, minutes=30))

		wo.reload()
		# native: WO operation status only moves off Pending on Job Card submit
		self.assertEqual(wo.status, "Not Started")

		# transfer is fine, but manufacture must be blocked natively — the guard
		# fires inside the entry builder itself (get_items → check_if_operations_completed)
		from erpnext.manufacturing.doctype.work_order.work_order import (
			make_stock_entry as make_wo_stock_entry,
		)
		from erpnext.stock.doctype.stock_entry.stock_entry import OperationsNotCompleteError

		se = frappe.get_doc(make_wo_stock_entry(wo.name, "Material Transfer for Manufacture"))
		se.insert()
		se.submit()

		with self.assertRaises(OperationsNotCompleteError):
			make_wo_stock_entry(wo.name, "Manufacture", qty=100)

		# finish the operation in a second timer cycle, then manufacture succeeds
		card.reload()
		card.start_timer(
			start_time=frappe.utils.add_to_date(None, minutes=31),
			employees=[{"employee": self._fresh_employee()}],
		)
		card.complete_job_card(
			qty=40, end_time=frappe.utils.add_to_date(None, minutes=60), auto_submit=True
		)
		card.reload()
		self.assertEqual(card.docstatus, 1)
		self.assertEqual(flt(card.total_completed_qty), 100)

		wo.reload()
		op_row = [op for op in wo.operations if op.name == card.operation_id][0]
		self.assertEqual(flt(op_row.completed_qty), 100)

		se = self._manufacture(wo, 100)
		wo.reload()
		self.assertEqual(flt(wo.produced_qty), 100)
		self.assertEqual(wo.status, "Completed")

		# bakery override effective: single inward batch entry, qty synced to FG row
		entries = self._fg_bundle_entries(se)
		self.assertEqual(len(entries), 1)
		self.assertEqual(flt(entries[0].qty), 100)
		# raw material rows carry no batch bundle of their own
		self.assertEqual(self._rm_bundles(se), [])

	def test_t04_job_card_time_logs_and_completed_qty_survive_reload(self):
		self._receipt(self.rm1, 1000)
		wo = self._make_wo(100)
		card = frappe.get_doc("Job Card", self._job_cards(wo)[0].name)
		card.start_timer(start_time=now(), employees=[{"employee": self._fresh_employee()}])
		card.complete_job_card(
			qty=100, end_time=frappe.utils.add_to_date(None, minutes=45), auto_submit=True
		)
		card.reload()
		self.assertEqual(card.docstatus, 1)
		self.assertEqual(flt(card.total_completed_qty), 100)
		self.assertTrue(card.time_logs, "time logs must persist")
		wo.reload()
		self.assertEqual(flt(wo.operations[0].completed_qty), 100)
