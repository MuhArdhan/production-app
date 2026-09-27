# T24/T31/T35 handover mutation actions — create/cancel request, send
#
# Proves, by execution (task-24-brief coverage as amended by T31 R1-R8 and the
# T35 three-lane cutover):
# 1. create_request(work_order, box_1, box_1_qty, box_2, box_2_qty): qty =
#    the Work Order's FULL produced_qty; the box allocation (kg + whole Packs,
#    sum == the server-computed Pack count) is validated BEFORE any write and
#    lands atomically on the Work Order summary (Link + 4 box fields);
#    from_warehouse follows the source setting (SE-derived fallback); a second
#    ACTIVE request is blocked with ZERO writes; a short pool is blocked.
# 2. save_post_packing: retired — compatibility rejection only, zero writes.
# 3. send_handover: moves EXACTLY the requested qty; no postpacking gate;
#    batch-tracked rows carry batch_no; MR NOT stopped (R6); duplicate send
#    blocked; SE posting is the send moment (R1); the WO summary (Link +
#    boxes) is retained through the send and status becomes Terkirim.
# 4. cancel_request: any unsent request (native cancel, reservation released);
#    WO summary cleared; with a submitted SE blocked; produksi denied.
# 5. Permissions: gudang cannot send; produksi cannot create/cancel; bare user
#    denied everything, empty board.
# 6. Errors atomic: insufficient batch stock -> zero partial SE/SLE;
#    unsupported legacy WO -> refused before mutation.
# 7. Native desk SE cancel -> board recomputes (lane returns to request); a
#    re-send works afterwards. Native desk MR cancel clears the WO summary.
# 8. Boxes: kg floats + whole Pack counts persist on the Work Order; never
#    converted to PCS; MR stays a pure request document (no box fields).
# 9. Legacy edge: WO with two Manufacture SEs -> create AND send refuse.
#
# Every record is test-only (T24/t24-prefixed); the Frappe test framework rolls
# the run back. The two real warehouses are never touched.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from MySQLdb import OperationalError as SQLOperationalError
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, now, random_string, today

from erpnext.manufacturing.doctype.work_order.work_order import (
    make_stock_entry as make_wo_stock_entry,
)
from erpnext.stock.doctype.batch.batch import get_batch_qty

from production_app.api.handover import (
    _item_stock,
    cancel_request,
    create_request,
    handover_board,
    save_post_packing,
    send_handover,
    sync_from_material_request,
    sync_from_stock_entry,
)
from production_app.api.work_order import finish, warehouse_defaults_save

PREFIX = "T24"


