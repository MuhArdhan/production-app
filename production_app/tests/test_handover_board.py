# T23 board read API tests — Serah Terima kanban derivation (isolated records)
#
# Proves, by execution: lot derivation from WO Manufacture SEs (FIFO by posting,
# enrichment incl. qtyInPack via _enrich_units, physical from the live ledger),
# §4.3 reserved/available math, lane transitions by DIRECT document mutation
# (submit MR -> postpacking confirmed -> submitted SE), legacy multi-manufacture
# WOs as explicit unsupported cards, the empty-lot drop rule, the older-lot
# hint, session-user permission filtering, and the target-warehouse setting.
#
# Every record is test-only (T23/t23-prefixed); the Frappe test framework rolls
# each run back. The two real warehouses are never written.

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, now, random_string, today

from contextlib import contextmanager
from unittest.mock import patch

from erpnext.manufacturing.doctype.work_order.work_order import (
	make_stock_entry as make_wo_stock_entry,
)
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.material_request.material_request import (
	make_stock_entry as make_mr_stock_entry,
)
from erpnext.stock.doctype.material_request.material_request import (
	update_status as update_mr_status,
)

from production_app.api.handover import handover_board
from production_app.api.work_order import (
	_enrich_units,
	_warehouse_defaults,
	warehouse_defaults_save,
)

PREFIX = "T23"


def _wo_config():
	"""Read-only: company + stock UOM from an existing WO; warehouse parent
	group from that WO's fg_warehouse (configuration read, never a write)."""
	name = frappe.db.get_value(
		"Work Order",
		{"docstatus": 1, "fg_warehouse": ("is", "set")},
		"name",
		order_by="creation desc",
	)
	cfg = frappe.db.get_value(
		"Work Order", name, ["company", "stock_uom", "fg_warehouse"], as_dict=True
	)
	parent = frappe.db.get_value("Warehouse", cfg.fg_warehouse, "parent_warehouse")
	return cfg, parent


def _make_item(code, group, uom, batch=True, item_name=None):
	# NOTE: on this site Item names come from a series (name/item_code become
	# ITEM000NN); `item_name` keeps the given display name.
	return (
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": item_name or code,
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
		.insert()
		.name
	)


