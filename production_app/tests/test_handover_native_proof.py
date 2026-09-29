# T21 handover proof — native Material Request -> Stock Entry path (isolated records)
#
# Proves, by execution on the installed runtime, the native flow the Serah
# Terima design (HANDOVER_PLAN.md) builds on:
# - MR (Material Transfer) with set_from_warehouse / set_warehouse and an item
#   row using the NATIVE Material Request Item.from_warehouse -> warehouse;
# - native `make_stock_entry` from the MR maps s_warehouse/t_warehouse and the
#   remaining quantity;
# - batch set on the SE row (old batch_no fields + use_serial_batch_fields)
#   becomes a Serial and Batch Bundle at submit (v16) and the batch stock
#   moves warehouse-wise (get_batch_qty) while MR ordered_qty/status follow;
# - MR stop (short-close) blocks further sends natively; MR cancel releases;
# - the bakery_manufacturing bundle override does not touch outward transfer
#   rows (guard: Manufacture + Inward only) — proven by execution and source;
# - permission matrix: Stock User can run the full flow, a bare user cannot;
# - the qtyInPack source the WO workspace uses (api/work_order._enrich_units);
# - the Manufacture-SE -> batch resolution query for a WO (read-only on
#   operational documents).
#
# Every record created here is test-only (items/warehouses/users/docs prefixed
# T21/t21); the Frappe test framework rolls each run back. The two REAL
# warehouses (Cold Storage Produksi - ROPI / Gudang Barang Jadi - ROPI) are
# only read for configuration (their parent group warehouse), never written.

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, now, random_string

from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.material_request.material_request import (
	make_stock_entry as make_mr_stock_entry,
)
from erpnext.stock.doctype.material_request.material_request import (
	update_status as update_mr_status,  # whitelisted: checks write permission
)
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	BatchNegativeStockError,
)
from erpnext.stock.stock_ledger import NegativeStockError

PREFIX = "T21"

# Expected MR status labels for Material Transfer (native status_map, verified
# in erpnext/controllers/status_updater.py):
STATUS_PENDING = "Pending"
STATUS_PARTIAL = "Partially Received"  # native label for partial Material Transfer
STATUS_TRANSFERRED = "Transferred"
STATUS_STOPPED = "Stopped"
STATUS_CANCELLED = "Cancelled"

# The 10 fields the handover design plans (HANDOVER_PLAN.md §3); created and
# left in place by T21 (controller ruling 3), reused idempotently by T22.
# Boxes are Float kg weights (T31, R8): written at Verifikasi Siap Kirim on
# the submitted MR, allow_on_submit, never converted to PCS.
PLANNED_MR_FIELDS = [
	("Material Request", "custom_box_1", "Float", 1),
	("Material Request", "custom_box_2", "Float", 1),
	("Material Request", "custom_good_qty_postpacking", "Float", 1),
	("Material Request", "custom_reject_qty_postpacking", "Float", 1),
	("Material Request", "custom_trial_qty_postpacking", "Float", 1),
	("Material Request", "custom_sisa_qty_postpacking", "Float", 1),
	("Material Request", "custom_jam_packing", "Time", 1),
	("Material Request", "custom_qc_packing", "Link", 1),
	("Material Request", "custom_postpacking_confirmed", "Check", 1),
	("Material Request Item", "custom_work_order", "Link", 0),
]


def _company_config():
	"""Read-only: company + stock UOM from an existing WO; parent group of the
	real Cold Storage warehouse (configuration read, never a write target)."""
	name = frappe.db.get_value(
		"Work Order",
		{"docstatus": 1, "fg_warehouse": ("is", "set")},
		"name",
		order_by="creation desc",
	)
	cfg = frappe.db.get_value("Work Order", name, ["company", "stock_uom"], as_dict=True)
	cold_storage = "Cold Storage Produksi - ROPI"
	parent = frappe.db.get_value("Warehouse", cold_storage, "parent_warehouse")
	if not parent:
		parent = frappe.db.get_value(
			"Warehouse", {"company": cfg.company, "is_group": 1}, "name", order_by="creation desc"
		)
	return cfg, parent


def _make_batch_item(code, group, uom):
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
			"has_batch_no": 1,
			"create_new_batch": 1,
			"standard_rate": 100,
			"is_fixed_asset": 0,
			"opening_stock": 0,
		}
	)
	doc.insert()
	return doc