class TestHandoverActions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		name = frappe.db.get_value(
			"Work Order", {"docstatus": 1, "fg_warehouse": ("is", "set")}, "name",
			order_by="creation desc",
		)
		cfg = frappe.db.get_value("Work Order", name, ["company", "stock_uom", "fg_warehouse"], as_dict=True)
		cls.company = cfg.company
		cls.uom = cfg.stock_uom
		cls.group = frappe.db.get_value("Item Group", {}, "name")
		parent = frappe.db.get_value("Warehouse", cfg.fg_warehouse, "parent_warehouse")
		suffix = random_string(6).upper()

		def warehouse(label):
			return (
				frappe.get_doc(
					{
						"doctype": "Warehouse",
						"warehouse_name": f"{PREFIX} {label} {suffix}",
						"company": cls.company,
						"parent_warehouse": parent,
						"is_group": 0,
					}
				)
				.insert()
				.name
			)

		cls.src_wh = warehouse("Source")
		cls.wip_wh = warehouse("WIP")
		cls.cold_wh = warehouse("Cold Storage")
		cls.target_wh = warehouse("Target")

		cls.rm = cls._make_item(f"{PREFIX}-RM-{suffix}", batch=False)
		cls.fg = cls._make_item(f"{PREFIX}-FG-{suffix}")
		# follow-up 8: a batchless FG item — stock pools at item level
		cls.fg_nb = cls._make_item(f"{PREFIX}-FGNB-{suffix}", batch=False)
		# FU29: a SECOND batchless item — the fu29 drift test ships from its own
		# pool so its target_wh balance cannot leak into T31's pool accounting
		cls.fg_nb2 = cls._make_item(f"{PREFIX}-FGNB2-{suffix}", batch=False)
		# T35: successful fixtures convert exactly (display UOM Pack, factor 5);
		# a separate item has NO Pack conversion for rejection tests
		cls.fg_nopack = cls._make_item(f"{PREFIX}-FGNP-{suffix}")
		cls._set_pack(cls.fg)
		cls._set_pack(cls.fg_nb)
		cls._set_pack(cls.fg_nb2)
		cls.bom = cls._make_bom(cls.fg)
		cls.bom_nb = cls._make_bom(cls.fg_nb)
		cls.bom_nb2 = cls._make_bom(cls.fg_nb2)
		cls.bom_nopack = cls._make_bom(cls.fg_nopack)
		cls._receipt(cls.rm, 20000, cls.src_wh)  # covers the run (no per-test rollback)

		cls.prior_target = frappe.db.get_single_value(
			"Manufacturing Settings", "custom_default_handover_warehouse"
		)
		cls.prior_source = frappe.db.get_single_value(
			"Manufacturing Settings", "custom_default_handover_source_warehouse"
		)
		warehouse_defaults_save(handover_warehouse=cls.target_wh)

		cls.gudang = cls._make_user(f"t24.gudang.{suffix.lower()}@prodapp.example.com", ["Gudang Barang Jadi"])
		cls.prod = cls._make_user(f"t24.prod.{suffix.lower()}@prodapp.example.com", ["Manufacturing User"])
		cls.bare = cls._make_user(f"t24.bare.{suffix.lower()}@prodapp.example.com", [])

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		warehouse_defaults_save(
			handover_warehouse=cls.prior_target, handover_source_warehouse=cls.prior_source
		)  # defensive restore
		super().tearDownClass()  # class-level rollback discards the run

	# ------------------------------------------------------------- fixtures

	@classmethod
	def _set_pack(cls, item_code, factor=5):
		"""T35: the raw conversion the server validates on — display UOM is
		exactly Pack with the given raw factor (never the display fallback)."""
		item = frappe.get_cached_doc("Item", item_code)
		item.custom_default_uom_warehouse = "Pack"
		item.append("uoms", {"uom": "Pack", "conversion_factor": factor})
		item.save()

	@classmethod
	def _make_item(cls, code, batch=True):
		return (
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": cls.group,
					"stock_uom": cls.uom,
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
			.insert()
			.name
		)

	@classmethod
	def _make_bom(cls, fg_item):
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": fg_item,
				"company": cls.company,
				"currency": frappe.db.get_value("Company", cls.company, "default_currency"),
				"quantity": 1,
				"items": [
					{
						"item_code": cls.rm,
						"qty": 1,
						"rate": 10,
						"uom": cls.uom,
						"stock_uom": cls.uom,
						"source_warehouse": cls.src_wh,
					}
				],
			}
		)
		bom.insert()
		bom.submit()
		return bom.name

	@staticmethod
	def _pin_posting(se, posting_time):
		"""Pinned fixture movements post YESTERDAY, so every real-time
		operation (action sends, desk transfers, cancels — all at now()) sorts
		strictly AFTER them in the stock ledger regardless of the wall clock;
		equal-timestamp ties inside the ledger are avoided entirely."""
		se.set_posting_time = 1
		se.posting_date = add_days(today(), -1)
		se.posting_time = posting_time

	@classmethod
	def _receipt(cls, item, qty, wh):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": cls.company,
				"items": [
					{"item_code": item, "qty": qty, "basic_rate": 10, "t_warehouse": wh, "use_serial_batch_fields": 0}
				],
			}
		)
		cls._pin_posting(se, "08:00:00")
		se.insert()
		se.submit()
		return se

	@classmethod
	def _make_wo(cls, qty, item=None, bom_no=None):
		bom_by_item = {
			cls.fg_nb: cls.bom_nb,
			cls.fg_nb2: cls.bom_nb2,
			cls.fg_nopack: cls.bom_nopack,
		}
		# unknown items (per-test local fixtures) follow the fu34 swap pattern;
		# a truly local item passes its OWN bom_no (WO validates BOM ↔ item)
		bom_no = bom_no or (cls.bom if item in (None, cls.fg) else bom_by_item.get(item, cls.bom_nb2))
		wo = frappe.get_doc(
			{
				"doctype": "Work Order",
				"production_item": item or cls.fg,
				"bom_no": bom_no,
				"qty": qty,
				"company": cls.company,
				"fg_warehouse": cls.cold_wh,
				"wip_warehouse": cls.wip_wh,
				"source_warehouse": cls.src_wh,
				"scrap_warehouse": cls.cold_wh,
				"stock_uom": cls.uom,
				"planned_start_date": now(),
				"transfer_material_against": "Work Order",
				"use_multi_level_bom": 0,
				"custom_adonan_ke": "1",
			}
		)
		wo.get_items_and_operations_from_bom()
		wo.insert()
		wo.submit()
		return wo

	@classmethod
	def _transfer(cls, wo):
		se = frappe.get_doc(make_wo_stock_entry(wo.name, "Material Transfer for Manufacture"))
		cls._pin_posting(se, "09:00:00")
		se.insert()
		se.submit()
		return se

	@classmethod
	def _manufacture(cls, wo, good, restore_rm=True):
		se = frappe.get_doc(make_wo_stock_entry(wo.name, "Manufacture", qty=good))
		cls._pin_posting(se, "10:00:00")
		if restore_rm:
			for row in se.items:
				if not row.is_finished_item:
					row.qty = flt(wo.qty)  # plan qty (1:1 BOM)
					row.transfer_qty = row.qty
		se.insert()
		se.submit()
		return se

	@classmethod
	def _make_user(cls, email, roles):
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		)
		for role in roles:
			user.append("roles", {"role": role})
		user.insert()
		return user.name

	@classmethod
	def _lot_in_cold(cls, wo):
		return frappe.get_all("Batch", filters={"reference_name": wo.name}, pluck="name")[0]

	@classmethod
	def _lot_ready(cls, qty, item=None, bom_no=None):
		"""WO + transfer + Manufacture -> a lot of `qty` pcs in Cold Storage."""
		wo = cls._make_wo(qty, item=item, bom_no=bom_no)
		cls._transfer(wo)
		cls._manufacture(wo, qty)
		return wo, cls._lot_in_cold(wo)

	def _request(self, wo, box_1=10, box_1_qty=None, box_2=0, box_2_qty=0, box_3=0, box_3_qty=0):
		"""create_request as the gudang actor (the matrix role), with a valid
		one-box allocation by default (factor 5 fixtures)."""
		if not flt(wo.produced_qty):
			wo.reload()  # pick up produced_qty written by the Manufacture step
		packs = box_1_qty if box_1_qty is not None else int(flt(wo.produced_qty) / 5)
		frappe.set_user(self.gudang)
		try:
			return create_request(
				wo.name,
				box_1=box_1,
				box_1_qty=packs,
				box_2=box_2,
				box_2_qty=box_2_qty,
				box_3=box_3,
				box_3_qty=box_3_qty,
			)
		finally:
			frappe.set_user("Administrator")

	def _summary(self, wo_name):
		"""The controlled Work Order handover summary (all eight fields)."""
		return frappe.db.get_value(
			"Work Order", wo_name,
			[
				"custom_handover_material_request", "custom_handover_status",
				"custom_box_1", "custom_box_1_qty", "custom_box_2", "custom_box_2_qty",
				"custom_box_3", "custom_box_3_qty",
			],
			as_dict=True,
		)

	def _assert_zero_write_rejection(self, wo, kwargs, message_part):
		"""A validation failure must throw the expected Indonesian error and
		write NOTHING: no MR row, unchanged Work Order summary."""
		before = self._summary(wo.name)
		count = self._bound_mr_count(wo.name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._request(wo, **kwargs)
		self.assertIn(message_part, str(ctx.exception))
		self.assertEqual(self._bound_mr_count(wo.name), count)
		self.assertEqual(self._summary(wo.name), before)

	def _send(self, mr):
		frappe.set_user(self.prod)
		try:
			return send_handover(mr.name)
		finally:
			frappe.set_user("Administrator")

	def _lot(self, board, wo_name):
		return next(l for l in board["lots"] if l["work_order"] == wo_name)

	def _req(self, board, mr_name):
		return next(r for r in board["requests"] if r["mr"] == mr_name)

	def _bound_mr_count(self, wo_name):
		return len(frappe.get_all("Material Request Item", filters={"custom_work_order": wo_name}))

	@classmethod
	def _desk_mr(cls, wo_name, item_code, qty):
		"""A handover MR built directly (desk-style fixture, T23 pattern)."""
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Material Transfer",
				"company": cls.company,
				"transaction_date": now(),
				"schedule_date": add_days(now(), 1),
				"set_from_warehouse": cls.cold_wh,
				"set_warehouse": cls.target_wh,
				"items": [
					{
						"item_code": item_code,
						"qty": qty,
						"uom": cls.uom,
						"from_warehouse": cls.cold_wh,
						"warehouse": cls.target_wh,
						"schedule_date": add_days(now(), 1),
						"custom_work_order": wo_name,
					}
				],
			}
		)
		mr.insert()
		mr.submit()
		return mr

	# -------------------------------------------------- 1. create_request

	def test_fu34_create_request_never_scans_unrelated_work_orders(self):
		wo, _batch = self._lot_ready(35)
		from production_app.api import handover

		original = handover._wo_lot_rows

		global_scans = 0

		def tracked(wo_names=None):
			nonlocal global_scans
			if wo_names is None:
				global_scans += 1
			return original(wo_names=wo_names)

		with patch.object(handover, "_wo_lot_rows", side_effect=tracked):
			result = self._request(wo)

		self.assertEqual(flt(result["qty"]), 35)
		self.assertEqual(self._bound_mr_count(wo.name), 1)
		self.assertEqual(global_scans, 1)  # one final server-truth response only

	def test_t31_create_request_full_wo_qty_source_and_duplicate_guard(self):
		"""R3: create_request(work_order) requests the WO's FULL produced qty —
		no qty param; from_warehouse falls back to the SE-derived lot warehouse
		when the source setting is unset and follows the setting when set;
		transaction_date is today; a second ACTIVE request is blocked with
		ZERO writes."""
		wo, batch = self._lot_ready(100)

		result = self._request(wo)
		self.assertEqual(flt(result["qty"]), 100)  # == WO produced_qty
		self.assertEqual(result["expected_unit_count"], 20)  # 100 pcs / factor 5
		self.assertEqual((result["box_1"], result["box_1_qty"]), (10, 20))
		mr = frappe.get_doc("Material Request", result["material_request"])
		self.assertEqual(mr.docstatus, 1)
		self.assertEqual(mr.material_request_type, "Material Transfer")
		self.assertEqual(mr.set_from_warehouse, self.cold_wh)  # fallback (unset)
		self.assertEqual(mr.set_warehouse, self.target_wh)
		self.assertEqual(mr.items[0].from_warehouse, self.cold_wh)
		self.assertEqual(mr.items[0].warehouse, self.target_wh)
		self.assertEqual(mr.items[0].custom_work_order, wo.name)
		self.assertEqual(flt(mr.items[0].qty), 100)
		self.assertEqual(str(mr.transaction_date)[:10], today())
		self.assertEqual(flt(mr.custom_box_1), 0)  # pure request: kg boxes come at verify
		board = result["board"]
		req = self._req(board, mr.name)
		self.assertEqual(req["lane"], "request")
		lot = self._lot(board, wo.name)
		self.assertEqual(flt(lot["reserved_qty"]), 100)
		self.assertEqual(flt(lot["available_qty"]), 0)
		self.assertEqual(flt(get_batch_qty(batch, self.cold_wh)), 100)  # request moves no stock

		# duplicate ACTIVE request for the same WO: blocked, zero writes
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._request(wo)
		self.assertIn("sudah punya permintaan aktif", str(ctx.exception))
		self.assertEqual(self._bound_mr_count(wo.name), 1)

		# from_warehouse follows the source setting when set (R2)
		wo2, _ = self._lot_ready(80)
		prior = frappe.db.get_single_value(
			"Manufacturing Settings", "custom_default_handover_source_warehouse"
		)
		try:
			# the save API writes every key — pass the target through unchanged
			warehouse_defaults_save(
				handover_warehouse=self.target_wh, handover_source_warehouse=self.src_wh
			)
			result2 = self._request(wo2)
			mr2 = frappe.get_doc("Material Request", result2["material_request"])
			self.assertEqual(mr2.set_from_warehouse, self.src_wh)
			self.assertEqual(mr2.items[0].from_warehouse, self.src_wh)
			self.assertEqual(flt(result2["qty"]), 80)
		finally:
			warehouse_defaults_save(
				handover_warehouse=self.target_wh, handover_source_warehouse=prior
			)

	def test_fu34_batchless_targeted_lot_counts_sibling_reservations(self):
		suffix = random_string(4).upper()
		fg = self._make_item(f"{PREFIX}-FGPOOL-{suffix}", batch=False)
		self._set_pack(fg)  # successful request needs a valid Pack conversion
		bom = self._make_bom(fg)
		cls = type(self)
		original_bom = cls.bom_nb2
		cls.bom_nb2 = bom
		try:
			wo1 = self._make_wo(60, item=fg)
			self._transfer(wo1)
			self._manufacture(wo1, 60)
			self._request(wo1)

			wo2 = self._make_wo(40, item=fg)
			self._transfer(wo2)
			self._manufacture(wo2, 40)
			pool = flt(_item_stock(fg, self.cold_wh))
			drain_qty = pool - 60
			if drain_qty > 0:
				se = frappe.get_doc(
					{
						"doctype": "Stock Entry",
						"stock_entry_type": "Material Transfer",
						"company": self.company,
						"items": [{
							"item_code": fg,
							"qty": drain_qty,
							"basic_rate": 10,
							"s_warehouse": self.cold_wh,
							"t_warehouse": self.target_wh,
							"use_serial_batch_fields": 0,
						}],
					}
				)
				self._pin_posting(se, "11:10:00")
				se.insert()
				se.submit()

			with self.assertRaises(frappe.ValidationError) as ctx:
				self._request(wo2)
			self.assertIn("tersedia 0", str(ctx.exception))
			self.assertEqual(self._bound_mr_count(wo2.name), 0)
		finally:
			cls.bom_nb2 = original_bom

	def test_t31_pool_short_blocked_zero_writes(self):
		"""R3: the availability guard still holds at the WO's full qty — a
		batchless pool shorter than the produced qty is rejected with the
		diminta/tersedia message and ZERO writes."""
		wo1 = self._make_wo(60, item=self.fg_nb)
		self._transfer(wo1)
		self._manufacture(wo1, 60)
		wo2 = self._make_wo(40, item=self.fg_nb)
		self._transfer(wo2)
		self._manufacture(wo2, 40)
		pool = flt(_item_stock(self.fg_nb, self.cold_wh))
		self.assertGreaterEqual(pool, 100)

		# drain the pool to 50 at the desk (plain transfer, batchless shape)
		drain_qty = pool - 50
		if drain_qty > 0:
			se = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"stock_entry_type": "Material Transfer",
					"company": self.company,
					"items": [
						{
							"item_code": self.fg_nb,
							"qty": drain_qty,
							"basic_rate": 10,
							"s_warehouse": self.cold_wh,
							"t_warehouse": self.target_wh,
							"use_serial_batch_fields": 0,
						}
					],
				}
			)
			self._pin_posting(se, "11:00:00")
			se.insert()
			se.submit()
		self.assertEqual(flt(_item_stock(self.fg_nb, self.cold_wh)), 50)

		with self.assertRaises(frappe.ValidationError) as ctx:
			self._request(wo1)  # produced 60 > available 50
		self.assertIn("diminta", str(ctx.exception))
		self.assertIn("tersedia", str(ctx.exception))
		self.assertEqual(self._bound_mr_count(wo1.name), 0)  # zero writes

	# ------------------------------------- 9. + 6. unsupported / setting guard

	def test_t24_unsupported_legacy_and_unset_setting_refuse_before_mutation(self):
		"""WO with two Manufacture SEs: create AND send refuse with the §4.9
		message (native SE links), zero mutation; a never-manufactured WO and an
		unset target setting also refuse before any write."""
		legacy = self._make_wo(100)
		self._transfer(legacy)
		self._manufacture(legacy, 40, restore_rm=False)
		self._manufacture(legacy, 30, restore_rm=False)

		with self.assertRaises(frappe.ValidationError) as ctx:
			self._request(legacy)
		self.assertIn(legacy.name, str(ctx.exception))
		self.assertEqual(self._bound_mr_count(legacy.name), 0)  # zero mutation

		never = self._make_wo(50)  # submitted but no Manufacture yet
		with self.assertRaises(frappe.ValidationError):
			self._request(never)
		self.assertEqual(self._bound_mr_count(never.name), 0)

		# send-side refusal on the same legacy WO (MR planted as on the desk)
		mr = self._desk_mr(legacy.name, legacy.production_item, 40)
		frappe.set_user(self.prod)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				send_handover(mr.name)
			self.assertIn(legacy.name, str(ctx.exception))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(
			len(frappe.get_all("Stock Entry", filters={"material_request": mr.name})), 0
		)

		# unset target setting -> clear error, zero writes
		prior = frappe.db.get_single_value("Manufacturing Settings", "custom_default_handover_warehouse")
		frappe.db.set_single_value("Manufacturing Settings", "custom_default_handover_warehouse", None)
		try:
			ready, _ = self._lot_ready(50)
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._request(ready)
			self.assertIn("Pengaturan", str(ctx.exception))
			self.assertEqual(self._bound_mr_count(ready.name), 0)
		finally:
			frappe.db.set_single_value("Manufacturing Settings", "custom_default_handover_warehouse", prior)

	# ----------------------------------- 2. T35: box/Pack validation + stub

	def test_t35_save_post_packing_is_a_compatibility_rejection(self):
		"""The retired verify endpoint always rejects with the reload message —
		for any role, before any lock/write — and mutates neither MR nor WO."""
		wo, _ = self._lot_ready(100)
		result = self._request(wo)
		mr = frappe.get_doc("Material Request", result["material_request"])
		before = self._summary(wo.name)

		for user in (self.gudang, self.prod):
			frappe.set_user(user)
			try:
				with self.assertRaises(frappe.ValidationError) as ctx:
					save_post_packing(mr.name, box_1=12.5, box_2=8)
				self.assertIn("Muat ulang halaman", str(ctx.exception))
			finally:
				frappe.set_user("Administrator")

		mr.reload()
		self.assertFalse(mr.custom_postpacking_confirmed)
		self.assertEqual(flt(mr.custom_box_1), 0)
		self.assertEqual(self._summary(wo.name), before)

	def test_t39_expected_unit_count_guard_rejects_invalid_factors(self):
		"""_expected_unit_count validates the RAW _enrich_units fields: the
		count unit is the item's warehouse display UOM — the stock UOM itself
		(EXACT factor 1, no conversion row needed) or any alternate UOM with a
		finite positive factor; whole amounts divide exactly into the count."""
		from production_app.api.handover import _expected_unit_count

		wo, _ = self._lot_ready(100)
		wo = frappe.get_doc("Work Order", wo.name)  # real doc for .precision()
		self.assertEqual(
			_expected_unit_count(
				wo, frappe._dict(display_uom="Pack", stock_uom="Pcs", display_conversion_factor=5), 100
			),
			20,
		)
		# T39 universal: display == stock UOM needs NO conversion row (factor 1
		# is exact — this is the path a plain Pcs item like fg_nopack takes)
		self.assertEqual(
			_expected_unit_count(
				wo, frappe._dict(display_uom="Pcs", stock_uom="Pcs", display_conversion_factor=None), 100
			),
			100,
		)
		for lot in (
			frappe._dict(display_uom="Pack", stock_uom="Pcs", display_conversion_factor=0),
			frappe._dict(display_uom="Pack", stock_uom="Pcs", display_conversion_factor=-5),
			frappe._dict(display_uom="Pack", stock_uom="Pcs", display_conversion_factor=float("inf")),
			frappe._dict(display_uom="Pack", stock_uom="Pcs", display_conversion_factor=float("nan")),
			frappe._dict(display_uom="Pack", stock_uom="Pcs", display_conversion_factor=None),
			frappe._dict(display_uom="Box", stock_uom="Pcs", display_conversion_factor=None),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				_expected_unit_count(wo, lot, 100)
			self.assertIn("konversi", str(ctx.exception))

	def test_t39_invalid_alternate_conversion_rejected_zero_writes(self):
		"""An FG item whose warehouse display UOM is an ALTERNATE UOM without a
		valid conversion row (e.g. Default UOM Gudang = Pack, no Pack factor)
		is rejected with zero writes — the system never guesses a factor."""
		code = self._make_item(f"{PREFIX}-FGWP-{random_string(4).lower()}")
		item = frappe.get_cached_doc("Item", code)
		item.custom_default_uom_warehouse = "Pack"  # no uoms row added
		item.save()
		wo, _ = self._lot_ready(50, item=code, bom_no=self._make_bom(code))
		self._assert_zero_write_rejection(wo, dict(box_1=5, box_1_qty=10), "konversi Pack")

	def test_t39_stock_uom_item_requests_in_pcs(self):
		"""T39 universal path: an item with NO warehouse UOM setting counts in
		its stock UOM (Pcs, factor 1) — requestable without any conversion
		setup; the count sum must equal produced qty exactly, in Pcs."""
		wo, _ = self._lot_ready(22, item=self.fg_nopack)
		frappe.set_user(self.gudang)
		try:
			result = create_request(
				wo.name, box_1=5, box_1_qty=15, box_2=3, box_2_qty=7
			)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(result["unit"], self.uom)
		self.assertEqual(result["expected_unit_count"], 22)
		self.assertEqual((result["box_1_qty"], result["box_2_qty"]), (15, 7))
		self.assertEqual(
			self._summary(wo.name),
			{
				"custom_handover_material_request": result["material_request"],
				"custom_handover_status": "Diminta Gudang",
				"custom_box_1": 5.0,
				"custom_box_1_qty": 15,
				"custom_box_2": 3.0,
				"custom_box_2_qty": 7,
				"custom_box_3": 0.0,
				"custom_box_3_qty": 0,
			},
		)
		# a wrong Pcs sum is a zero-write rejection in the item's own unit
		wo2, _ = self._lot_ready(30, item=self.fg_nopack)
		self._assert_zero_write_rejection(
			wo2, dict(box_1=5, box_1_qty=21, box_2=0, box_2_qty=0), f"tepat 30 {self.uom}"
		)

	def test_t35_fractional_pack_producing_qty_rejected_zero_writes(self):
		"""produced 7 pcs at factor 5 = 1.4 Packs: no whole-Pack allocation can
		match, so create refuses before any write."""
		wo, _ = self._lot_ready(7)
		self._assert_zero_write_rejection(wo, dict(box_1=2, box_1_qty=2), "Pack utuh")

	def test_t35_box_validation_rejects_with_zero_writes(self):
		"""Every box/Pack validation failure throws an Indonesian error and
		writes NOTHING: no Material Request row, unchanged WO summary."""
		wo, _ = self._lot_ready(100)  # expected 20 Pack (factor 5)
		valid = dict(box_1=10, box_1_qty=20, box_2=0, box_2_qty=0)

		# old cached client: no box payload at all
		before = self._summary(wo.name)
		frappe.set_user(self.gudang)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				create_request(wo.name)
			self.assertIn("Box 1", str(ctx.exception))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._bound_mr_count(wo.name), 0)
		self.assertEqual(self._summary(wo.name), before)

		# Box 1 kg <= 0 / Pack <= 0
		self._assert_zero_write_rejection(wo, dict(valid, box_1=0), "Box 1")
		self._assert_zero_write_rejection(wo, dict(valid, box_1=-2), "kg")
		self._assert_zero_write_rejection(wo, dict(valid, box_1="bukan angka"), "kg")
		self._assert_zero_write_rejection(wo, dict(valid, box_1_qty=0), "Box 1")

		# negative / fractional / NaN / infinite Pack input
		self._assert_zero_write_rejection(wo, dict(valid, box_1_qty=-1), "harus bilangan bulat")
		self._assert_zero_write_rejection(wo, dict(valid, box_1_qty=2.5), "harus bilangan bulat")
		self._assert_zero_write_rejection(wo, dict(valid, box_1_qty=float("nan")), "harus bilangan bulat")
		self._assert_zero_write_rejection(wo, dict(valid, box_1_qty=float("inf")), "harus bilangan bulat")

		# Pack sum below / above the expected count
		self._assert_zero_write_rejection(wo, dict(valid, box_1_qty=19), "tepat")
		self._assert_zero_write_rejection(wo, dict(valid, box_1_qty=21), "tepat")

		# Box 2 must be exactly 0/0 or positive/positive
		self._assert_zero_write_rejection(wo, dict(valid, box_2=5, box_2_qty=0), "Box 2")
		self._assert_zero_write_rejection(wo, dict(valid, box_2=0, box_2_qty=5), "Box 2")
		self._assert_zero_write_rejection(wo, dict(valid, box_2=-1, box_2_qty=-1), "kg")

	def test_t35_box_kg_beyond_db_capacity_rejected_zero_writes(self):
		"""A FINITE kg beyond the decimal(18,6) column capacity (e.g. 1e308)
		must die in _finite_kg with the Indonesian bound message — never at the
		db write as a driver error — and write NOTHING (no MR, no summary)."""
		wo, _ = self._lot_ready(100)  # expected 20 Pack (factor 5)
		self._assert_zero_write_rejection(wo, dict(box_1=1e308, box_1_qty=20), "maksimal")
		self._assert_zero_write_rejection(
			wo, dict(box_1=10, box_1_qty=20, box_2=1e308, box_2_qty=1), "maksimal"
		)
		self._assert_zero_write_rejection(
			wo, dict(box_1=10, box_1_qty=20, box_3=1e308, box_3_qty=1), "maksimal"
		)

	def test_w18_box3_split_lands_on_summary_and_rejects_malformed(self):
		"""W18 Box 3 (mirror of Box 2): a valid THREE-box split (sum 12+7+1=20
		Pack) lands atomically on the Work Order summary incl. custom_box_3/qty
		and mirrors onto the board row; a half-filled Box 3, a negative one and
		a wrong three-box sum are zero-write rejections; a plain 2-box call
		(back-compat) leaves custom_box_3/qty at 0."""
		# (b) half-filled Box 3 (either half) -> zero writes (clean WO)
		wo, _ = self._lot_ready(100)  # expected 20 Pack (factor 5)
		self._assert_zero_write_rejection(
			wo, dict(box_1=12.5, box_1_qty=20, box_3=5, box_3_qty=0), "Box 3"
		)
		self._assert_zero_write_rejection(
			wo, dict(box_1=12.5, box_1_qty=20, box_3=0, box_3_qty=5), "Box 3"
		)

		# (c) negative Box 3 -> zero writes
		self._assert_zero_write_rejection(
			wo, dict(box_1=12.5, box_1_qty=20, box_3=-1, box_3_qty=-1), "kg"
		)

		# three-box sum mismatch names all boxes (fg fixture counts in Pack)
		self._assert_zero_write_rejection(
			wo, dict(box_1=12.5, box_1_qty=12, box_2=8, box_2_qty=7, box_3=2, box_3_qty=2),
			"Box 1 + Box 2 + Box 3 (21) harus tepat 20 Pack",
		)

		# (a) happy 3-box split
		wo2, _ = self._lot_ready(100)
		result = self._request(wo2, box_1=12.5, box_1_qty=12, box_2=8, box_2_qty=7, box_3=2, box_3_qty=1)
		self.assertEqual(result["expected_unit_count"], 20)
		self.assertEqual((result["box_3"], result["box_3_qty"]), (2, 1))
		summary = self._summary(wo2.name)
		self.assertEqual(summary.custom_handover_material_request, result["material_request"])
		self.assertEqual((flt(summary.custom_box_3), summary.custom_box_3_qty), (2, 1))
		self.assertEqual(summary.custom_handover_status, "Diminta Gudang")
		row = self._req(result["board"], result["material_request"])
		self.assertEqual((row["box_3"], row["box_3_qty"]), (2, 1))
		self.assertEqual(row["boxes"], [12.5, 8, 2])  # all three kgs listed

		# (d) backward-compat: a plain 2-box call writes Box 3 as 0/0
		wo3, _ = self._lot_ready(60)
		result2 = self._request(wo3)  # default 10 kg / 12 Packs, no Box 3 payload
		summary2 = self._summary(wo3.name)
		self.assertEqual(summary2.custom_handover_material_request, result2["material_request"])
		self.assertEqual((flt(summary2.custom_box_2), summary2.custom_box_2_qty), (0, 0))
		self.assertEqual((flt(summary2.custom_box_3), summary2.custom_box_3_qty), (0, 0))

	def test_t35_create_request_writes_wo_summary_atomically(self):
		"""Valid two-box and one-box allocations land atomically on the Work
		Order summary (Link + four box values, status Diminta Gudang); the MR
		stays a pure request document (no box/postpacking fields)."""
		wo, _ = self._lot_ready(100)
		result = self._request(wo, box_1=12.5, box_1_qty=12, box_2=8.25, box_2_qty=8)
		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, result["material_request"])
		self.assertEqual((flt(summary.custom_box_1), summary.custom_box_1_qty), (12.5, 12))
		self.assertEqual((flt(summary.custom_box_2), summary.custom_box_2_qty), (8.25, 8))
		self.assertEqual(summary.custom_handover_status, "Diminta Gudang")
		self.assertEqual(result["expected_unit_count"], 20)
		mr = frappe.get_doc("Material Request", result["material_request"])
		self.assertFalse(mr.custom_postpacking_confirmed)
		self.assertEqual(flt(mr.custom_box_1), 0)
		# the board mirrors the summary on the request row
		row = self._req(result["board"], mr.name)
		self.assertEqual(row["lane"], "request")
		self.assertEqual((row["box_1"], row["box_1_qty"]), (12.5, 12))
		self.assertEqual((row["box_2"], row["box_2_qty"]), (8.25, 8))

		# valid ONE-box allocation on a fresh WO
		wo2, _ = self._lot_ready(60)
		result2 = self._request(wo2)  # default 10 kg / 12 Packs
		summary2 = self._summary(wo2.name)
		self.assertEqual(summary2.custom_handover_material_request, result2["material_request"])
		self.assertEqual((flt(summary2.custom_box_1), summary2.custom_box_1_qty), (10, 12))
		self.assertEqual((flt(summary2.custom_box_2), summary2.custom_box_2_qty), (0, 0))

	def test_t35_second_request_owns_link_and_boxes_after_send(self):
		"""Resolver precedence: after M1 is sent, a newer request M2 owns the
		Work Order Link and the box values — an older SE can never demote an
		equal-or-newer selected request. The sent M1 card stays document-derived
		terkirim and never receives M2's boxes. Stock consumed by M1's send is
		restored through the smallest honest native desk movement so M2 can be
		a full produced-qty request without weakening any production guard."""
		from production_app.api import handover as handover_api

		wo, batch = self._lot_ready(60)
		mr1 = frappe.get_doc("Material Request", self._request(wo)["material_request"])  # 10 kg / 12 Pack
		self._send(mr1)

		# restore the sent batch to the lot warehouse at the desk (native)
		restore = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer",
				"company": self.company,
				"items": [
					{
						"item_code": self.fg,
						"qty": 60,
						"basic_rate": 10,
						"s_warehouse": self.target_wh,
						"t_warehouse": self.cold_wh,
						"use_serial_batch_fields": 1,
						"batch_no": batch,
					}
				],
			}
		)
		restore.insert()
		restore.submit()

		# distinct boxes on the second request (7 + 5 = 12 Pack)
		result2 = self._request(wo, box_1=4.5, box_1_qty=7, box_2=3.5, box_2_qty=5)
		mr2 = frappe.get_doc("Material Request", result2["material_request"])

		# the newer request owns the resolver state: an older SE may not revert it
		self.assertEqual(
			handover_api._handover_state([wo.name]),
			{wo.name: {"lane": "request", "mr": mr2.name}},
		)
		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, mr2.name)
		self.assertEqual((flt(summary.custom_box_1), summary.custom_box_1_qty), (4.5, 7))
		self.assertEqual((flt(summary.custom_box_2), summary.custom_box_2_qty), (3.5, 5))
		# document-derived: M2 is the open request (M1's SE is history, not state)
		self.assertEqual(summary.custom_handover_status, "Diminta Gudang")

		board = handover_board()
		row1, row2 = self._req(board, mr1.name), self._req(board, mr2.name)
		self.assertEqual(row1["lane"], "terkirim")  # per-MR: the submitted SE stays
		self.assertEqual(row1["stock_entry"], frappe.db.get_value(
			"Stock Entry Detail", {"material_request": mr1.name, "docstatus": 1}, "parent"
		))
		# the OLD sent card must not display M2's boxes (WO Link points at M2)
		self.assertIsNone(row1["box_1"])
		self.assertIsNone(row1["box_1_qty"])
		self.assertIsNone(row1["box_2"])
		self.assertIsNone(row1["box_2_qty"])
		self.assertEqual(row2["lane"], "request")
		self.assertEqual((row2["box_1"], row2["box_1_qty"]), (4.5, 7))
		self.assertEqual((row2["box_2"], row2["box_2_qty"]), (3.5, 5))

	def test_t36_cancelled_request_never_leaves_its_boxes_on_another_mr(self):
		"""T36 controller ruling: create M1 -> send M1 -> create M2 -> cancel M2.
		The resolver falls back to the older sent M1 (M2 died unsent), so the
		summary moves the Link back to M1 — and M2's box values MUST NOT travel
		with it: summary selection/clearing never leaks a cancelled request's
		allocation onto another MR (M1's own allocation was overwritten when M2
		was created and MRs carry no box fields, so honest = cleared)."""
		from production_app.api import handover as handover_api

		wo, batch = self._lot_ready(60)
		mr1 = frappe.get_doc("Material Request", self._request(wo)["material_request"])  # 10 kg / 12 Pack
		self._send(mr1)

		# restore the sent batch so M2 can request the full produced qty
		restore = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer",
				"company": self.company,
				"items": [
					{
						"item_code": self.fg,
						"qty": 60,
						"basic_rate": 10,
						"s_warehouse": self.target_wh,
						"t_warehouse": self.cold_wh,
						"use_serial_batch_fields": 1,
						"batch_no": batch,
					}
				],
			}
		)
		restore.insert()
		restore.submit()

		result2 = self._request(wo, box_1=4.5, box_1_qty=7, box_2=3.5, box_2_qty=5)
		mr2 = frappe.get_doc("Material Request", result2["material_request"])
		self.assertEqual(self._summary(wo.name).custom_handover_material_request, mr2.name)

		frappe.set_user(self.gudang)
		try:
			cancel_request(mr2.name)
		finally:
			frappe.set_user("Administrator")

		# resolver falls back to the older sent M1 (newest relevant evidence)
		self.assertEqual(
			handover_api._handover_state([wo.name]),
			{wo.name: {"lane": "terkirim", "mr": mr1.name}},
		)
		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, mr1.name)
		self.assertEqual(summary.custom_handover_status, "Terkirim")
		# the cancelled request's boxes died with it — nothing leaks onto M1
		self.assertEqual(
			(flt(summary.custom_box_1), summary.custom_box_1_qty,
			 flt(summary.custom_box_2), summary.custom_box_2_qty),
			(0, 0, 0, 0),
		)
		board = handover_board()
		row1, row2 = self._req(board, mr1.name), self._req(board, mr2.name)
		self.assertEqual(row1["lane"], "terkirim")
		self.assertIsNone(row1["box_1"])
		self.assertIsNone(row1["box_1_qty"])
		self.assertIsNone(row1["box_2"])
		self.assertIsNone(row1["box_2_qty"])
		self.assertIsNone(row2["lane"])
		self.assertEqual(row2["flag"], "cancelled")
		self.assertIsNone(row2["box_1"])

	def test_t35_send_does_not_require_postpacking_confirmation_and_retains_summary(self):
		"""send_handover runs on a plain request (no verify step anywhere) and
		the Work Order summary survives the send with status Terkirim."""
		wo, batch = self._lot_ready(50)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		self.assertFalse(mr.custom_postpacking_confirmed)
		self.assertEqual(flt(mr.custom_box_1), 0)

		result = self._send(mr)
		self.assertEqual(flt(result["qty"]), 50)
		self.assertEqual(flt(get_batch_qty(batch, self.target_wh)), 50)  # stock moved
		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, mr.name)
		self.assertEqual((flt(summary.custom_box_1), summary.custom_box_1_qty), (10, 10))
		self.assertEqual(summary.custom_handover_status, "Terkirim")

	def test_t28_wo_postpacking_not_touched_by_handover(self):
		"""T28: the WO postpacking fields (written by the workspace
		confirm_postpacking BEFORE manufacture, T27) survive the request/send
		steps; only the summary block is ours to write."""
		wo, _ = self._lot_ready(100)
		# plant the workspace-written postpacking values directly on the WO
		frappe.db.set_value(
			"Work Order", wo.name,
			{
				"custom_good_qty_postpacking": 100,
				"custom_reject_qty_postpacking": 2,
				"custom_trial_qty_postpacking": 1,
				"custom_sisa_qty_postpacking": -3,  # whatever the workspace wrote — NOT ours to touch
				"custom_jam_packing": "08:00:00",
				"custom_qc_packing": "Administrator",
			},
		)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		self._send(mr)

		wob = frappe.db.get_value(
			"Work Order", wo.name,
			["custom_good_qty_postpacking", "custom_reject_qty_postpacking", "custom_trial_qty_postpacking", "custom_sisa_qty_postpacking", "custom_jam_packing", "custom_qc_packing"],
			as_dict=True,
		)
		self.assertEqual(flt(wob.custom_good_qty_postpacking), 100)  # NOT touched
		self.assertEqual(flt(wob.custom_reject_qty_postpacking), 2)
		self.assertEqual(flt(wob.custom_trial_qty_postpacking), 1)
		self.assertEqual(flt(wob.custom_sisa_qty_postpacking), -3)
		self.assertEqual(str(wob.custom_jam_packing), "8:00:00")
		self.assertEqual(wob.custom_qc_packing, "Administrator")

	# --------------------------------------------------------- 3. send

	def test_t31_send_requested_qty_mr_not_stopped(self):
		"""R6: send moves EXACTLY the requested qty (== WO produced_qty), the
		MR goes Transferred and is NEVER stopped (the short-close branch is
		gone); duplicate send blocked; SE posts at the send moment (R1)."""
		wo, batch = self._lot_ready(100)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		result = self._send(mr)
		self.assertNotIn("stopped", result)  # dead keys removed
		self.assertNotIn("good", result)
		self.assertEqual(flt(result["qty"]), 100)
		se = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual(se.docstatus, 1)
		self.assertEqual(se.purpose, "Material Transfer")
		self.assertEqual(se.items[0].s_warehouse, self.cold_wh)
		self.assertEqual(se.items[0].t_warehouse, self.target_wh)
		self.assertEqual(se.items[0].batch_no, batch)  # v16: old field kept + bundle built
		self.assertTrue(se.items[0].serial_and_batch_bundle)
		ledger = {
			r.warehouse: flt(r.actual_qty)
			for r in frappe.get_all(
				"Stock Ledger Entry", filters={"voucher_no": se.name, "is_cancelled": 0},
				fields=["warehouse", "actual_qty"],
			)
		}
		self.assertEqual(ledger.get(self.cold_wh), -100)
		self.assertEqual(ledger.get(self.target_wh), 100)
		self.assertEqual(flt(get_batch_qty(batch, self.cold_wh) or 0), 0)
		self.assertEqual(flt(get_batch_qty(batch, self.target_wh)), 100)
		mr.reload()
		self.assertEqual(mr.status, "Transferred")  # NOT stopped
		self.assertEqual(flt(mr.per_ordered), 100)
		# R1: the SE posting is the actual send moment — never back-dated
		self.assertEqual(str(se.posting_date), today())
		board = handover_board()
		req = self._req(board, mr.name)
		self.assertEqual(req["lane"], "terkirim")
		self.assertEqual(req["stock_entry"], se.name)
		self.assertEqual(flt(self._lot(board, wo.name)["reserved_qty"]), 0)

		# duplicate send blocked, still exactly one SE
		with self.assertRaises(frappe.ValidationError):
			self._send(mr)
		self.assertEqual(
			len(frappe.get_all("Stock Entry Detail", filters={"material_request": mr.name, "docstatus": 1})), 1
		)

	# ------------------------------- FU8. batchless lots: pool + no-batch send

	def test_t31_batchless_pool_actions_and_send(self):
		"""Batchless FG — the pool is item-level: requests from TWO WOs of the
		same item draw one balance and fill it exactly; send creates a NO-batch
		Stock Entry that drains exactly the requested qty; the MR is not
		stopped; boxes mirror to the batchless WO like the batch path."""
		wo1 = self._make_wo(60, item=self.fg_nb)
		self._transfer(wo1)
		self._manufacture(wo1, 60)
		wo2 = self._make_wo(40, item=self.fg_nb)
		self._transfer(wo2)
		self._manufacture(wo2, 40)
		self.assertEqual(flt(_item_stock(self.fg_nb, self.cold_wh)), 100)  # one pool

		result = self._request(wo1)  # qty == produced 60, from the shared pool
		self.assertEqual(flt(result["qty"]), 60)
		mr1 = frappe.get_doc("Material Request", result["material_request"])
		lot2 = self._lot(result["board"], wo2.name)
		self.assertTrue(lot2["batchless"])
		self.assertIsNone(lot2["batch"])
		self.assertEqual(flt(lot2["available_qty"]), 40)  # 100 - 60, same pool

		# the second WO's full qty fills the pool exactly
		result2 = self._request(wo2)
		self.assertEqual(flt(result2["qty"]), 40)
		board = handover_board()
		self.assertEqual(flt(self._lot(board, wo1.name)["reserved_qty"]), 100)
		self.assertEqual(flt(self._lot(board, wo1.name)["available_qty"]), 0)

		res = self._send(mr1)
		self.assertIsNone(res["batch"])
		self.assertEqual(flt(res["qty"]), 60)
		se = frappe.get_doc("Stock Entry", res["stock_entry"])
		self.assertFalse(se.items[0].batch_no)  # batchless: no batch anywhere
		self.assertFalse(se.items[0].serial_and_batch_bundle)
		self.assertEqual(flt(_item_stock(self.fg_nb, self.cold_wh)), 40)
		self.assertEqual(flt(_item_stock(self.fg_nb, self.target_wh)), 60)
		mr1.reload()
		self.assertEqual(mr1.status, "Transferred")  # full qty: never stopped (R6)
		# the summary (kg + Pack) was written at create and survives the send
		summary = self._summary(wo1.name)
		self.assertEqual(summary.custom_handover_material_request, mr1.name)
		self.assertEqual((flt(summary.custom_box_1), summary.custom_box_1_qty), (10, 12))
		self.assertEqual(summary.custom_handover_status, "Terkirim")

		# duplicate send still blocked; pool unchanged by the failed attempt
		with self.assertRaises(frappe.ValidationError):
			self._send(mr1)
		self.assertEqual(flt(_item_stock(self.fg_nb, self.cold_wh)), 40)

	# --------------------------------------------------------- 4. cancel

	def test_t24_cancel_request_only_before_send(self):
		"""Cancel any UNSENT request (native, reservation released, board
		updated, Work Order summary cleared) — the retired postpacking flag no
		longer blocks; a sent request is blocked; produksi denied."""
		wo, _ = self._lot_ready(100)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		self.assertEqual(flt(self._lot(handover_board(), wo.name)["available_qty"]), 0)
		self.assertEqual(self._summary(wo.name).custom_handover_material_request, mr.name)

		frappe.set_user(self.gudang)
		try:
			result = cancel_request(mr.name)
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(result["ok"])
		mr.reload()
		self.assertEqual(mr.docstatus, 2)
		req = self._req(result["board"], mr.name)
		self.assertIsNone(req["lane"])
		self.assertEqual(req["flag"], "cancelled")
		self.assertEqual(flt(self._lot(result["board"], wo.name)["reserved_qty"]), 0)  # released
		self.assertEqual(flt(self._lot(result["board"], wo.name)["available_qty"]), 100)
		# the Work Order summary cleared with the request (Link + four boxes)
		summary = self._summary(wo.name)
		self.assertIsNone(summary.custom_handover_material_request)
		self.assertEqual(
			(flt(summary.custom_box_1), summary.custom_box_1_qty,
			 flt(summary.custom_box_2), summary.custom_box_2_qty),
			(0, 0, 0, 0),
		)
		self.assertFalse(summary.custom_handover_status)

		# a legacy confirmed request cancels too (guard removed)
		wo2, _ = self._lot_ready(100)
		mr2 = frappe.get_doc("Material Request", self._request(wo2)["material_request"])
		mr2.db_set("custom_postpacking_confirmed", 1)  # legacy flag: no longer blocks
		frappe.set_user(self.gudang)
		try:
			result2 = cancel_request(mr2.name)
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(result2["ok"])
		self.assertIsNone(self._summary(wo2.name).custom_handover_material_request)

		# with a submitted SE -> blocked (clear error before the native guard)
		wo3, _ = self._lot_ready(100)
		mr3 = frappe.get_doc("Material Request", self._request(wo3)["material_request"])
		self._send(mr3)
		frappe.set_user(self.gudang)
		try:
			with self.assertRaises(frappe.ValidationError):
				cancel_request(mr3.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Material Request", mr3.name, "docstatus"), 1)
		# sent handover RETAINS the summary (Link + boxes, status Terkirim)
		sent_summary = self._summary(wo3.name)
		self.assertEqual(sent_summary.custom_handover_material_request, mr3.name)
		self.assertEqual(sent_summary.custom_handover_status, "Terkirim")

		# produksi (Manufacturing User) denied — `wo` again: its MR was
		# cancelled, so the lot is unreserved and the request passes availability
		mr4 = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		frappe.set_user(self.prod)
		try:
			with self.assertRaises(frappe.PermissionError):
				cancel_request(mr4.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Material Request", mr4.name, "docstatus"), 1)

	def test_t35_cancel_request_requires_work_order_read(self):
		"""A gudang-side user whose role lost Work Order READ is denied BEFORE
		the WO lock/write: real DocPerm denial (no permission mocking) — the MR
		stays submitted and the summary (Link + boxes + status) is untouched."""
		wo, _ = self._lot_ready(100)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		before = self._summary(wo.name)
		self.assertEqual(before.custom_handover_material_request, mr.name)

		# fixture: deny Work Order READ for the whole gudang role. The site
		# grants it via a standing Custom DocPerm row and frappe ORs every
		# role row (get_role_permissions), so a second read=0 row proves
		# nothing — the existing grant itself is flipped. This framework rolls
		# back per CLASS (not per test), so the flip is restored in the
		# instance cleanup to keep the rest of the class unpoisoned.
		perm = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": "Work Order", "role": "Gudang Barang Jadi"},
			["name", "read"],
			as_dict=True,
		)
		if perm and perm.read:
			frappe.db.set_value("Custom DocPerm", perm.name, "read", 0)
			frappe.clear_cache(doctype="Work Order")  # db.set_value fires no on_update
			self.addCleanup(frappe.db.set_value, "Custom DocPerm", perm.name, "read", perm.read)
			self.addCleanup(frappe.clear_cache, doctype="Work Order")
		noread = self._make_user(
			f"t24.noread.{random_string(6).lower()}@prodapp.example.com", ["Gudang Barang Jadi"]
		)
		self.assertFalse(frappe.has_permission("Work Order", "read", user=noread))

		frappe.set_user(noread)
		try:
			with self.assertRaises(frappe.PermissionError):
				cancel_request(mr.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Material Request", mr.name, "docstatus"), 1)
		self.assertEqual(self._summary(wo.name), before)

	def test_t35_native_desk_cancel_clears_wo_summary(self):
		"""An unsent Desk cancel of the MR (doc_events path) clears the Work
		Order Link and all four box values."""
		wo, _ = self._lot_ready(40)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		self.assertEqual(self._summary(wo.name).custom_handover_material_request, mr.name)

		frappe.get_doc("Material Request", mr.name).cancel()  # native desk cancel

		summary = self._summary(wo.name)
		self.assertIsNone(summary.custom_handover_material_request)
		self.assertEqual(
			(flt(summary.custom_box_1), summary.custom_box_1_qty,
			 flt(summary.custom_box_2), summary.custom_box_2_qty),
			(0, 0, 0, 0),
		)
		self.assertFalse(summary.custom_handover_status)

	# ------------------------------------------------------- 5. permissions

	def test_t24_permission_matrix_all_actions(self):
		"""Gudang cannot verify/send/touch WO mutation paths; produksi cannot
		create/cancel; bare user denied everything with an empty board."""
		wo, _ = self._lot_ready(100)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])

		# gudang: send denied; the retired verify endpoint rejects (compat message)
		frappe.set_user(self.gudang)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				save_post_packing(mr.name)
			self.assertIn("Muat ulang halaman", str(ctx.exception))
			with self.assertRaises(frappe.PermissionError):
				send_handover(mr.name)
			self.assertFalse(frappe.has_permission("Work Order", "write"))
			with self.assertRaises(frappe.PermissionError):
				finish(wo.name)  # WO mutation path
		finally:
			frappe.set_user("Administrator")

		# produksi: create + cancel denied
		frappe.set_user(self.prod)
		try:
			with self.assertRaises(frappe.PermissionError):
				create_request(wo.name)
			with self.assertRaises(frappe.PermissionError):
				cancel_request(mr.name)
		finally:
			frappe.set_user("Administrator")

		# bare user: everything denied, board readable-but-empty
		frappe.set_user(self.bare)
		try:
			with self.assertRaises(frappe.PermissionError):
				create_request(wo.name)
			with self.assertRaises(frappe.PermissionError):
				cancel_request(mr.name)
			with self.assertRaises(frappe.ValidationError) as ctx:
				save_post_packing(mr.name)  # compat rejection is role-agnostic
			self.assertIn("Muat ulang halaman", str(ctx.exception))
			with self.assertRaises(frappe.PermissionError):
				send_handover(mr.name)
			board = handover_board()  # must not raise
			self.assertEqual(board["lots"], [])
			self.assertEqual(board["requests"], [])
		finally:
			frappe.set_user("Administrator")

	# -------------------------------- 5b. role semantics: Stock User (user decision 2026-09-14)

	def test_t24_stock_user_role_semantics(self):
		"""Sisi gudang = Stock User JUGA (bukan hanya Gudang Barang Jadi):
		Stock-only -> hanya request (verify/send ditolak, flag gudang murni);
		Manufacturing+Stock -> dua sisi sekaligus (loop penuh satu akun)."""
		stock = self._make_user("t24.stock.role@prodapp.example.com", ["Stock User"])
		both = self._make_user("t24.both.role@prodapp.example.com", ["Stock User", "Manufacturing User"])
		wo, _ = self._lot_ready(120)

		# stock-only: flag gudang murni, boleh request, ditolak verify/send
		frappe.set_user(stock)
		try:
			board = handover_board()
			self.assertTrue(board["roles"]["is_gudang"])
			self.assertFalse(board["roles"]["is_produksi"])
			result = create_request(wo.name, box_1=10, box_1_qty=24)
			self.assertEqual(flt(result["qty"]), 120)
			stock_mr = frappe.get_doc("Material Request", result["material_request"])
			self.assertEqual(stock_mr.docstatus, 1)
			with self.assertRaises(frappe.ValidationError):
				save_post_packing(stock_mr.name)  # compat rejection, role-agnostic
			with self.assertRaises(frappe.PermissionError):
				send_handover(stock_mr.name)
		finally:
			frappe.set_user("Administrator")

		# manufacturing+stock: dua flag aktif, loop penuh dalam satu akun
		# (fresh WO — the duplicate-active guard blocks a second request on wo)
		wo2, _ = self._lot_ready(40)
		frappe.set_user(both)
		try:
			board = handover_board()
			self.assertTrue(board["roles"]["is_gudang"])
			self.assertTrue(board["roles"]["is_produksi"])
			both_mr = frappe.get_doc(
				"Material Request",
				create_request(wo2.name, box_1=5, box_1_qty=8)["material_request"],
			)
			res = send_handover(both_mr.name)
			self.assertTrue(res["ok"])
			self.assertEqual(flt(res["qty"]), 40)
			self.assertEqual(res["stock_entry"], frappe.db.get_value(
				"Stock Entry Detail", {"material_request": both_mr.name}, "parent"
			))
		finally:
			frappe.set_user("Administrator")

	# ------------------------------------- 6. atomicity: shortage

	def test_t24_send_insufficient_batch_stock_is_atomic(self):
		"""Batch drained after verification -> send refuses; zero partial
		documents survive (request-boundary rollback)."""
		wo, batch = self._lot_ready(100)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])

		# consume the batch at the desk AFTER the request exists
		drain = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer",
				"company": self.company,
				"items": [
					{
						"item_code": self.fg,
						"qty": 100,
						"basic_rate": 10,
						"s_warehouse": self.cold_wh,
						"t_warehouse": self.target_wh,
						"use_serial_batch_fields": 1,
						"batch_no": batch,
					}
				],
			}
		)
		drain.insert()
		drain.submit()

		frappe.db.savepoint("t24_shortage")
		try:
			with self.assertRaises(frappe.ValidationError):
				self._send(mr)
		finally:
			frappe.db.rollback(save_point="t24_shortage")

		self.assertEqual(
			len(frappe.get_all("Stock Entry Detail", filters={"material_request": mr.name})), 0
		)
		mr.reload()
		self.assertEqual(mr.status, "Pending")  # untouched, still reserved
		self.assertEqual(flt(mr.per_ordered), 0)

	# ---------------------------------- 7. native desk cancel of the SE

	def test_t24_desk_cancel_se_recomputes_board(self):
		"""Native desk SE cancel of a full send: the board returns the request
		to the request lane and a re-send works (derived truth: no submitted SE
		anymore); the WO summary is retained, status drops back."""
		wo, batch = self._lot_ready(100)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		result = self._send(mr)
		frappe.get_doc("Stock Entry", result["stock_entry"]).cancel()
		board = handover_board()
		self.assertEqual(flt(self._lot(board, wo.name)["physical_qty"]), 100)  # stock restored
		req = self._req(board, mr.name)
		self.assertEqual(req["lane"], "request")  # nothing submitted against the MR anymore
		self.assertIsNone(req["stock_entry"])
		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, mr.name)
		self.assertEqual(summary.custom_handover_status, "Diminta Gudang")

		# re-send works
		result2 = self._send(mr)
		self.assertTrue(frappe.db.exists("Stock Entry", result2["stock_entry"]))
		self.assertEqual(flt(get_batch_qty(batch, self.target_wh)), 100)

	def test_fu23_wo_native_handover_status_field_syncs(self):
		"""FU23: Work Order.custom_handover_status (Select read-only di form
		Desk) mengikuti alur serah terima lewat doc_events + aksi aplikasi:
		belum -> Diminta Gudang -> Terkirim; cancel mengosongkan; cancel SE
		(Desk) menurunkan kembali ke Diminta Gudang (MR masih submitted)."""
		status = lambda name: frappe.db.get_value(
			"Work Order", name, "custom_handover_status"
		)

		wo, _ = self._lot_ready(100)
		self.assertFalse(status(wo.name))  # belum pernah diminta (None/'' kosong)

		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		self.assertEqual(status(wo.name), "Diminta Gudang")  # MR on_submit

		# cancel dari lane request -> kembali kosong (MR on_cancel)
		frappe.set_user(self.gudang)
		try:
			cancel_request(mr.name)
		finally:
			frappe.set_user("Administrator")
		self.assertFalse(status(wo.name))

		# alur lengkap: request -> send (tanpa langkah verify)
		mr2 = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		self.assertEqual(status(wo.name), "Diminta Gudang")
		result = self._send(mr2)
		self.assertEqual(status(wo.name), "Terkirim")  # SE on_submit

		# desk SE cancel menurunkan state (SE on_cancel): MR masih aktif
		frappe.get_doc("Stock Entry", result["stock_entry"]).cancel()
		self.assertEqual(status(wo.name), "Diminta Gudang")

	# --------------- FU37. doc_events fail-safe untuk transaksi native

	def test_fu37_se_sync_skips_when_app_metadata_missing(self):
		"""FU37 (insiden nyata MAT-STE-2026-06920): kode app terpasang tapi
		migrate belum dijalankan -> kolom MRI.custom_work_order tidak ada ->
		submit Stock Entry Material Transfer reguler mati OperationalError
		1054. doc_events wajib memeriksa kesiapan metadata lebih dulu dan
		melewati mirror (data turunan), tanpa query tabel apa pun."""
		# SimpleNamespace: frappe._dict tidak bisa dipakai karena atribut
		# `items` tertutup method dict.items bawaan.
		se_doc = SimpleNamespace(
			name="T24-SE-FU37",
			items=[frappe._dict(material_request="MAT-MR-2026-00325")],
		)
		meta = MagicMock()
		meta.has_field.return_value = False
		with (
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(
				frappe, "get_all",
				side_effect=SQLOperationalError(
					1054, "Unknown column 'custom_work_order' in 'SELECT'"
				),
			) as get_all,
		):
			sync_from_stock_entry(se_doc)  # tidak boleh raise
		get_all.assert_not_called()

	def test_fu37_mr_sync_skips_when_app_metadata_missing(self):
		"""FU37 pasangan MR: dokumen yang masih membawa nilai lama (meta
		stale/half-migrated) pun tidak boleh membuat submit/cancel Material
		Request native gagal karena mirror serah terima."""
		mr_doc = SimpleNamespace(
			name="T24-MR-FU37",
			items=[frappe._dict(custom_work_order="MFG-WO-2026-03115")],
		)
		meta = MagicMock()
		meta.has_field.return_value = False
		with (
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(
				frappe, "get_all",
				side_effect=SQLOperationalError(
					1054, "Unknown column 'custom_work_order' in 'SELECT'"
				),
			) as get_all,
		):
			sync_from_material_request(mr_doc)  # tidak boleh raise
		get_all.assert_not_called()

	def test_fu37_sync_failure_is_logged_never_raises(self):
		"""FU37 jaring pengaman: kegagalan sync tak terduga (di luar kasus
		metadata) dicatat ke Error Log dan TIDAK dinaikkan — transaksi
		SE/MR native tidak pernah dibatalkan oleh mirror production_app."""
		se_doc = SimpleNamespace(
			name="T24-SE-FU37B",
			items=[frappe._dict(material_request="MAT-MR-2026-00325")],
		)
		mr_doc = SimpleNamespace(
			name="T24-MR-FU37B",
			items=[frappe._dict(custom_work_order="MFG-WO-2026-03115")],
		)
		with patch(
			"production_app.api.handover.sync_handover_status",
			side_effect=RuntimeError("boom fu37"),
		):
			sync_from_stock_entry(se_doc)  # tidak boleh raise
			sync_from_material_request(mr_doc)  # tidak boleh raise
		self.assertTrue(frappe.db.exists(
			"Error Log",
			{"method": "Production App: sinkronisasi status serah terima gagal"},
		))

	def test_fu37_sync_guard_failure_is_contained(self):
		"""FU37 (review advisor): pemeriksaan kesiapan metadata itu sendiri
		gagal (mis. cache meta rusak) — handler tetap menelan error, mencatat
		ke Error Log, dan tidak mengganggu transaksi native."""
		se_doc = SimpleNamespace(
			name="T24-SE-FU37C",
			items=[frappe._dict(material_request="MAT-MR-2026-00325")],
		)
		mr_doc = SimpleNamespace(
			name="T24-MR-FU37C",
			items=[frappe._dict(custom_work_order="MFG-WO-2026-03115")],
		)
		# Patch selektif: hanya meta dua doctype guard yang meledak —
		# frappe.log_error sendiri memanggil get_meta("Error Log") dan itu
		# harus tetap jalan supai Error Log benar-benar tertulis.
		real_get_meta = frappe.get_meta

		def meta_boom(doctype=None, *args, **kwargs):
			if doctype in ("Material Request Item", "Work Order"):
				raise RuntimeError("meta boom fu37")
			return real_get_meta(doctype, *args, **kwargs)

		with patch.object(frappe, "get_meta", side_effect=meta_boom):
			sync_from_stock_entry(se_doc)  # tidak boleh raise
			sync_from_material_request(mr_doc)  # tidak boleh raise
		self.assertTrue(frappe.db.exists(
			"Error Log",
			{"method": "Production App: sinkronisasi status serah terima gagal"},
		))

	# ------------------- FU29. pre-check gudang rute + route_available board

	def test_fu29_send_blocked_when_route_warehouse_lacks_batch(self):
		"""FU29 root cause (kasus nyata MR 00270): stok lot ada di gudang asal
		SE, tetapi rute request mengarah ke source setting yang kosong.
		send_handover wajib menolak terhadap from_warehouse RUTE (pesan
		Indonesia menyebut gudangnya, nol tulis) — bukan mati di submit native
		dengan error Inggris mentah."""
		wo, batch = self._lot_ready(50)
		prior = frappe.db.get_single_value(
			"Manufacturing Settings", "custom_default_handover_source_warehouse"
		)
		try:
			warehouse_defaults_save(
				handover_warehouse=self.target_wh, handover_source_warehouse=self.src_wh
			)
			result = self._request(wo)
			mr = frappe.get_doc("Material Request", result["material_request"])
			self.assertEqual(mr.items[0].from_warehouse, self.src_wh)  # origin rute

			# peringatan dini di board: stok batch ini di gudang rute = 0
			req = self._req(result["board"], mr.name)
			self.assertEqual(flt(req["route_available"]), 0)

			frappe.db.savepoint("fu29_blocked")
			try:
				with self.assertRaises(frappe.ValidationError) as ctx:
					self._send(mr)
			finally:
				frappe.db.rollback(save_point="fu29_blocked")
			self.assertIn("gudang asal", str(ctx.exception))
			self.assertIn(self.src_wh, str(ctx.exception))  # menyebut gudang rutenya
			self.assertEqual(
				len(frappe.get_all("Stock Entry Detail", filters={"material_request": mr.name})), 0
			)
		finally:
			warehouse_defaults_save(
				handover_warehouse=self.target_wh, handover_source_warehouse=prior
			)

	def test_fu29_route_available_full_and_send_still_ok(self):
		"""Rute = tempat batch berada (fallback lot warehouse): tanpa peringatan
		(route_available == qty) dan kirim tetap sukses — regresi jalur sehat."""
		wo, batch = self._lot_ready(30)
		result = self._request(wo)  # setting unset -> rute fallback ke lot warehouse
		mr = frappe.get_doc("Material Request", result["material_request"])
		req = self._req(result["board"], mr.name)
		self.assertEqual(flt(req["route_available"]), 30)
		res = self._send(mr)
		self.assertEqual(flt(res["qty"]), 30)

	def test_fu29_batchless_send_follows_route_not_current_setting(self):
		"""Batchless drift (FU29): request dibuat saat source setting menunjuk
		pool yang berisi; setting LALU berubah -> kirim tetap mengikuti RUTE
		(MR from_warehouse) yang benar-benar dipakai SE, bukan pool setting
		baru. Kode lama memeriksa pool setting terkini dan salah memblokir."""
		wo = self._make_wo(40, item=self.fg_nb2)
		self._transfer(wo)
		self._manufacture(wo, 40)
		prior = frappe.db.get_single_value(
			"Manufacturing Settings", "custom_default_handover_source_warehouse"
		)
		try:
			warehouse_defaults_save(
				handover_warehouse=self.target_wh, handover_source_warehouse=self.cold_wh
			)
			result = self._request(wo)  # rute = cold_wh, pool berisi 40
			mr = frappe.get_doc("Material Request", result["material_request"])
			self.assertEqual(mr.items[0].from_warehouse, self.cold_wh)
			req = self._req(result["board"], mr.name)
			self.assertEqual(flt(req["route_available"]), 40)

			# setting berubah SETELAH request dibuat — rute MR tidak ikut pindah
			warehouse_defaults_save(
				handover_warehouse=self.target_wh, handover_source_warehouse=self.src_wh
			)
			board = handover_board()
			self.assertEqual(flt(self._req(board, mr.name)["route_available"]), 40)

			res = self._send(mr)  # route berisi: kirim sah walau setting kini src_wh
			self.assertEqual(flt(res["qty"]), 40)
		finally:
			warehouse_defaults_save(
				handover_warehouse=self.target_wh, handover_source_warehouse=prior
			)