class TestHandoverBoard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		(cfg, parent) = _wo_config()
		cls.company = cfg.company
		cls.uom = cfg.stock_uom
		cls.group = frappe.db.get_value("Item Group", {}, "name")
		suffix = random_string(6).upper()

		def warehouse(name):
			return (
				frappe.get_doc(
					{
						"doctype": "Warehouse",
						"warehouse_name": f"{PREFIX} {name} {suffix}",
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
		cls.cold_wh = warehouse("Cold Storage")  # the lots' warehouse (WO fg)
		cls.target_wh = warehouse("Target")  # the handover target

		cls.rm = _make_item(f"{PREFIX}-RM-{suffix}", cls.group, cls.uom, batch=False)
		# one item per concern: the runner does NOT roll back between tests
		# (state accumulates within the run), so FIFO/hint asserts need items no
		# earlier test can seed same-item lots for
		cls.fg = _make_item(
			f"{PREFIX}-FG-{suffix}", cls.group, cls.uom, item_name=f"T23 FG Display {suffix}"
		)
		cls.fg_name = frappe.db.get_value("Item", cls.fg, "item_name")
		item = frappe.get_cached_doc("Item", cls.fg)
		item.custom_default_inventory_unit_of_measure = "Pack"  # qtyInPack = 25 Pcs/Pack
		item.append("uoms", {"uom": "Pack", "conversion_factor": 25})
		item.save()
		cls.fg2 = _make_item(f"{PREFIX}-FG2-{suffix}", cls.group, cls.uom)
		cls.fg3 = _make_item(
			f"{PREFIX}-FG3-{suffix}", cls.group, cls.uom, item_name=f"T23 FG Fifo A {suffix}"
		)
		item3 = frappe.get_cached_doc("Item", cls.fg3)
		item3.custom_default_inventory_unit_of_measure = "Pack"  # qtyInPack = 25 Pcs/Pack
		item3.append("uoms", {"uom": "Pack", "conversion_factor": 25})
		item3.save()
		cls.fg3_name = frappe.db.get_value("Item", cls.fg3, "item_name")
		cls.fg_hint = _make_item(
			f"{PREFIX}-FGH-{suffix}", cls.group, cls.uom, item_name=f"T23 FG Hint {suffix}"
		)

		cls.bom = cls._make_bom(cls.fg)
		cls.bom2 = cls._make_bom(cls.fg2)
		cls.bom3 = cls._make_bom(cls.fg3)
		cls.bom_hint = cls._make_bom(cls.fg_hint)
		# covers the whole run: no per-test rollback, RM consumption accumulates
		cls._receipt(cls.rm, 6000, cls.src_wh)

	# ------------------------------------------------------------- fixtures

	@staticmethod
	def _pin_posting(se, posting_time):
		"""Pinned same-day posting order keeps the ledger deterministic and the
		FIFO order wall-clock independent:
		receipt 08:00 < transfer 09:00 < manufacture 10:00+ < send/drain 15:00.
		Without set_posting_time=1, Stock Entry resets posting to now()."""
		se.set_posting_time = 1
		se.posting_date = today()
		se.posting_time = posting_time

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

	@classmethod
	def _receipt(cls, item, qty, warehouse, batch=None):
		row = {
			"item_code": item,
			"qty": qty,
			"basic_rate": 10,
			"t_warehouse": warehouse,
			"use_serial_batch_fields": 0,
		}
		if batch:
			row.update({"use_serial_batch_fields": 1, "batch_no": batch})
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": cls.company,
				"items": [row],
			}
		)
		cls._pin_posting(se, "08:00:00")
		se.insert()
		se.submit()
		return se

	@classmethod
	def _make_wo(cls, bom, qty, item, adonan):
		wo = frappe.get_doc(
			{
				"doctype": "Work Order",
				"production_item": item,
				"bom_no": bom,
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
				"custom_adonan_ke": adonan,
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
	def _manufacture(cls, wo, good, posting_time, restore_rm=True):
		"""Submit a Manufacture; posting_time pins the FIFO order deterministically."""
		se = frappe.get_doc(make_wo_stock_entry(wo.name, "Manufacture", qty=good))
		cls._pin_posting(se, posting_time)
		if restore_rm:
			# the workspace consumption rule: RM rows follow the transferred plan
			transferred = frappe._dict()
			for r in frappe.get_all(
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
			):
				transferred[r.item_code] = transferred.get(r.item_code, 0.0) + flt(r.qty)
			for row in se.items:
				if not row.is_finished_item and row.item_code in transferred:
					row.qty = transferred[row.item_code]
					row.transfer_qty = row.qty * flt(row.conversion_factor or 1)
		se.insert()
		se.submit()
		return se

	@classmethod
	def _lot_in_cold(cls, wo):
		"""The FG batch the WO's Manufacture created in the cold warehouse."""
		return frappe.get_all("Batch", filters={"reference_name": wo.name}, pluck="name")[0]

	@classmethod
	def _make_mr(cls, wo, qty):
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Material Transfer",
				"company": cls.company,
				"transaction_date": now(),
				"schedule_date": add_days(now(), 1),
				"set_from_warehouse": cls.cold_wh,
				"set_warehouse": cls.target_wh,
				"custom_box_1": 12.5,
				"custom_box_2": None,  # empty is dropped from display
				"items": [
					{
						"item_code": wo.production_item,
						"qty": qty,
						"uom": cls.uom,
						"from_warehouse": cls.cold_wh,
						"warehouse": cls.target_wh,
						"schedule_date": add_days(now(), 1),
						"custom_work_order": wo.name,
					}
				],
			}
		)
		mr.insert()
		mr.submit()
		return mr

	@classmethod
	def _send(cls, mr, qty, batch):
		"""The proven native send shape: build from MR, set qty + batch, submit."""
		se = frappe.get_doc(make_mr_stock_entry(mr.name))
		assert len(se.items) == 1
		row = se.items[0]
		row.qty = qty
		row.transfer_qty = qty * flt(row.conversion_factor or 1)
		row.use_serial_batch_fields = 1
		row.batch_no = batch
		cls._pin_posting(se, "15:00:00")
		se.insert()
		se.submit()
		return se

	@classmethod
	def _make_user(cls, email, roles):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
			}
		)
		for role in roles:
			user.append("roles", {"role": role})
		user.insert()
		return user.name

	@classmethod
	def _lot(cls, board, wo_name):
		return next(l for l in board["lots"] if l["work_order"] == wo_name)

	@classmethod
	def _req(cls, board, mr_name):
		return next(r for r in board["requests"] if r["mr"] == mr_name)

	@contextmanager
	def _pin_settings(self, **overrides):
		"""FU20: hermetic board reads against the OPERATOR's live warehouse
		defaults (Manufacturing Settings is global, shared production data).
		warehouse_defaults_save clears omitted keys, so pin all six, run the
		body under the overridden values, then restore the prior ones."""
		prior = _warehouse_defaults()
		warehouse_defaults_save(**{**prior, **overrides})
		try:
			yield
		finally:
			warehouse_defaults_save(**prior)

	# ------------------------------------------------- 1. lots: FIFO + truth

	# FU30: test_fu25_gudang_confirmed_hides_lot dipensiun bersama fieldnya —
	# lot kini turun murni dari stok (physical 0) / Status Serah Terima.

	def test_t23_lot_fifo_enrichment_and_physical(self):
		"""Two lots (different items) list FIFO by Manufacture posting; cards
		carry item/adonan/WO display fields, qtyInPack via _enrich_units, and
		physical from the live ledger."""
		wo1 = self._make_wo(self.bom3, 100, self.fg3, "1")
		wo2 = self._make_wo(self.bom2, 200, self.fg2, "2")
		self._transfer(wo1)
		self._transfer(wo2)
		self._manufacture(wo1, 100, "10:00:00")
		self._manufacture(wo2, 200, "11:00:00")

		board = handover_board()
		lot1, lot2 = self._lot(board, wo1.name), self._lot(board, wo2.name)
		self.assertLess(board["lots"].index(lot1), board["lots"].index(lot2))  # FIFO

		self.assertEqual(lot1["batch"], self._lot_in_cold(wo1))
		self.assertEqual(lot1["item_code"], self.fg3)
		self.assertEqual(lot1["item_name"], self.fg3_name)
		self.assertEqual(lot1["adonan_ke"], 1)  # FU69: Int
		self.assertEqual(flt(lot1["qty"]), 100)  # WO target qty
		# T31 (R7): the WO's finished-goods qty + latest Manufacture posting
		self.assertEqual(flt(lot1["produced_qty"]), 100)
		self.assertEqual(lot1["completed_at"], f"{today()} 10:00:00")
		self.assertIn("source_warehouse", board)  # T31 payload key (None until set)
		self.assertEqual(lot1["warehouse"], self.cold_wh)  # derived, not hardcoded
		self.assertEqual(lot1["entered_at"][-8:], "10:00:00")
		# qtyInPack source shared with the WO workspace
		self.assertEqual(flt(lot1["qty_in_pack"]), 25)
		self.assertEqual(lot1["stock_uom"], self.uom)
		# live ledger quantity, zero reservation
		self.assertEqual(flt(lot1["physical_qty"]), 100)
		self.assertEqual(
			flt(lot1["physical_qty"]), flt(get_batch_qty(lot1["batch"], self.cold_wh))
		)
		self.assertEqual(flt(lot1["reserved_qty"]), 0)
		self.assertEqual(flt(lot1["available_qty"]), 100)
		self.assertFalse(lot1["unsupported"])
		self.assertFalse(lot1["has_older_lot_same_item"])
		# second item has no Pack conversion -> factor 1, still enriched
		self.assertEqual(flt(lot2["qty_in_pack"]), 1)
		self.assertEqual(flt(lot2["physical_qty"]), 200)
		self.assertFalse(lot2["has_older_lot_same_item"])  # different item

	def test_fu34_batch_balances_are_bulk_loaded_in_one_query_pair(self):
		"""Two batches of one item in one warehouse use one native bulk lookup,
		while every card keeps the exact native per-batch quantity."""
		wo1 = self._make_wo(self.bom_hint, 50, self.fg_hint, "1")
		wo2 = self._make_wo(self.bom_hint, 60, self.fg_hint, "2")
		self._transfer(wo1)
		self._transfer(wo2)
		self._manufacture(wo1, 50, "10:10:00")
		self._manufacture(wo2, 60, "10:20:00")
		batch1 = self._lot_in_cold(wo1)
		batch2 = self._lot_in_cold(wo2)
		expected = {
			batch1: flt(get_batch_qty(batch1, self.cold_wh)),
			batch2: flt(get_batch_qty(batch2, self.cold_wh)),
		}

		from production_app.api import handover

		available = handover.get_available_batches
		legacy = handover.get_stock_ledgers_batches
		with (
			patch.object(handover, "get_available_batches", wraps=available) as available_call,
			patch.object(handover, "get_stock_ledgers_batches", wraps=legacy) as legacy_call,
		):
			rows = handover._wo_lot_rows(wo_names=[wo1.name, wo2.name])

		lots = {row.batch: row for row in rows}
		self.assertEqual(flt(lots[batch1].physical_qty), expected[batch1])
		self.assertEqual(flt(lots[batch2].physical_qty), expected[batch2])
		self.assertEqual(available_call.call_count, 1)
		self.assertEqual(legacy_call.call_count, 1)
		kwargs = available_call.call_args.args[0]
		self.assertEqual(kwargs.item_code, [self.fg_hint])
		self.assertEqual(kwargs.warehouse, [self.cold_wh])
		self.assertEqual(set(kwargs.batch_no), {batch1, batch2})

	def test_fu34_bulk_balances_preserve_physical_qty_when_sre_exists(self):
		wo = self._make_wo(self.bom3, 50, self.fg3, "1")
		self._transfer(wo)
		self._manufacture(wo, 50, "10:22:00")
		batch = self._lot_in_cold(wo)

		from erpnext.stock.doctype.serial_and_batch_bundle import serial_and_batch_bundle
		from production_app.api import handover

		def reserved_for_item(kwargs):
			if not kwargs.get("item_code"):
				return frappe._dict()
			return frappe._dict(
				{
					(batch, self.cold_wh): frappe._dict(
						batch_no=batch,
						warehouse=self.cold_wh,
						qty=-10,
					)
				}
			)

		with patch.object(
			serial_and_batch_bundle,
			"get_reserved_batches_for_sre",
			side_effect=reserved_for_item,
		):
			expected = flt(get_batch_qty(batch, self.cold_wh))
			actual = handover._batch_quantities([(self.fg3, batch, self.cold_wh)])

		self.assertEqual(expected, 50)
		self.assertEqual(flt(actual[(batch, self.cold_wh)]), expected)

	def test_fu34_checked_lot_does_not_build_the_full_board(self):
		suffix = random_string(4).upper()
		fg = _make_item(f"{PREFIX}-FGSC-{suffix}", self.group, self.uom)
		bom = self._make_bom(fg)
		wo = self._make_wo(bom, 40, fg, "1")
		self._transfer(wo)
		self._manufacture(wo, 40, "10:25:00")

		from production_app.api import handover

		original = handover._wo_lot_rows

		def scoped_only(wo_names=None):
			if wo_names is None:
				raise AssertionError("targeted lot lookup must scope Work Orders")
			return original(wo_names=wo_names)

		with (
			patch.object(
				handover,
				"_build_board",
				side_effect=AssertionError("targeted lot lookup must not build the full board"),
			),
			patch.object(handover, "_wo_lot_rows", side_effect=scoped_only),
		):
			lot = handover._checked_lot(wo.name)

		self.assertEqual(lot.work_order, wo.name)
		self.assertEqual(flt(lot.available_qty), 40)

	# ------------------------------- FU8. batchless lots: item-level pool

	def test_fu8_batchless_pool_math_and_drain(self):
		"""Follow-up 8: a batchless FG item yields batch=None lots whose stock
		is ONE item-level pool — every WO card of the item shows the same
		physical/reserved/available; a request on any WO pools the reservation;
		a drained pool drops the unreferenced cards."""
		# FU20: pool math asserts the SE-derived fallback, so the operator's
		# live source-warehouse setting must not leak into the read.
		with self._pin_settings(handover_source_warehouse=None):
			suffix = random_string(4).upper()
			fg = _make_item(f"{PREFIX}-FGNB-{suffix}", self.group, self.uom, batch=False)
			bom = self._make_bom(fg)
			wo1 = self._make_wo(bom, 60, fg, "1")
			wo2 = self._make_wo(bom, 40, fg, "2")
			self._transfer(wo1)
			self._transfer(wo2)
			self._manufacture(wo1, 60, "10:30:00")
			self._manufacture(wo2, 40, "10:45:00")

			board = handover_board()
			lot1, lot2 = self._lot(board, wo1.name), self._lot(board, wo2.name)
			self.assertTrue(lot1["batchless"])
			self.assertIsNone(lot1["batch"])
			self.assertFalse(lot1["unsupported"])
			self.assertTrue(lot2["batchless"])
			# one shared pool: both cards show the whole item balance, not per-WO
			self.assertEqual(flt(lot1["physical_qty"]), 100)
			self.assertEqual(flt(lot2["physical_qty"]), 100)
			self.assertEqual(flt(lot1["available_qty"]), 100)
			self.assertEqual(flt(lot1["reserved_qty"]), 0)

			mr = self._make_mr(wo1, 100)
			board = handover_board()
			lot1, lot2 = self._lot(board, wo1.name), self._lot(board, wo2.name)
			self.assertEqual(flt(lot1["reserved_qty"]), 100)  # pools across WOs
			self.assertEqual(flt(lot2["reserved_qty"]), 100)  # visible on BOTH cards
			self.assertEqual(flt(lot1["available_qty"]), 0)

			# drain the pool via the native send — batchless shape: NO batch on row
			se = frappe.get_doc(make_mr_stock_entry(mr.name))
			self.assertEqual(len(se.items), 1)
			row = se.items[0]
			self.assertEqual(flt(row.qty), 100)  # builder: stock_qty - ordered_qty
			self._pin_posting(se, "15:00:00")
			se.insert()
			se.submit()
			self.assertFalse(row.batch_no)
			board = handover_board()
			names = {l["work_order"] for l in board["lots"]}
			self.assertIn(wo1.name, names)  # its request still anchors the card
			self.assertNotIn(wo2.name, names)  # empty pool, unanchored -> drops
			lot1 = self._lot(board, wo1.name)
			self.assertEqual(flt(lot1["physical_qty"]), 0)  # pool drained honestly

	def test_fu8_batchless_multi_manufacture_is_supported(self):
		"""Follow-up 8: several Manufactures on a BATCHLESS WO flatten honestly
		(the pool has no batch dimension) — NOT an unsupported card, unlike the
		batch legacy case."""
		with self._pin_settings(handover_source_warehouse=None):
			suffix = random_string(4).upper()
			fg = _make_item(f"{PREFIX}-FGNB2-{suffix}", self.group, self.uom, batch=False)
			bom = self._make_bom(fg)
			wo = self._make_wo(bom, 40, fg, "1")
			self._transfer(wo)
			self._manufacture(wo, 30, "10:00:00", restore_rm=False)
			self._manufacture(wo, 10, "10:30:00", restore_rm=False)

			board = handover_board()
			lot = self._lot(board, wo.name)
			self.assertFalse(lot["unsupported"])
			self.assertTrue(lot["batchless"])
			self.assertIsNone(lot["batch"])
			self.assertEqual(flt(lot["produced_qty"]), 40)  # 30 + 10
			self.assertEqual(lot["completed_at"], f"{today()} 10:30:00")  # LATEST posting
			self.assertEqual(flt(lot["physical_qty"]), 40)
			self.assertEqual(flt(lot["available_qty"]), 40)

	# ------------------- T31 (R2). batchless pool follows the source setting

	def test_t31_batchless_pool_follows_source_setting(self):
		"""With the source setting set, the batchless pool counts stock in the
		SETTING warehouse only (stock elsewhere ignored); an MR created BEFORE
		the setting existed (from_warehouse = SE-derived lot warehouse) keeps
		reserving the pool afterwards (WO-lot-based keying, not from_warehouse)."""
		suffix = random_string(4).upper()
		fg = _make_item(f"{PREFIX}-FGNB3-{suffix}", self.group, self.uom, batch=False)
		bom = self._make_bom(fg)
		pool_wh = (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": f"{PREFIX} Pool {suffix}",
					"company": self.company,
					"parent_warehouse": frappe.db.get_value(
						"Warehouse", self.cold_wh, "parent_warehouse"
					),
					"is_group": 0,
				}
			)
			.insert()
			.name
		)
		wo = self._make_wo(bom, 100, fg, "1")
		self._transfer(wo)
		self._manufacture(wo, 100, "10:00:00")  # SE lands in the cold warehouse
		self._receipt(fg, 300, pool_wh)  # setting-warehouse stock is elsewhere

		# FU20: pin all six defaults — phase 1 must read with NO source setting
		# (SE-derived fallback) regardless of the operator's live value, and
		# the restores never wipe the other defaults.
		with self._pin_settings(handover_source_warehouse=None):
			# legacy MR created BEFORE the setting exists: from_warehouse = cold
			mr = self._make_mr(wo, 40)
			board = handover_board()
			lot = self._lot(board, wo.name)
			self.assertEqual(flt(lot["physical_qty"]), 100)  # SE-derived fallback
			self.assertEqual(flt(lot["reserved_qty"]), 40)

			warehouse_defaults_save(
				**{**_warehouse_defaults(), "handover_source_warehouse": pool_wh}
			)
			board = handover_board()
			self.assertEqual(board["source_warehouse"], pool_wh)
			lot = self._lot(board, wo.name)
			self.assertEqual(flt(lot["physical_qty"]), 300)  # SETTING warehouse counts
			self.assertEqual(flt(lot["reserved_qty"]), 40)  # legacy MR still reserves
			self.assertEqual(flt(lot["available_qty"]), 260)

	# --------------------------------------- 2. reserved / available math

	def test_t23_reserved_available_math(self):
		"""§4.3: reserved sums open (submitted, not stopped/cancelled, SE-less)
		MRs bound to the WO; available is never clamped; cancellation/stop
		release; edge states stay on the board flagged."""
		wo = self._make_wo(self.bom, 100, self.fg, "1")
		self._transfer(wo)
		self._manufacture(wo, 100, "10:00:00")

		mr_a = self._make_mr(wo, 30)
		mr_b = self._make_mr(wo, 50)
		mr_c = self._make_mr(wo, 150)  # over-physical on purpose

		board = handover_board()
		lot = self._lot(board, wo.name)
		self.assertEqual(flt(lot["reserved_qty"]), 230)
		self.assertEqual(flt(lot["available_qty"]), -130)  # negative surfaces as-is

		mr_c.cancel()  # cancelled MR: no reservation, kept as flagged history
		board = handover_board()
		self.assertEqual(flt(self._lot(board, wo.name)["reserved_qty"]), 80)
		cancelled = self._req(board, mr_c.name)
		self.assertEqual(cancelled["docstatus"], 2)
		self.assertIsNone(cancelled["lane"])
		self.assertEqual(cancelled["flag"], "cancelled")

		update_mr_status(mr_a.name, "Stopped")  # stopped without SE: dead request
		board = handover_board()
		self.assertEqual(flt(self._lot(board, wo.name)["reserved_qty"]), 50)
		stopped = self._req(board, mr_a.name)
		self.assertEqual(stopped["lane"], "request")
		self.assertEqual(stopped["flag"], "stopped")
		self.assertEqual(self._req(board, mr_b.name)["flag"], None)  # still open

	# ------------------------------------- 3. lane transitions (mutations)

	def test_t23_lane_transitions_by_direct_mutation(self):
		"""request -> terkirim, re-deriving the board after each direct
		mutation (submit MR; native SE). The retired postpacking confirm no
		longer advances any lane: a legacy confirmed MR without SE stays a
		plain request and `siap_kirim` never appears on the board."""
		# SE existence rides `material_request` on Stock Entry Detail (the link
		# the native MR->SE builder sets, T21); the parent-level SE filter is
		# resolved natively against the child table (frappe/database/query.py)
		self.assertTrue(
			frappe.get_meta("Stock Entry Detail").has_field("material_request")
		)

		wo = self._make_wo(self.bom, 100, self.fg, "1")
		self._transfer(wo)
		self._manufacture(wo, 100, "10:00:00")
		mr = self._make_mr(wo, 40)

		board = handover_board()
		req = self._req(board, mr.name)
		self.assertEqual(req["lane"], "request")
		self.assertIsNone(req["flag"])
		self.assertFalse(req["postpacking"]["confirmed"])
		self.assertIsNone(req["stock_entry"])
		self.assertEqual(flt(req["qty"]), 40)
		# T35: the doc_events sync claims the WO Link on submit; the row reads
		# the (still empty) WO summary — legacy MR kg only shows pre-cutover
		self.assertEqual(self._lot(board, wo.name)["custom_handover_material_request"], mr.name)
		self.assertIsNone(req["box_1"])
		self.assertIsNone(req["box_1_qty"])  # Pack never invented from MR data
		self.assertIsNone(req["box_2"])
		self.assertEqual(req["boxes"], [])
		self.assertEqual(req["from_warehouse"], self.cold_wh)
		self.assertEqual(req["to_warehouse"], self.target_wh)
		self.assertEqual(req["batch"], self._lot(board, wo.name)["batch"])
		self.assertEqual(flt(self._lot(board, wo.name)["available_qty"]), 60)

		# legacy post-packing + confirm (allow_on_submit fields, db_set as in
		# T22): the display block stays, but the lane remains request
		mr.db_set("custom_good_qty_postpacking", 40)
		mr.db_set("custom_jam_packing", "13:40:00")
		mr.db_set("custom_qc_packing", "Administrator")
		mr.db_set("custom_postpacking_confirmed", 1)

		board = handover_board()
		req = self._req(board, mr.name)
		self.assertEqual(req["lane"], "request")
		self.assertNotIn("siap_kirim", {r["lane"] for r in board["requests"] if r["lane"]})
		self.assertTrue(req["postpacking"]["confirmed"])
		self.assertEqual(flt(req["postpacking"]["good"]), 40)
		self.assertEqual(req["postpacking"]["jam_packing"], "13:40:00")
		self.assertEqual(req["postpacking"]["qc_packing"], "Administrator")

		# native send: submitted SE flips the lane, the lot unreserves and the
		# live ledger shows the movement (physical 100 -> 60)
		batch = self._req(board, mr.name)["batch"]
		se = self._send(mr, 40, batch)
		board = handover_board()
		req = self._req(board, mr.name)
		lot = self._lot(board, wo.name)
		self.assertEqual(req["lane"], "terkirim")
		self.assertEqual(req["stock_entry"], se.name)
		self.assertTrue(req["sent_at"])
		self.assertEqual(flt(lot["reserved_qty"]), 0)
		self.assertEqual(flt(lot["physical_qty"]), 60)
		self.assertEqual(flt(lot["available_qty"]), 60)

	# --------------------- T35. three-lane document precedence + summaries

	def test_t35_submitted_se_precedence_over_abnormal_cancel(self):
		"""A submitted Stock Entry keeps the request terkirim even when the MR
		is forced to the test-only abnormal docstatus=2 (cancelled AFTER send);
		both `_requests` and `_handover_lanes` derive the same truth."""
		from production_app.api import handover as handover_api

		wo = self._make_wo(self.bom, 100, self.fg, "9")
		self._transfer(wo)
		self._manufacture(wo, 100, "10:00:00")
		mr = self._make_mr(wo, 40)
		batch = self._lot(handover_board(), wo.name)["batch"]
		se = self._send(mr, 40, batch)

		mr.db_set("docstatus", 2)  # abnormal: cancelled after the send existed

		board = handover_board()
		req = self._req(board, mr.name)
		self.assertEqual(req["lane"], "terkirim")
		self.assertEqual(req["stock_entry"], se.name)
		self.assertEqual(handover_api._handover_lanes([wo.name]), {wo.name: "terkirim"})

	def test_t35_wo_summary_bulk_mapping_and_fallback(self):
		"""Request rows read box kg/Pack from the BULK Work Order lot rows only
		while the WO Link points at the MR; a pre-cutover MR (empty Link) falls
		back to its own kg with empty Pack — and the mapping performs no
		per-card Work Order lookup."""
		from production_app.api import handover as handover_api

		wo1 = self._make_wo(self.bom, 100, self.fg, "1")
		self._transfer(wo1)
		self._manufacture(wo1, 100, "10:00:00")
		wo2 = self._make_wo(self.bom, 100, self.fg, "2")
		self._transfer(wo2)
		self._manufacture(wo2, 100, "11:00:00")
		mr1 = self._make_mr(wo1, 100)
		mr2 = self._make_mr(wo2, 50)
		# pre-cutover simulation: the doc_events sync claims the Link on MR
		# submit, so clear it explicitly (db_set fires no hooks)
		wo2.db_set("custom_handover_material_request", None)

		wo1.db_set("custom_handover_material_request", mr1.name)
		wo1.db_set("custom_box_1", 12.5)
		wo1.db_set("custom_box_1_qty", 20)
		wo1.db_set("custom_box_2", 8.0)
		wo1.db_set("custom_box_2_qty", 19)
		row = self._req(handover_board(), mr1.name)
		self.assertEqual((row["box_1"], row["box_1_qty"]), (12.5, 20))
		self.assertEqual((row["box_2"], row["box_2_qty"]), (8.0, 19))
		# T39: every request row carries its item's count unit for the UI
		self.assertEqual(row["display_uom"], "Pack")
		# kg precedence proof: the WO value outranks the MR's legacy kg (12.5)
		wo1.db_set("custom_box_1", 21.5)
		self.assertEqual(self._req(handover_board(), mr1.name)["box_1"], 21.5)

		# pre-cutover fallback: MR kg fields, both Pack values stay empty
		mr2.db_set("custom_box_1", 6.5)
		mr2.db_set("custom_box_2", 2.25)
		row2 = self._req(handover_board(), mr2.name)
		self.assertEqual((row2["box_1"], row2["box_1_qty"]), (6.5, None))
		self.assertEqual((row2["box_2"], row2["box_2_qty"]), (2.25, None))

		# the mapping is bulk: no per-card Work Order lookup inside _requests
		real_get_value = frappe.db.get_value

		def no_wo_lookups(*args, **kwargs):
			if args and args[0] == "Work Order":
				raise AssertionError("request-row mapping must not look up Work Orders per card")
			return real_get_value(*args, **kwargs)

		wo_rows = handover_api._wo_lot_rows([wo1.name, wo2.name])
		with patch.object(frappe.db, "get_value", side_effect=no_wo_lookups):
			rows = handover_api._requests(wo_rows, wo_names=[wo1.name, wo2.name])
		by_mr = {r["mr"]: r for r in rows}
		self.assertEqual((by_mr[mr1.name]["box_1"], by_mr[mr1.name]["box_1_qty"]), (21.5, 20))
		self.assertEqual((by_mr[mr2.name]["box_1"], by_mr[mr2.name]["box_1_qty"]), (6.5, None))
		self.assertIsNone(by_mr[mr2.name]["box_2_qty"])

	def test_t36_board_single_bulk_seams_no_fan_out(self):
		"""T36: ONE full board build for MULTIPLE Work Orders hits each bulk
		seam exactly once — a single (get_available_batches +
		get_stock_ledgers_batches) pair covers every batch balance, the single
		bulk Work Order result set already carries the summary Link/kg/Pack
		fields onto the request rows, and nothing fans out per card:
		_checked_lot is never used while building the board, and neither
		frappe.db.get_value nor frappe.get_doc touches a Work Order."""
		suffix = random_string(4).upper()
		fg_a = _make_item(f"{PREFIX}-FGA-{suffix}", self.group, self.uom)
		fg_b = _make_item(f"{PREFIX}-FGB-{suffix}", self.group, self.uom)
		bom_a = self._make_bom(fg_a)
		bom_b = self._make_bom(fg_b)
		wo_a = self._make_wo(bom_a, 50, fg_a, "1")
		wo_b = self._make_wo(bom_b, 60, fg_b, "2")
		self._transfer(wo_a)
		self._transfer(wo_b)
		self._manufacture(wo_a, 50, "10:30:00")
		self._manufacture(wo_b, 60, "10:40:00")
		mr_a = self._make_mr(wo_a, 50)
		mr_b = self._make_mr(wo_b, 60)
		batch_a = self._lot_in_cold(wo_a)
		batch_b = self._lot_in_cold(wo_b)
		# pre-cutover B keeps only its legacy MR kg (Link cleared, no hooks)
		wo_b.db_set("custom_handover_material_request", None)
		# A's summary: distinct kg proves the BULK WO row wins over the MR's 12.5
		wo_a.db_set("custom_handover_material_request", mr_a.name)
		wo_a.db_set("custom_box_1", 21.5)
		wo_a.db_set("custom_box_1_qty", 9)
		wo_a.db_set("custom_box_2", 7.0)
		wo_a.db_set("custom_box_2_qty", 2)

		from production_app.api import handover

		available = handover.get_available_batches
		legacy = handover.get_stock_ledgers_batches
		real_get_value = frappe.db.get_value
		real_get_doc = frappe.get_doc

		def no_wo_get_value(*args, **kwargs):
			if args and args[0] == "Work Order":
				raise AssertionError("board build must not look Work Orders up per card")
			return real_get_value(*args, **kwargs)

		def no_wo_get_doc(doctype=None, *args, **kwargs):
			if doctype == "Work Order":
				raise AssertionError("board build must not get_doc a Work Order")
			return real_get_doc(doctype, *args, **kwargs)

		with (
			patch.object(handover, "get_available_batches", wraps=available) as available_call,
			patch.object(handover, "get_stock_ledgers_batches", wraps=legacy) as legacy_call,
			patch.object(
				handover, "_checked_lot", side_effect=AssertionError("board must not use _checked_lot")
			),
			patch.object(frappe.db, "get_value", side_effect=no_wo_get_value),
			patch.object(frappe, "get_doc", side_effect=no_wo_get_doc),
		):
			board = handover_board()

		# the targeted mutation seam also stays scoped: no full board inside
		with patch.object(
			handover, "_build_board", side_effect=AssertionError("_checked_lot must not build the board")
		):
			lot = handover._checked_lot(wo_a.name)
		self.assertEqual(flt(lot.reserved_qty), 50)  # scoped re-derivation sees the MR

		# one bulk query pair for the WHOLE board (site-wide lots included)
		self.assertEqual(available_call.call_count, 1)
		self.assertEqual(legacy_call.call_count, 1)
		kwargs = available_call.call_args.args[0]
		self.assertLessEqual({batch_a, batch_b}, set(kwargs.batch_no))

		row_a = self._req(board, mr_a.name)
		row_b = self._req(board, mr_b.name)
		# Link/kg/Pack read from the single bulk Work Order result set
		self.assertEqual((row_a["box_1"], row_a["box_1_qty"]), (21.5, 9))
		self.assertEqual((row_a["box_2"], row_a["box_2_qty"]), (7.0, 2))
		self.assertEqual(self._lot(board, wo_a.name)["custom_handover_material_request"], mr_a.name)
		# pre-cutover fallback: legacy MR kg, Pack never invented
		self.assertEqual((row_b["box_1"], row_b["box_1_qty"]), (12.5, None))

	# ------------------------- 4. legacy unsupported + empty-lot drop rule

	def test_t23_legacy_unsupported_and_empty_lot_drop(self):
		"""A WO with two Manufacture SEs/batches -> one explicit unsupported
	card (never an error, never silent flattening); a lot drained to zero with
	no lane-bound request drops off the Cold Storage lane."""
		legacy = self._make_wo(self.bom, 100, self.fg, "1")
		normal = self._make_wo(self.bom, 100, self.fg, "2")
		for wo in (legacy, normal):
			self._transfer(wo)
		# legacy partial history: two Manufactures -> two FG batches
		self._manufacture(legacy, 40, "10:00:00", restore_rm=False)
		self._manufacture(legacy, 30, "11:00:00", restore_rm=False)
		self._manufacture(normal, 100, "12:00:00")

		board = handover_board()
		bad = self._lot(board, legacy.name)
		self.assertTrue(bad["unsupported"])
		self.assertIn(legacy.name, bad["unsupported_reason"])
		self.assertIsNone(bad["batch"])
		good = self._lot(board, normal.name)
		self.assertFalse(good["unsupported"])

		# drain the normal lot with a plain (non-MR) transfer, then it drops
		se = frappe.get_doc(
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
					"batch_no": good["batch"],
				}
			],
		}
		)
		self._pin_posting(se, "15:00:00")
		se.insert()
		se.submit()

		board = handover_board()
		self.assertFalse(any(l["work_order"] == normal.name for l in board["lots"]))
		self.assertTrue(any(l["work_order"] == legacy.name for l in board["lots"]))

	# ------------------------------------------------ 5. older-lot FIFO hint

	def test_t23_older_lot_same_item_hint(self):
		"""Two lots of the SAME item: the newer one carries the informational
		FIFO hint; the older one does not."""
		older = self._make_wo(self.bom_hint, 50, self.fg_hint, "1")
		newer = self._make_wo(self.bom_hint, 60, self.fg_hint, "2")
		self._transfer(older)
		self._transfer(newer)
		self._manufacture(older, 50, "10:00:00")
		self._manufacture(newer, 60, "11:00:00")

		board = handover_board()
		self.assertFalse(self._lot(board, older.name)["has_older_lot_same_item"])
		self.assertTrue(self._lot(board, newer.name)["has_older_lot_same_item"])

	# ------------------------------------------------- 6. role filtering

	def test_t23_role_filtering_per_session_user(self):
		"""Gudang and Produksi render lots+requests; a bare user gets an empty
		board (no exception, no data)."""
		wo = self._make_wo(self.bom, 100, self.fg, "1")
		self._transfer(wo)
		self._manufacture(wo, 100, "10:00:00")
		mr = self._make_mr(wo, 20)

		suffix = random_string(6).upper()
		# FU64: persona gudang pasca-migrasi = legacy + Stock User (akses papan
		# lewat hak native Stock User).
		gudang = self._make_user(
			f"t23.gudang.{suffix}@prodapp.example.com", ["Gudang Barang Jadi", "Stock User"]
		)
		prod = self._make_user(
			f"t23.prod.{suffix}@prodapp.example.com", ["Manufacturing User"]
		)
		bare = self._make_user(f"t23.bare.{suffix}@prodapp.example.com", [])

		for username, is_gudang_flag, is_produksi_flag in (
			(gudang, True, False),
			(prod, False, True),
		):
			frappe.set_user(username)
			try:
				board = handover_board()
				self.assertEqual(board["roles"]["is_gudang"], is_gudang_flag)
				self.assertEqual(board["roles"]["is_produksi"], is_produksi_flag)
				self.assertTrue(any(l["work_order"] == wo.name for l in board["lots"]))
				self.assertTrue(any(r["mr"] == mr.name for r in board["requests"]))
			finally:
				frappe.set_user("Administrator")

		frappe.set_user(bare)
		try:
			board = handover_board()  # must not raise
			self.assertEqual(board["lots"], [])
			self.assertEqual(board["requests"], [])
			self.assertFalse(board["roles"]["is_gudang"])
			self.assertFalse(board["roles"]["is_produksi"])
		finally:
			frappe.set_user("Administrator")

	# ------------------------------------------- 7. target warehouse setting

	def test_t23_target_warehouse_from_setting(self):
		"""The send target comes from Manufacturing Settings
		custom_default_handover_warehouse (written via the T22 API by a
		Manufacturing Manager); unset -> null."""
		suffix = random_string(6).upper()
		manager = self._make_user(
			f"t23.manager.{suffix}@prodapp.example.com", ["Manufacturing Manager"]
		)
		# FU20: pin all six defaults — the manager's partial saves must never
		# wipe the operator's other live warehouse settings.
		with self._pin_settings():
			frappe.set_user(manager)
			warehouse_defaults_save(handover_warehouse=self.target_wh)
			frappe.set_user("Administrator")
			self.assertEqual(handover_board()["target_warehouse"], self.target_wh)

			frappe.set_user(manager)
			warehouse_defaults_save(handover_warehouse=None)
			frappe.set_user("Administrator")
			self.assertIsNone(handover_board()["target_warehouse"])

	# ------------------------------------------- 8. deleted Item master (FU65)

	def test_fu65_dangling_item_keeps_the_board_alive(self):
		"""FU65: a cancelled request keeps its item_code for audit; when the
		Item master is deleted afterwards, the board must degrade that ONE row
		instead of failing the whole API — a board failure starved the SPA role
		flags and hid the Form Order/Pengaturan menus for every user."""
		suffix = random_string(6).upper()
		fg = _make_item(f"{PREFIX}-GONE-{suffix}", self.group, self.uom, batch=False)
		bom = self._make_bom(fg)
		wo = self._make_wo(bom, 1, fg, "1")
		mr = self._make_mr(wo, 3)
		mr.cancel()
		# simulate the admin deletion (cancelled docs never block it) without a
		# cascade teardown: raw update mirrors the exact dirty-data shape
		row_name = frappe.get_value("Material Request Item", {"parent": mr.name}, "name")
		frappe.db.set_value(
			"Material Request Item", row_name, "item_code", f"{PREFIX}-DELETED-{suffix}"
		)
		board = handover_board()  # must not raise
		self.assertIn("roles", board)
		self.assertIn("is_produksi", board["roles"])
		self.assertIn("can_settings", board["roles"])
		req = self._req(board, mr.name)
		self.assertEqual(req["flag"], "cancelled")
		# satuan tampil masih hidup dari snapshot baris MR; konversi display
		# didegradasi (tanpa master) dan qtyInPack jatuh ke fallback 1
		self.assertEqual(req["stock_uom"], self.uom)
		self.assertIsNone(req["display_uom"])
		self.assertEqual(req["qty_in_pack"], 1.0)

	def test_fu65_enrich_units_tolerates_deleted_item(self):
		"""Unit contract for the other callers (WO list, wo_detail, label)."""
		row = frappe._dict(
			production_item=f"{PREFIX}-MISSING-{random_string(4).upper()}", custom_uom="Gram"
		)
		_enrich_units([row])
		self.assertIsNone(row.stock_uom)
		self.assertEqual(row.display_uom, "Gram")
		self.assertIsNone(row.display_conversion_factor)
		self.assertFalse(row.stock_uom_whole_number)
		self.assertIsNone(row.uom_warning)