class TestHandoverNativeProof(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		(cfg, parent_wh) = _company_config()
		cls.company = cfg.company
		cls.uom = cfg.stock_uom
		cls.group = frappe.db.get_value("Item Group", {}, "name")
		suffix = random_string(6).upper()

		# dedicated T21 warehouses (parent: the real Cold Storage's group)
		cls.src_wh = (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": f"{PREFIX} Cold Storage {suffix}",
					"company": cls.company,
					"parent_warehouse": parent_wh,
					"is_group": 0,
				}
			)
			.insert()
			.name
		)
		cls.dst_wh = (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": f"{PREFIX} Gudang Jadi {suffix}",
					"company": cls.company,
					"parent_warehouse": parent_wh,
					"is_group": 0,
				}
			)
			.insert()
			.name
		)

		# one shared lot for non-quantity-sensitive tests; quantity-sensitive
		# tests build FRESH items/batches (receipts accumulate within a run)
		cls.item = _make_batch_item(f"{PREFIX}-ITEM-{suffix}", cls.group, cls.uom).name
		cls.batch = cls._new_batch(cls.item, f"{PREFIX}-BATCH-{suffix}")
		cls._receipt(cls.item, 500, cls.batch)

	# ------------------------------------------------------------- fixtures

	@staticmethod
	def _new_batch(item_code, batch_id):
		return (
			frappe.get_doc({"doctype": "Batch", "batch_id": batch_id, "item": item_code})
			.insert()
			.name
		)

	@classmethod
	def _receipt(cls, item, qty, batch):
		"""Material Receipt of a batch-tracked item via the OLD batch fields
		(use_serial_batch_fields=1) — v16 converts them to a bundle on submit."""
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": cls.company,
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"basic_rate": 10,
						"t_warehouse": cls.src_wh,
						"use_serial_batch_fields": 1,
						"batch_no": batch,
					}
				],
			}
		)
		se.insert()
		se.submit()
		return se

	def _fresh_lot(self, qty):
		"""Fresh item + batch + receipt, for quantity-sensitive assertions."""
		suffix = random_string(6).upper()
		item = _make_batch_item(f"{PREFIX}-FRESH-{suffix}", self.group, self.uom).name
		batch = self._new_batch(item, f"{PREFIX}-FBATCH-{suffix}")
		self._receipt(item, qty, batch)
		return item, batch

	def _make_mr(self, item, qty):
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Material Transfer",
				"company": self.company,
				"transaction_date": now(),
				"schedule_date": add_days(now(), 1),
				"set_from_warehouse": self.src_wh,
				"set_warehouse": self.dst_wh,
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"uom": self.uom,
						"from_warehouse": self.src_wh,
						"warehouse": self.dst_wh,
						"schedule_date": add_days(now(), 1),
					}
				],
			}
		)
		mr.insert()
		mr.submit()
		return mr

	def _build_se(self, mr):
		"""Native builder: unsaved Stock Entry mapped from the MR."""
		return frappe.get_doc(make_mr_stock_entry(mr.name))

	def _send(self, mr, qty, batch):
		"""The §4.7 send shape: build from MR, set qty + batch, submit."""
		se = self._build_se(mr)
		self.assertEqual(len(se.items), 1)
		row = se.items[0]
		row.qty = qty
		row.transfer_qty = qty * flt(row.conversion_factor or 1)
		row.use_serial_batch_fields = 1
		row.batch_no = batch
		se.insert()
		se.submit()
		return se

	def _bundle_entries(self, se):
		return frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": ("in", [r.serial_and_batch_bundle for r in se.items if r.serial_and_batch_bundle])},
			fields=["parenttype", "batch_no", "qty"],
		)

	# ------------------------------------------------- planned field schema

	def test_t21_planned_mr_custom_fields_schema(self):
		"""Draft-schema check: the 10 planned fields exist with exact names,
		types and allow_on_submit (created by T21, reused by T22)."""
		for dt, fieldname, fieldtype, aos in PLANNED_MR_FIELDS:
			meta = frappe.get_meta(dt)
			self.assertTrue(meta.has_field(fieldname), f"{dt}.{fieldname} missing")
			df = meta.get_field(fieldname)
			self.assertEqual(df.fieldtype, fieldtype, f"{dt}.{fieldname} type")
			self.assertEqual(bool(df.allow_on_submit), bool(aos), f"{dt}.{fieldname} allow_on_submit")

	# ------------------------------------------------- native MR -> SE path

	def test_t21_native_item_from_warehouse_and_builder_mapping(self):
		"""Material Request Item.from_warehouse is native; the MR -> SE builder
		maps s_warehouse/t_warehouse from the row and defaults to remaining qty."""
		self.assertTrue(frappe.get_meta("Material Request Item").has_field("from_warehouse"))
		item, batch = self._fresh_lot(100)
		mr = self._make_mr(item, 100)
		self.assertEqual(mr.docstatus, 1)
		self.assertEqual(mr.status, STATUS_PENDING)
		self.assertEqual(mr.items[0].from_warehouse, self.src_wh)
		self.assertEqual(mr.items[0].warehouse, self.dst_wh)
		self.assertEqual(flt(mr.items[0].ordered_qty), 0)

		se = self._build_se(mr)
		self.assertEqual(se.purpose, "Material Transfer")
		self.assertEqual(se.from_warehouse, self.src_wh)
		self.assertEqual(se.to_warehouse, self.dst_wh)
		row = se.items[0]
		self.assertEqual(row.s_warehouse, self.src_wh)
		self.assertEqual(row.t_warehouse, self.dst_wh)
		self.assertEqual(flt(row.qty), 100)

	def test_t21_full_send_moves_batch_and_updates_mr(self):
		"""Full send: batch stock moves src -> dst warehouse-wise, MR goes
		Transferred with ordered_qty = qty, transfer_status Completed."""
		item, batch = self._fresh_lot(100)
		mr = self._make_mr(item, 100)
		se = self._send(mr, 100, batch)

		# v16 batch truth: old batch_no field kept AND converted to a bundle
		row = frappe.get_doc("Stock Entry", se.name).items[0]
		self.assertEqual(row.batch_no, batch)
		self.assertTrue(row.serial_and_batch_bundle, "bundle must exist on submit")
		entries = self._bundle_entries(se)
		self.assertEqual(len(entries), 1)
		self.assertEqual(entries[0].batch_no, batch)
		# v16 convention: OUTWARD bundle entries store the qty NEGATIVE
		self.assertEqual(flt(entries[0].qty), -100)

		# ledger: exactly -100 in source, +100 in target
		ledger = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": se.name, "is_cancelled": 0},
			fields=["warehouse", "actual_qty"],
		)
		by_wh = {r.warehouse: flt(r.actual_qty) for r in ledger}
		self.assertEqual(by_wh.get(self.src_wh), -100)
		self.assertEqual(by_wh.get(self.dst_wh), 100)

		# live batch-wise quantity (HANDOVER_PLAN §4.2 helper)
		self.assertEqual(flt(get_batch_qty(batch, self.dst_wh)), 100)
		self.assertEqual(flt(get_batch_qty(batch, self.src_wh) or 0), 0)

		# MR follow-up
		mr.reload()
		self.assertEqual(flt(mr.items[0].ordered_qty), 100)
		self.assertEqual(flt(mr.per_ordered), 100)
		self.assertEqual(mr.status, STATUS_TRANSFERRED)
		# NOTE: transfer_status stays empty here — it is only set in the
		# add_to_transit / outgoing_stock_entry (two-step internal transfer)
		# flow, NOT for MR-linked SEs. Status truth is per_ordered/status.

	def test_t21_bakery_override_does_not_touch_outward_rows(self):
		"""Outward transfer row bundle keeps exactly the sent qty (the bakery
		override only resyncs Manufacture + Inward single-entry bundles)."""
		item, batch = self._fresh_lot(60)
		mr = self._make_mr(item, 60)
		se = self._send(mr, 60, batch)

		row = frappe.get_doc("Stock Entry", se.name).items[0]
		bundle = frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle)
		self.assertEqual(bundle.type_of_transaction, "Outward")
		self.assertEqual(bundle.warehouse, self.src_wh)
		# outward bundle carries the qty negative; not rescaled/adjusted by
		# the bakery override (guard: Manufacture + Inward only)
		self.assertEqual(flt(bundle.total_qty), -60)
		entries = self._bundle_entries(se)
		self.assertEqual(len(entries), 1)
		self.assertEqual(flt(entries[0].qty), -60)
		self.assertEqual(flt(row.qty), 60)

	# ------------------------------------------------------ stop and cancel

	def test_t21_partial_send_then_stop_short_closes(self):
		"""good < requested: MR keeps the partial state, Stop short-closes it,
		and further sends against the stopped MR are blocked natively."""
		item, batch = self._fresh_lot(100)
		mr = self._make_mr(item, 100)

		self._send(mr, 80, batch)
		mr.reload()
		self.assertEqual(flt(mr.items[0].ordered_qty), 80)
		self.assertEqual(mr.status, STATUS_PARTIAL)
		# transfer_status is only used by the add_to_transit flow — stays empty

		# stop via the whitelisted native action (checks write permission)
		update_mr_status(mr.name, STATUS_STOPPED)
		mr.reload()
		self.assertEqual(mr.status, STATUS_STOPPED)

		# a further send builds fine (builder checks docstatus only) but is
		# blocked at submit by the native MR follow-up
		se = self._build_se(mr)
		row = se.items[0]
		row.use_serial_batch_fields = 1
		row.batch_no = batch
		frappe.db.savepoint("t21_stopped")
		try:
			self.assertRaises(frappe.InvalidStatusError, se.submit)
		finally:
			frappe.db.rollback(save_point="t21_stopped")

		# nothing extra persisted: one submitted SE (80) against the MR
		sent = frappe.get_all(
			"Stock Entry",
			filters={"material_request": mr.name, "docstatus": 1},
			pluck="name",
		)
		self.assertEqual(len(sent), 1)
		mr.reload()
		self.assertEqual(mr.status, STATUS_STOPPED)
		self.assertEqual(flt(mr.items[0].ordered_qty), 80)
		# native cancel guard (v16): an MR linked with submitted SEs refuses
		# cancel — in the handover design cancel only ever happens BEFORE a
		# send, and stop (short-close) is the terminal state after it
		self.assertRaises(
			frappe.LinkExistsError, frappe.get_doc("Material Request", mr.name).cancel
		)

	def test_t21_cancel_pending_mr_and_builder_refuses(self):
		"""Cancelling a submitted MR with no send: docstatus 2 (also from the
		Stopped state); the native builder refuses a cancelled source doc."""
		item, batch = self._fresh_lot(50)
		mr = self._make_mr(item, 50)
		mr.cancel()
		mr.reload()
		self.assertEqual(mr.docstatus, 2)
		self.assertEqual(mr.status, STATUS_CANCELLED)

		self.assertRaises(Exception, self._build_se, mr)

		# a STOPPED MR without any send can still be cancelled (unstop path)
		mr2 = self._make_mr(item, 10)
		update_mr_status(mr2.name, STATUS_STOPPED)
		frappe.get_doc("Material Request", mr2.name).cancel()  # fresh: status db_set bumped modified
		mr2.reload()
		self.assertEqual(mr2.docstatus, 2)
		self.assertEqual(mr2.status, STATUS_CANCELLED)

	# ------------------------------------------------------- atomicity proof

	def test_t21_insufficient_batch_stock_is_atomic(self):
		"""Sending more than the batch holds: submit fails natively and nothing
		partial survives the request-boundary rollback."""
		item, batch = self._fresh_lot(30)
		mr = self._make_mr(item, 50)

		se = self._build_se(mr)
		row = se.items[0]
		row.qty = 50
		row.transfer_qty = 50
		row.use_serial_batch_fields = 1
		row.batch_no = batch

		frappe.db.savepoint("t21_shortage")
		try:
			self.assertRaises((NegativeStockError, BatchNegativeStockError), se.submit)
		finally:
			frappe.db.rollback(save_point="t21_shortage")

		mr.reload()
		self.assertEqual(flt(mr.items[0].ordered_qty), 0)
		self.assertEqual(mr.status, STATUS_PENDING)
		self.assertEqual(
			len(frappe.get_all("Stock Entry", filters={"material_request": mr.name})), 0
		)
		self.assertEqual(
			len(frappe.get_all("Stock Ledger Entry", filters={"voucher_no": se.name})), 0
		)

	# ------------------------------------------------------------ permissions

	def test_t21_permission_matrix_stock_user_vs_bare_user(self):
		"""(a) a user with ONLY the Stock User role can run request -> send;
		(b) a bare user cannot create MR/SE nor stop an MR."""
		suffix = random_string(6).upper()
		stock_user = (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": f"t21.stock.{suffix}@prodapp.example.com",
					"first_name": "T21 Stock",
					"send_welcome_email": 0,
				}
			)
			.insert()
		)
		stock_user.append("roles", {"role": "Stock User"})
		stock_user.save()
		stock_user = stock_user.name

		bare_user = (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": f"t21.bare.{suffix}@prodapp.example.com",
					"first_name": "T21 Bare",
					"send_welcome_email": 0,
				}
			)
			.insert()
			.name
		)

		# stock received as Administrator (Stock User has no Item create)
		item, batch = self._fresh_lot(20)

		# (a) Stock User runs the whole native flow
		frappe.set_user(stock_user)
		try:
			self.assertTrue(frappe.has_permission("Material Request", "create"))
			self.assertTrue(frappe.has_permission("Stock Entry", "create"))
			mr = self._make_mr(item, 20)
			se = self._send(mr, 20, batch)
			self.assertEqual(flt(get_batch_qty(batch, self.dst_wh)), 20)
			mr.reload()
			self.assertEqual(mr.status, STATUS_TRANSFERRED)
			mr_name, se_name = mr.name, se.name
		finally:
			frappe.set_user("Administrator")

		# (b) bare user: no create on MR/SE, no write on MR (cannot stop)
		frappe.set_user(bare_user)
		try:
			self.assertFalse(frappe.has_permission("Material Request", "create"))
			self.assertFalse(frappe.has_permission("Stock Entry", "create"))
			with self.assertRaises(frappe.PermissionError):
				self._make_mr(item, 5)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": "Stock Entry",
						"stock_entry_type": "Material Receipt",
						"company": self.company,
						"items": [],
					}
				).insert()
			with self.assertRaises(frappe.PermissionError):
				update_mr_status(mr_name, STATUS_STOPPED)
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists("Stock Entry", se_name))

	# ---------------------------------------------------- qtyInPack source

	def test_t21_qtyinpack_source_is_item_uom_conversion(self):
		"""The qtyInPack source the WO workspace uses: Item UOM Conversion
		Detail (Item.uoms conversion_factor) selected by
		Item.custom_default_inventory_unit_of_measure — resolved by api.work_order._enrich_units."""
		from production_app.api.work_order import _enrich_units

		suffix = random_string(6).upper()
		item = _make_batch_item(f"{PREFIX}-PACK-{suffix}", self.group, self.uom)
		item.custom_default_inventory_unit_of_measure = "Pack"
		item.append("uoms", {"uom": "Pack", "conversion_factor": 25})
		item.save()

		row = frappe._dict(production_item=item.name, custom_uom=None)
		_enrich_units([row])
		self.assertEqual(row.display_uom, "Pack")
		self.assertEqual(flt(row.display_conversion_factor), 25)  # Pcs per Pack

	# ------------------------- Manufacture-SE -> batch resolution for a WO

	def test_t21_wo_manufacture_batch_resolution_query(self):
		"""Read-only on operational documents: the Manufacture-SE -> batch
		resolution query (the finish() pattern, plus its SQL equivalent)
		returns the WO's FG batch."""
		# find one operational Manufacture with a finished-item bundle
		# (work_order lives on the parent Stock Entry, not on the detail row)
		refs = frappe.db.sql(
			"""select se.name as se, se.work_order as wo, sed.serial_and_batch_bundle as bundle
				from `tabStock Entry` se
				join `tabStock Entry Detail` sed on sed.parent = se.name
				where se.purpose = 'Manufacture' and se.docstatus = 1
				and se.work_order is not null and sed.is_finished_item = 1
				and sed.serial_and_batch_bundle is not null
				order by se.creation desc limit 1""",
			as_dict=1,
		)
		if not refs:
			self.skipTest("no operational Manufacture with a batch bundle to read")
		wo_name, bundle = refs[0].wo, refs[0].bundle

		# API variant (the finish() pattern) — reused as-is by T23/T24
		entries = frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": bundle},
			fields=["batch_no", "qty"],
		)
		self.assertTrue(entries)
		batch = entries[0].batch_no
		self.assertTrue(frappe.db.exists("Batch", batch))

		# SQL equivalent for reports/desk queries
		rows = frappe.db.sql(
			"""select sbe.batch_no
				from `tabStock Entry Detail` sed
				join `tabSerial and Batch Entry` sbe on sbe.parent = sed.serial_and_batch_bundle
				where sed.parent in (
						select name from `tabStock Entry`
						where work_order = %(wo)s and purpose = 'Manufacture' and docstatus = 1)
				and sed.is_finished_item = 1""",
			{"wo": wo_name},
			as_dict=1,
		)
		self.assertIn(batch, [r.batch_no for r in rows])
