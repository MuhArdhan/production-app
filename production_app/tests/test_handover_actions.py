# T24/T31/T35 handover mutation actions — create/cancel request, send
#
# Proves, by execution (task-24-brief coverage as amended by T31 R1-R8 and the
# T35 three-lane cutover):
# 1. create_request(work_order): qty = the Work Order's FULL produced_qty
#    (FU96: box arguments retired — qty-only contract, the box allocation
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
# 8. FU96: box kg/counts are RETIRED everywhere; MR stays a pure request
#    document and the WO summary is Link + status only.
# 9. Legacy edge: WO with two Manufacture SEs -> create AND send refuse.
# 10. W19 shared-box groups: create_group_request (same item, shared physical
#     boxes) -> ONE Handover Box Plan + N member MRs carrying the plan link,
#     WO box fields stay 0, Σ(box qty) == Σ(expected); single/mixed/mismatch/
#     malformed-box rejections are zero-write; a member MR cannot be cancelled
#     alone while siblings are active; cancel_group_request cancels every
#     still-active member and leaves shipped members alone; the board row
#     exposes box_plan/group_size.
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
    _checked_lot,
    _insert_submitted_handover_mr,
    _item_stock,
    _pool_reserved_now,
    _pool_warehouse,
    cancel_group_request,
    cancel_request,
    create_group_request,
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

		# FU64: persona gudang jujur pasca-migrasi — role legacy + Stock User
		# (akses data SE/WO/Item/Warehouse kini lewat hak native Stock User).
		cls.gudang = cls._make_user(
			f"t24.gudang.{suffix.lower()}@prodapp.example.com", ["Gudang Barang Jadi", "Stock User"]
		)
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
		item.custom_default_inventory_unit_of_measure = "Pack"
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

	def _request(self, wo):
		"""create_request as the gudang actor (the matrix role). FU96: the
		contract is qty-only — parameter box tidak ada lagi."""
		if not flt(wo.produced_qty):
			wo.reload()  # pick up produced_qty written by the Manufacture step
		frappe.set_user(self.gudang)
		try:
			return create_request(wo.name)
		finally:
			frappe.set_user("Administrator")

	def _summary(self, wo_name):
		"""The controlled Work Order handover summary (FU96: Link + status only)."""
		return frappe.db.get_value(
			"Work Order", wo_name,
			["custom_handover_material_request", "custom_handover_status"],
			as_dict=True,
		)

	def _assert_zero_write_rejection(self, wo, kwargs, message_part):
		"""A validation failure must throw the expected Indonesian error and
		write NOTHING: no MR row, unchanged Work Order summary. FU96: kwargs
		diabaikan (qty-only) — dipakai utk msg-part saja."""
		before = self._summary(wo.name)
		count = self._bound_mr_count(wo.name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._request(wo)
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
		# FU96: result TANPA kunci box sama sekali (bukan nol)
		for key in ("box_1", "box_1_qty", "box_2", "box_2_qty", "box_3", "box_3_qty"):
			self.assertNotIn(key, result)
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

	def test_pool_reserved_ignores_transferred_mr_without_se(self):
		"""Migrated MRs are 'Transferred' with no linked SE — they are done and
		must not reserve the pool (else available goes negative)."""
		def mr(status):
			doc = frappe.get_doc(
				{
					"doctype": "Material Request",
					"material_request_type": "Material Transfer",
					"company": self.company,
					"schedule_date": today(),
					"items": [
						{
							"item_code": self.fg_nb,
							"qty": 7,
							"uom": self.uom,
							"from_warehouse": self.cold_wh,
							"warehouse": self.target_wh,
						}
					],
				}
			).insert()
			doc.submit()
			doc.db_set("status", status)

		before = _pool_reserved_now(self.fg_nb)
		mr("Transferred")
		mr("Issued")
		self.assertEqual(_pool_reserved_now(self.fg_nb), before)
		mr("Pending")
		self.assertEqual(_pool_reserved_now(self.fg_nb), before + 7)

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

	# ----------------------------- 2. FU96: qty-only contract + guard stub

	def test_t35_save_post_packing_is_a_compatibility_rejection(self):
		"""The retired verify endpoint always rejects with the reload message —
		for any role, before any lock/write — and mutates neither MR nor WO.
		FU96: signature param box-nya sudah hilang; kwarg box lama dari klien
		HTTP difilter get_newargs sebelum dipanggil (dibuktikan test FU96
		signature), jadi di sini panggilan qty-only murni."""
		wo, _ = self._lot_ready(100)
		result = self._request(wo)
		mr = frappe.get_doc("Material Request", result["material_request"])
		before = self._summary(wo.name)

		for user in (self.gudang, self.prod):
			frappe.set_user(user)
			try:
				with self.assertRaises(frappe.ValidationError) as ctx:
					save_post_packing(mr.name)
				self.assertIn("Muat ulang halaman", str(ctx.exception))
			finally:
				frappe.set_user("Administrator")

		mr.reload()
		self.assertFalse(mr.custom_postpacking_confirmed)
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

	def test_t39_invalid_alternate_conversion_rejected_at_source(self):
		"""An FG item whose warehouse display UOM is an ALTERNATE UOM without a
		valid conversion row (Default Inventory UOM = Pack, no Pack factor) is
		REJECTED at Item save by warehouse_app's validation (29 Sep: the field
		moved to warehouse_app ownership) — an unguessable factor can never
		exist in data, so the request path never sees it."""
		code = self._make_item(f"{PREFIX}-FGWP-{random_string(4).lower()}")
		item = frappe.get_cached_doc("Item", code)
		item.custom_default_inventory_unit_of_measure = "Pack"  # no uoms row added
		with self.assertRaises(frappe.ValidationError) as ctx:
			item.save()
		self.assertIn("tidak valid", str(ctx.exception))

	def test_t39_stock_uom_item_requests_in_pcs(self):
		"""T39 universal path (FU96 qty-only): an item with NO warehouse UOM
		setting counts in its stock UOM (Pcs, factor 1) — requestable without
		any conversion setup; the count == produced qty exactly, in Pcs."""
		wo, _ = self._lot_ready(22, item=self.fg_nopack)
		result = self._request(wo)
		self.assertEqual(result["unit"], self.uom)
		self.assertEqual(result["expected_unit_count"], 22)
		self.assertEqual(
			self._summary(wo.name),
			{
				"custom_handover_material_request": result["material_request"],
				"custom_handover_status": "Diminta Gudang",
			},
		)

	def test_fu96_fractional_pack_producing_qty_rejected_zero_writes(self):
		"""produced 7 pcs at factor 5 = 1.4 Packs: FU96 mempertahankan guard
		count server-side — request qty-only tetap ditolak sebelum tulisan apa
		pun (baris non-bulat tak pernah jadi dokumen setengah sah)."""
		wo, _ = self._lot_ready(7)
		self._assert_zero_write_rejection(wo, {}, "Pack utuh")

	def test_fu96_qty_only_signature_and_legacy_kwargs_filtered(self):
		"""FU96: kontrak create_request = qty-only MURNI (tidak ada param box).
		Pemanggil HTTP lama yang masih mengirim argumen box tetap AMAN: frappe
		memfilter kwarg tak dikenal via get_newargs SEBELUM fungsi dipanggil —
		nilainya diabaikan, bukan divalidasi."""
		import inspect

		from production_app.api.handover import create_request as cr

		params = inspect.signature(cr).parameters
		for key in ("box_1", "box_1_qty", "box_2", "box_2_qty", "box_3", "box_3_qty"):
			self.assertNotIn(key, params)
		self.assertEqual(
			frappe.get_newargs(cr, {"work_order": "X", "box_1": 10, "box_1_qty": 20, "box_3": 0}),
			{"work_order": "X"},
		)
		# dan perilaku lama (sampling): request dgn kwarg bocor tetap sukses
		# lewat jalur qty-only — diuji di sini sebagai kontrak, bukan via HTTP
		wo, _ = self._lot_ready(100, item=self.fg_nopack)
		result = self._request(wo)
		self.assertEqual(flt(result["qty"]), 100)
		self.assertNotIn("box_1", result)

	def test_fu96_create_request_writes_wo_summary_atomically(self):
		"""FU96: request qty-only menulis ringkasan Work Order SECARA ATOMIK —
		Link + status Diminta Gudang — dan MR tetap dokumen permintaan murni
		(tanpa field postpacking/box); baris papan identik Link-nya."""
		wo, _ = self._lot_ready(100)
		result = self._request(wo)
		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, result["material_request"])
		self.assertEqual(summary.custom_handover_status, "Diminta Gudang")
		self.assertEqual(result["expected_unit_count"], 20)
		mr = frappe.get_doc("Material Request", result["material_request"])
		self.assertFalse(mr.custom_postpacking_confirmed)
		# the board row reflects the request (no box keys at all)
		row = self._req(result["board"], mr.name)
		self.assertEqual(row["lane"], "request")
		self.assertNotIn("box_1", row)
		self.assertNotIn("boxes", row)

	def test_fu96_second_request_owns_link_after_send(self):
		"""Resolver precedence: after M1 is sent, a newer request M2 owns the
		Work Order Link — an older SE can never demote an equal-or-newer
		selected request. The sent M1 card stays document-derived terkirim.
		Stock consumed by M1's send is restored through the smallest honest
		native desk movement so M2 can be a full produced-qty request without
		weakening any production guard."""
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

		result2 = self._request(wo)
		mr2 = frappe.get_doc("Material Request", result2["material_request"])

		# the newer request owns the resolver state: an older SE may not revert it
		self.assertEqual(
			handover_api._handover_state([wo.name]),
			{wo.name: {"lane": "request", "mr": mr2.name}},
		)
		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, mr2.name)
		# document-derived: M2 is the open request (M1's SE is history, not state)
		self.assertEqual(summary.custom_handover_status, "Diminta Gudang")

		board = handover_board()
		row1, row2 = self._req(board, mr1.name), self._req(board, mr2.name)
		self.assertEqual(row1["lane"], "terkirim")  # per-MR: the submitted SE stays
		self.assertEqual(row1["stock_entry"], frappe.db.get_value(
			"Stock Entry Detail", {"material_request": mr1.name, "docstatus": 1}, "parent"
		))
		self.assertEqual(row2["lane"], "request")
		# FU96: tidak ada kunci box di kedua baris
		for row in (row1, row2):
			self.assertNotIn("box_1", row)
			self.assertNotIn("boxes", row)

	def test_fu96_cancelled_request_never_leaves_state_on_another_mr(self):
		"""T36 controller ruling (FU96: Link saja): create M1 -> send M1 ->
		create M2 -> cancel M2. The resolver falls back to the older sent M1
		(M2 died unsent), so the summary moves the Link back to M1 — a
		cancelled request never leaves any state behind."""
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

		result2 = self._request(wo)
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
		board = handover_board()
		row1, row2 = self._req(board, mr1.name), self._req(board, mr2.name)
		self.assertEqual(row1["lane"], "terkirim")
		self.assertNotIn("box_1", row1)
		self.assertIsNone(row2["lane"])
		self.assertEqual(row2["flag"], "cancelled")
		self.assertNotIn("box_1", row2)

	def test_fu96_send_does_not_require_postpacking_confirmation_and_retains_summary(self):
		"""send_handover runs on a plain request (no verify step anywhere) and
		the Work Order summary (Link + status) survives the send."""
		wo, batch = self._lot_ready(50)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		self.assertFalse(mr.custom_postpacking_confirmed)

		result = self._send(mr)
		self.assertEqual(flt(result["qty"]), 50)
		self.assertEqual(flt(get_batch_qty(batch, self.target_wh)), 50)  # stock moved
		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, mr.name)
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
		# the summary (Link + status) was written at create and survives the send
		summary = self._summary(wo1.name)
		self.assertEqual(summary.custom_handover_material_request, mr1.name)
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
		# the Work Order summary cleared with the request (Link + status)
		summary = self._summary(wo.name)
		self.assertIsNone(summary.custom_handover_material_request)
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
		# sent handover RETAINS the summary (Link + status Terkirim)
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
		stays submitted and the summary (Link + status) is untouched."""
		wo, _ = self._lot_ready(100)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		before = self._summary(wo.name)
		self.assertEqual(before.custom_handover_material_request, mr.name)

		# FU64: baris Custom DocPerm WO dihapus (doctype retired — native yang
		# berlaku), jadi skenario "tanpa read Work Order" cukup lewat persona
		# role legacy MURNI: Gudang Barang Jadi tanpa Stock User tidak punya
		# baca Work Order native (kontrak migrasi FU64 — user gudang wajib
		# membawa Stock User).
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

	def test_fu96_native_desk_cancel_clears_wo_summary(self):
		"""An unsent Desk cancel of the MR (doc_events path) clears the Work
		Order Link (dan status)."""
		wo, _ = self._lot_ready(40)
		mr = frappe.get_doc("Material Request", self._request(wo)["material_request"])
		self.assertEqual(self._summary(wo.name).custom_handover_material_request, mr.name)

		frappe.get_doc("Material Request", mr.name).cancel()  # native desk cancel

		summary = self._summary(wo.name)
		self.assertIsNone(summary.custom_handover_material_request)
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
			result = create_request(wo.name)
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
				create_request(wo2.name)["material_request"],
			)
			res = send_handover(both_mr.name)
			self.assertTrue(res["ok"])
			self.assertEqual(flt(res["qty"]), 40)
			self.assertEqual(res["stock_entry"], frappe.db.get_value(
				"Stock Entry Detail", {"material_request": both_mr.name}, "parent"
			))
		finally:
			frappe.set_user("Administrator")

	# -------------------------------- 5c. FU64: satu akun empat role native

	def test_t64_four_role_account_runs_both_sides(self):
		"""FU64 — skenario org nyata: satu user memegang Stock User + Stock
		Manager + Manufacturing User + Manufacturing Manager. Union native =
		akses penuh dua sisi dalam satu akun (dulu DocPerm custom sparse
		menolak hampir semuanya — inilah laporan "4-role merespon jelek").
		Catatan konvensi native: role manager MURNI (tanpa role User-nya)
		bukan persona app — WO read hanya lewat Manufacturing User/Stock
		User, jadi papan butuh pasangan role (didokumentasikan FU64)."""
		four = self._make_user(
			f"t64.four.{random_string(6).lower()}@prodapp.example.com",
			["Stock User", "Stock Manager", "Manufacturing User", "Manufacturing Manager"],
		)
		wo, _ = self._lot_ready(90)

		frappe.set_user(four)
		try:
			board = handover_board()
			self.assertTrue(board["roles"]["is_gudang"])
			self.assertTrue(board["roles"]["is_produksi"])
			self.assertTrue(board["roles"]["is_manajer_produksi"])
			self.assertTrue(board["roles"]["can_settings"])
			# sisi gudang: request; sisi produksi: kirim — dalam satu akun
			mr_name = create_request(wo.name)["material_request"]
			res = send_handover(mr_name)
			self.assertTrue(res["ok"])
			self.assertTrue(frappe.db.exists("Stock Entry", res["stock_entry"]))
			# sisi gudang lagi: batalkan request kedua (gate ROLES_GUDANG)
			wo2, _ = self._lot_ready(30)
			mr2 = create_request(wo2.name)["material_request"]
			cancel_request(mr2)
			self.assertEqual(frappe.db.get_value("Material Request", mr2, "docstatus"), 2)
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

	# ---------------- W19. shared-box groups (Handover Box Plan endpoint)

	@classmethod
	def _group_item(cls):
		"""A FRESH batchless Pack item per test — group fixtures must never
		share a pool (batchless totals accumulate across the class-rollback
		run, so a shared item would couple the group tests to test order)."""
		fg = cls._make_item(f"{PREFIX}-FGGRP-{random_string(6).upper()}", batch=False)
		cls._set_pack(fg)
		return fg, cls._make_bom(fg)

	@classmethod
	def _group_ready(cls, *qtys):
		"""Ready lots of ONE fresh item: (item_code, (wo, ...)). Batchless —
		no Batch exists, so unlike _lot_ready this skips the batch lookup."""
		fg, bom = cls._group_item()
		wos = []
		for q in qtys:
			wo = cls._make_wo(q, item=fg, bom_no=bom)
			cls._transfer(wo)
			cls._manufacture(wo, q)
			wos.append(wo)
		return fg, wos

	def _group_request(self, wos):
		"""create_group_request as the gudang actor (FU96: qty-only; plan tetap
		dibuat sebagai penanda grup)."""
		frappe.set_user(self.gudang)
		try:
			return create_group_request([w.name for w in wos])
		finally:
			frappe.set_user("Administrator")

	def _legacy_group(self, wos):
		"""Pre-2026-10-10 group shape (still live in data): one Handover Box
		Plan + one single-row MR per WO linking it."""
		fg = wos[0].production_item
		plan = frappe.get_doc(
			{"doctype": "Handover Box Plan", "item_code": fg, "boxes": [{"kg": 0, "qty": 1}]}
		).insert()
		mr_names = []
		for wo in wos:
			wo.reload()
			lot = _checked_lot(wo.name)
			mr = _insert_submitted_handover_mr(
				[(wo, flt(wo.produced_qty), self.uom, _pool_warehouse(lot))],
				self.target_wh, wo.company, box_plan=plan.name,
			)
			mr_names.append(mr.name)
		return {"box_plan": plan.name, "material_requests": mr_names}

	def test_bulk_group_request_one_mr_rows_per_wo(self):
		"""2026-10-10: dua WO satu item → SATU MR submitted, satu baris per WO
		(qty penuh, custom_work_order), tanpa Handover Box Plan; kedua WO Link
		ke MR yang sama. Produksi tidak bisa memanggil endpoint."""
		fg, (wo1, wo2) = self._group_ready(60, 40)

		frappe.set_user(self.prod)
		try:
			with self.assertRaises(frappe.PermissionError):
				create_group_request([wo1.name, wo2.name])
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._bound_mr_count(wo1.name), 0)  # denied before writes

		plans_before = frappe.db.count("Handover Box Plan")
		result = self._group_request((wo1, wo2))
		self.assertTrue(result["ok"])
		self.assertIsNone(result["box_plan"])
		self.assertEqual(result["expected_unit_count"], 20)
		self.assertEqual(result["material_requests"], [result["material_request"]])
		self.assertEqual(frappe.db.count("Handover Box Plan"), plans_before)

		mr = frappe.get_doc("Material Request", result["material_request"])
		self.assertEqual(mr.docstatus, 1)
		self.assertEqual(mr.material_request_type, "Material Transfer")
		self.assertFalse(mr.custom_handover_box_plan)
		self.assertEqual(mr.set_warehouse, self.target_wh)
		self.assertEqual({i.custom_work_order: flt(i.qty) for i in mr.items}, {wo1.name: 60, wo2.name: 40})
		for wo in (wo1, wo2):
			summary = self._summary(wo.name)
			self.assertEqual(summary.custom_handover_material_request, mr.name)
			self.assertEqual(summary.custom_handover_status, "Diminta Gudang")

	def test_bulk_board_one_row_per_wo_and_reservation(self):
		fg, (wo1, wo2) = self._group_ready(60, 40)
		before = self._lot(handover_board(), wo1.name)["available_qty"]
		mr = self._group_request((wo1, wo2))["material_request"]
		board = handover_board()
		rows = {r["work_order"]: r for r in board["requests"] if r["mr"] == mr}
		self.assertEqual(set(rows), {wo1.name, wo2.name})
		self.assertEqual((rows[wo1.name]["qty"], rows[wo2.name]["qty"]), (60, 40))
		self.assertEqual({r["group_size"] for r in rows.values()}, {2})
		self.assertEqual({r["lane"] for r in rows.values()}, {"request"})
		# batchless: one item pool, every row of the MR holds from it
		self.assertEqual(self._lot(board, wo1.name)["reserved_qty"], 100)
		self.assertEqual(self._lot(board, wo2.name)["reserved_qty"], 100)
		self.assertEqual(self._lot(board, wo1.name)["available_qty"], before - 100)

	def test_bulk_cancel_clears_every_wo(self):
		fg, (wo1, wo2) = self._group_ready(60, 40)
		mr = self._group_request((wo1, wo2))["material_request"]
		frappe.set_user(self.gudang)
		try:
			cancel_request(mr)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Material Request", mr, "docstatus"), 2)
		for wo in (wo1, wo2):
			summary = self._summary(wo.name)
			self.assertIsNone(summary.custom_handover_material_request)
			self.assertFalse(summary.custom_handover_status)

	def test_bulk_send_one_se_links_rows_all_wo_terkirim(self):
		fg, (wo1, wo2) = self._group_ready(60, 40)
		mr = frappe.get_doc("Material Request", self._group_request((wo1, wo2))["material_request"])
		cold_before = flt(_item_stock(fg, self.cold_wh))
		result = self._send(mr)
		self.assertEqual(result["qty"], 100)
		self.assertEqual(sorted(result["work_orders"]), sorted([wo1.name, wo2.name]))
		se = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual(se.docstatus, 1)
		row_by_wo = {i.custom_work_order: i.name for i in mr.items}
		self.assertEqual(
			sorted((d.material_request, d.material_request_item, flt(d.qty)) for d in se.items),
			sorted([(mr.name, row_by_wo[wo1.name], 60), (mr.name, row_by_wo[wo2.name], 40)]),
		)
		self.assertEqual(flt(_item_stock(fg, self.cold_wh)), cold_before - 100)
		self.assertEqual(frappe.db.get_value("Material Request", mr.name, "status"), "Transferred")
		for wo in (wo1, wo2):
			self.assertEqual(self._summary(wo.name).custom_handover_status, "Terkirim")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._send(mr)
		self.assertIn("duplikat", str(ctx.exception))
		self.assertEqual(
			len({d.parent for d in frappe.get_all(
				"Stock Entry Detail", filters={"material_request": mr.name, "docstatus": 1}, fields=["parent"]
			)}),
			1,
		)

	def test_bulk_send_batch_rows_keep_own_batch(self):
		wo1, b1 = self._lot_ready(30)
		wo2, b2 = self._lot_ready(20)
		mr = frappe.get_doc("Material Request", self._group_request((wo1, wo2))["material_request"])
		se = frappe.get_doc("Stock Entry", self._send(mr)["stock_entry"])
		row_wo = {i.name: i.custom_work_order for i in mr.items}
		self.assertEqual(
			{row_wo[d.material_request_item]: (d.batch_no, flt(d.qty)) for d in se.items},
			{wo1.name: (b1, 30), wo2.name: (b2, 20)},
		)
		self.assertEqual(flt(get_batch_qty(b1, self.target_wh)), 30)
		self.assertEqual(flt(get_batch_qty(b2, self.target_wh)), 20)

	def test_bulk_send_total_short_zero_writes(self):
		fg, (wo1, wo2) = self._group_ready(60, 40)
		mr = frappe.get_doc("Material Request", self._group_request((wo1, wo2))["material_request"])
		drain = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer",
				"company": self.company,
				"items": [
					{
						"item_code": fg, "qty": flt(_item_stock(fg, self.cold_wh)) - 70,
						"basic_rate": 10, "s_warehouse": self.cold_wh,
						"t_warehouse": self.target_wh, "use_serial_batch_fields": 0,
					}
				],
			}
		)
		self._pin_posting(drain, "11:00:00")
		drain.insert()
		drain.submit()
		self.assertEqual(flt(_item_stock(fg, self.cold_wh)), 70)  # each row fits, total does not
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._send(mr)
		self.assertIn("tidak bisa mengirim", str(ctx.exception))
		self.assertFalse(frappe.get_all("Stock Entry Detail", filters={"material_request": mr.name}))

	def test_bulk_group_request_rejects_mixed_company(self):
		fg, (wo1, wo2) = self._group_ready(60, 40)
		other = frappe.db.get_value("Company", {"name": ("!=", self.company)}, "name")
		if not other:
			self.skipTest("site punya satu company")
		frappe.db.set_value("Work Order", wo2.name, "company", other, update_modified=False)
		frappe.set_user(self.gudang)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				create_group_request([wo1.name, wo2.name])
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("Work Order", wo2.name, "company", self.company, update_modified=False)
		self.assertIn("satu company", str(ctx.exception))
		self.assertEqual(self._bound_mr_count(wo1.name), 0)

	def test_bulk_group_request_rejects_wo_with_active_request(self):
		fg, (wo1, wo2) = self._group_ready(60, 40)
		self._request(wo1)
		frappe.set_user(self.gudang)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				create_group_request([wo1.name, wo2.name])
		finally:
			frappe.set_user("Administrator")
		self.assertIn("sudah punya permintaan aktif", str(ctx.exception))
		self.assertEqual(self._bound_mr_count(wo2.name), 0)

	def test_fu96_group_request_rejections_are_zero_write(self):
		"""FU96: minimal 2 WO dan item campur tetap ditolak dgn pesan Indonesia
		+ ZERO tulisan (tanpa plan, tanpa MR); payload boxes dari pemanggil lama
		difilter frappe.get_newargs (kwarg tak dikenal) — kontrak qty-only."""
		fg, (wo1, wo2) = self._group_ready(60, 40)
		fg2, bom2 = self._group_item()
		wo_other = self._make_wo(50, item=fg2, bom_no=bom2)
		self._transfer(wo_other)
		self._manufacture(wo_other, 50)
		plans_before = frappe.db.count("Handover Box Plan")
		mrs_before = self._bound_mr_count(wo1.name) + self._bound_mr_count(wo2.name)

		def rejects(work_orders, part):
			frappe.set_user(self.gudang)
			try:
				with self.assertRaises(frappe.ValidationError) as ctx:
					create_group_request(work_orders)
			finally:
				frappe.set_user("Administrator")
			self.assertIn(part, str(ctx.exception))

		# a group needs >= 2 distinct Work Orders (duplicates dedupe to one)
		rejects([wo1.name], "minimal 2 Work Order")
		rejects([wo1.name, wo1.name], "minimal 2 Work Order")
		# ALL WOs must share one production_item (the distinct items are listed)
		rejects([wo1.name, wo_other.name], "satu item")
		rejects([wo1.name, wo_other.name], fg2)

		# signature: qty-only MURNI; kwarg boxes lama difilter get_newargs
		import inspect

		params = inspect.signature(create_group_request).parameters
		self.assertNotIn("boxes", params)
		self.assertEqual(
			frappe.get_newargs(create_group_request, {"work_orders": "[]", "boxes": "[{}]"}),
			{"work_orders": "[]"},
		)

		self.assertEqual(frappe.db.count("Handover Box Plan"), plans_before)
		self.assertEqual(
			self._bound_mr_count(wo1.name) + self._bound_mr_count(wo2.name), mrs_before
		)

	def test_fu96_group_member_cancel_blocked_until_group_cancel(self):
		"""cancel_request refuses a member while any sibling on the plan is
		still active (group message names the plan); cancel_group_request
		cancels every still-active member in one call, clears each WO summary,
		and leaves the plan doc as the audit record; once no sibling is live a
		plain cancel_request falls through to the normal docstatus guard."""
		fg, (wo1, wo2) = self._group_ready(60, 40)
		result = self._legacy_group((wo1, wo2))
		mr1, mr2 = sorted(result["material_requests"])

		frappe.set_user(self.gudang)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				cancel_request(mr1)
			self.assertIn("bagian grup serah terima", str(ctx.exception))
			self.assertIn(result["box_plan"], str(ctx.exception))
			self.assertEqual(frappe.db.get_value("Material Request", mr1, "docstatus"), 1)

			done = cancel_group_request(result["box_plan"])
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(done["ok"])
		self.assertEqual(done["cancelled"], [mr1, mr2])
		self.assertEqual(done["skipped"], [])
		for mr_name in (mr1, mr2):
			self.assertEqual(frappe.db.get_value("Material Request", mr_name, "docstatus"), 2)
		for wo in (wo1, wo2):
			summary = self._summary(wo.name)
			self.assertIsNone(summary.custom_handover_material_request)
			self.assertFalse(summary.custom_handover_status)
		self.assertTrue(frappe.db.exists("Handover Box Plan", result["box_plan"]))
		# board row shows the cancelled members with the group marker resolved
		row = self._req(handover_board(), mr1)
		self.assertEqual(row["flag"], "cancelled")
		self.assertEqual(row["group_size"], 0)  # no live members anymore

		# no live sibling remains -> a plain member cancel is no longer blocked
		# by the group guard (it falls through to the docstatus refusal)
		frappe.set_user(self.gudang)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				cancel_request(mr1)
			self.assertIn("docstatus 2", str(ctx.exception))
		finally:
			frappe.set_user("Administrator")

	def test_fu96_group_cancel_leaves_shipped_members_alone(self):
		"""A member with submitted Stock Entry evidence is OUT of the group
		cancel (barangnya sudah keluar) — only the still-active siblings are
		cancelled; the shipped MR stays submitted."""
		fg, (wo1, wo2) = self._group_ready(60, 40)
		result = self._legacy_group((wo1, wo2))
		wo_by_mr = {
			frappe.db.get_value("Material Request Item", {"parent": n}, "custom_work_order"): n
			for n in result["material_requests"]
		}
		mr_sent = wo_by_mr[wo1.name]
		mr_open = wo_by_mr[wo2.name]
		self._send(frappe.get_doc("Material Request", mr_sent))

		frappe.set_user(self.gudang)
		try:
			done = cancel_group_request(result["box_plan"])
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(done["cancelled"], [mr_open])
		self.assertEqual(done["skipped"], [mr_sent])
		self.assertEqual(frappe.db.get_value("Material Request", mr_sent, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Material Request", mr_open, "docstatus"), 2)
		# the shipped member keeps its summary (Terkirim, Link retained)
		summary = self._summary(wo1.name)
		self.assertEqual(summary.custom_handover_material_request, mr_sent)
		self.assertEqual(summary.custom_handover_status, "Terkirim")
		self.assertIsNone(self._summary(wo2.name).custom_handover_material_request)

	# ---------------- FU71. qty-only contract (box arguments omitted entirely)

	def test_fu96_create_request_qty_only_full_guards(self):
		"""FU96: create_request(work_order) = kontrak qty-only satu-satunya:
		MR = Material Transfer full-qty yang identik, ringkasan WO = Link +
		status (tanpa kunci box di respons/board), dan guard duplikat tetap
		menolak panggilan kedua dengan zero writes."""
		fg, (wo,) = self._group_ready(100)  # batchless, factor 5: 100 -> 20 Pack

		frappe.set_user(self.gudang)
		try:
			result = create_request(wo.name)
			# a second qty-only call hits the active-request guard, zero writes
			before = self._summary(wo.name)
			with self.assertRaises(frappe.ValidationError) as ctx:
				create_request(wo.name)
			self.assertIn("sudah punya permintaan aktif", str(ctx.exception))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._bound_mr_count(wo.name), 1)
		self.assertEqual(self._summary(wo.name), before)

		self.assertTrue(result["ok"])
		self.assertEqual(result["expected_unit_count"], 20)
		self.assertEqual(result["unit"], "Pack")
		for key in ("box_1", "box_1_qty", "box_2", "box_2_qty", "box_3", "box_3_qty"):
			self.assertNotIn(key, result)
		produced = flt(frappe.db.get_value("Work Order", wo.name, "produced_qty"))
		mr = frappe.get_doc("Material Request", result["material_request"])
		self.assertEqual(mr.docstatus, 1)
		self.assertEqual(mr.material_request_type, "Material Transfer")
		self.assertEqual(flt(mr.items[0].qty), produced)  # R3: full qty
		self.assertEqual(mr.items[0].stock_uom, self.uom)
		self.assertEqual(mr.items[0].uom, mr.items[0].stock_uom)
		self.assertEqual(mr.items[0].custom_work_order, wo.name)
		self.assertFalse(mr.custom_handover_box_plan)

		summary = self._summary(wo.name)
		self.assertEqual(summary.custom_handover_material_request, mr.name)
		self.assertEqual(summary.custom_handover_status, "Diminta Gudang")

		# the board request row shows lane request with NO box keys at all
		row = self._req(result["board"], mr.name)
		self.assertEqual(row["lane"], "request")
		self.assertNotIn("boxes", row)
		self.assertNotIn("box_1", row)

	def test_fu96_create_request_role_and_fractional_guards(self):
		"""FU96 edges: Produksi ditolak sebelum tulisan apa pun; produksi qty
		yang tidak membentuk Pack utuh tetap zero-write rejection qty-only."""
		fg, (wo,) = self._group_ready(60)  # expected 12 Pack

		frappe.set_user(self.prod)
		try:
			with self.assertRaises(frappe.PermissionError):
				create_request(wo.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._bound_mr_count(wo.name), 0)

		# fractional output: 7 pcs at factor 5 = 1.4 Pack -> qty-only refuses
		fg2, (wo2,) = self._group_ready(7)
		self._assert_zero_write_rejection(wo2, {}, "tidak membentuk")

	def test_fu96_lot_short_rejected_zero_writes(self):
		"""FU96: the lot-availability guard sits directly below the count guard
		and stays authoritative — a drained batchless pool rejects the full-qty
		request with the diminta/tersedia message and ZERO writes (staging
		mirrors t31: plain desk transfer out of the pool)."""
		fg, (wo,) = self._group_ready(60)  # fresh item: the pool is exactly 60
		pool = flt(_item_stock(fg, self.cold_wh))
		self.assertGreaterEqual(pool, 60)
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer",
				"company": self.company,
				"items": [
					{
						"item_code": fg,
						"qty": pool - 50,
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
		self.assertEqual(flt(_item_stock(fg, self.cold_wh)), 50)

		self._assert_zero_write_rejection(wo, {}, "tersedia")

	def test_fu96_legacy_group_plan_board_and_cancel(self):
		"""Legacy plan groups (data before 2026-10-10) keep their board marker
		(box_plan + live member count) and cancel_group_request."""
		fg, (wo1, wo2) = self._group_ready(60, 40)
		result = self._legacy_group((wo1, wo2))
		board = handover_board()
		for mr_name in result["material_requests"]:
			row = self._req(board, mr_name)
			self.assertEqual(row["lane"], "request")
			self.assertEqual(row["box_plan"], result["box_plan"])
			self.assertEqual(row["group_size"], 2)
			self.assertNotIn("box_1", row)
			self.assertNotIn("group_boxes", row)

		# the group cancel mechanism survives qty-only (it rides the plan)
		frappe.set_user(self.gudang)
		try:
			done = cancel_group_request(result["box_plan"])
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(done["ok"])
		for mr_name in result["material_requests"]:
			self.assertEqual(frappe.db.get_value("Material Request", mr_name, "docstatus"), 2)

	def test_reco_pool_balance_not_ledger_sum(self):
		"""RECO posts actual_qty=0 with the balance in qty_after_transaction —
		_item_stock must read the Bin balance, not SUM(SLE.actual_qty); a
		timing correction like RECO-JURI-261020003 would otherwise shrink the
		batchless pool (cloud: Bin 1,023 vs SUM 152)."""
		fg, (wo,) = self._group_ready(60)
		self.assertEqual(flt(_item_stock(fg, self.cold_wh)), 60)
		reco = frappe.get_doc(
			{
				"doctype": "Stock Reconciliation",
				"company": self.company,
				"purpose": "Stock Reconciliation",
				"posting_date": today(),
				"posting_time": "11:30:00",
				"set_posting_time": 1,
				"items": [
					{"item_code": fg, "warehouse": self.cold_wh, "qty": 160, "valuation_rate": 10}
				],
			}
		)
		reco.insert()
		reco.submit()
		self.assertEqual(
			flt(frappe.db.get_value("Bin", {"item_code": fg, "warehouse": self.cold_wh}, "actual_qty") or 0),
			160,
		)
		self.assertEqual(flt(_item_stock(fg, self.cold_wh)), 160)
		self.assertEqual(flt(self._request(wo)["qty"]), 60)
