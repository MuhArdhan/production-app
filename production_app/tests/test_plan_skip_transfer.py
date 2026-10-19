"""Per-plan-row choice: native WO creation, persistence and non-interference."""

from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from production_app.api.production_plan import apply_plan_skip_transfer_on_create, item_plan_info
from production_app.tests.test_outlet_single_level_bom import (
	_company_warehouse, _first_existing_wo_config, _make_bom, _make_item, _root_group,
)
from production_app.upgrade import ensure_plan_skip_transfer_field


class TestPlanSkipTransferGuard(TestCase):
	def test_item_info_only_allows_active_krim_kopi(self):
		for name, disabled, allowed in (
			("Krim Kopi", 0, True), ("Krim Kopi 700gr", 0, True),
			("Dough", 0, False), ("Krim Kopi (Deactivate)", 0, False),
			("Krim Kopi", 1, False),
		):
			with self.subTest(name=name, disabled=disabled):
				item = frappe._dict(item_name=name, stock_uom="Kg", disabled=disabled)
				with patch("frappe.has_permission", return_value=True), patch(
					"frappe.get_cached_doc", return_value=item
				), patch("frappe.db.get_value", return_value=None):
					self.assertEqual(item_plan_info("ITEM")["skip_transfer_allowed"], allowed)

	def _doc(self, **values):
		doc = frappe.get_doc({
			"doctype": "Work Order", "production_plan": "PLAN-A",
			"production_plan_item": "ROW-A", "production_item": "ITEM-A",
			"skip_transfer": 0, **values,
		})
		doc.set("__islocal", 0 if values.get("name") else 1)
		return doc

	def test_checked_and_unchecked_use_exact_row(self):
		for checked in (0, 1):
			with self.subTest(checked=checked):
				doc = self._doc(skip_transfer=1 - checked)
				with patch("frappe.db.get_value", return_value=frappe._dict(custom_skip_transfer=checked)) as query:
					apply_plan_skip_transfer_on_create(doc)
					self.assertEqual(doc.skip_transfer, checked)
					filters = query.call_args.args[1]
					self.assertEqual(filters, {
						"name": "ROW-A", "parent": "PLAN-A", "parenttype": "Production Plan",
						"parentfield": "po_items", "item_code": "ITEM-A",
					})

	def test_existing_manual_and_subassembly_are_not_changed(self):
		for values in (
			{"name": "EXISTING"}, {"production_plan": None},
			{"production_plan_item": None}, {"production_plan_sub_assembly_item": "SUB-A"},
		):
			doc = self._doc(**values)
			with patch("frappe.db.get_value") as query:
				apply_plan_skip_transfer_on_create(doc)
				query.assert_not_called()
				self.assertEqual(doc.skip_transfer, 0)

	def test_mismatched_source_does_not_change_native_value(self):
		doc = self._doc(skip_transfer=1)
		with patch("frappe.db.get_value", return_value=None):
			apply_plan_skip_transfer_on_create(doc)
		self.assertEqual(doc.skip_transfer, 1)


class TestPlanSkipTransferNative(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# DDL belongs to installation, outside the isolated record transaction.
		ensure_plan_skip_transfer_field()
		frappe.db.commit()

	def test_saved_plan_rows_create_native_work_orders(self):
		self.assertEqual(ensure_plan_skip_transfer_field(), "unchanged")
		self.assertEqual(ensure_plan_skip_transfer_field(), "unchanged")
		cfg = _first_existing_wo_config()
		warehouse = _company_warehouse(cfg.company, cfg.source_warehouse or cfg.fg_warehouse)
		suffix = frappe.generate_hash(length=8)
		group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or _root_group()
		raw = _make_item(f"PST-RAW-{suffix}", group, cfg.company, warehouse)
		fg = _make_item(f"PST-FG-{suffix}", group, cfg.company, warehouse)
		bom = _make_bom(fg, cfg.company, [{"item_code": raw, "qty": 2, "rate": 10}])
		plan = frappe.get_doc({
			"doctype": "Production Plan", "company": cfg.company,
			"get_items_from": "Material Request", "reserve_stock": 0,
			"po_items": [{
				"item_code": fg, "bom_no": bom, "planned_qty": 3,
				"stock_uom": "Nos", "warehouse": warehouse,
				"custom_skip_transfer": checked,
			} for checked in (1, 0, 1)],
		})
		plan.insert()
		plan.reload()
		self.assertEqual([row.custom_skip_transfer for row in plan.po_items], [1, 0, 1])
		items = plan.get_production_items()
		for row in plan.po_items:
			item = items[row.name]
			self.assertEqual(item["production_plan_item"], row.name)
			item.update({
				"source_warehouse": warehouse,
				"fg_warehouse": warehouse, "wip_warehouse": warehouse,
				"planned_start_date": frappe.utils.now(),
			})
			name = plan.create_work_order(item)
			wo = frappe.get_doc("Work Order", name)
			self.assertEqual(wo.skip_transfer, row.custom_skip_transfer)
			self.assertFalse(wo.get("custom_prepacking_confirmed"))
			# Once created, saving a WO must not reapply a changed plan choice.
			wo.skip_transfer = 1 - row.custom_skip_transfer
			wo.save()
			wo.reload()
			self.assertEqual(wo.skip_transfer, 1 - row.custom_skip_transfer)
