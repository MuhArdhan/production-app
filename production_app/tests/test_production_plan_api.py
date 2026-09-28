# FU62 Production Plan wizard API suite (TASKS.md FU62, 2026-09-28)
#
# Proves by execution on the installed runtime:
# - get_uom_conversion_factor: identitas stock UOM selalu valid (factor 1,
#   found) walau UOM Conversion Detail tidak punya baris untuk stock UOM
#   sendiri — kasus yang paling sering salah; konversi item-specific ditemukan
#   dengan faktor benar; UOM tanpa baris → found False + faktor fallback 1
#   (bentuk balasan = drop-in pengganti Server Script lama);
# - item_plan_info: rantai UOM default (custom_default_uom_warehouse → stock
#   UOM) beserta faktornya, dan BOM default aktif (name, bom_name, quantity);
# - bom_info: nama tampilan + quantity untuk ganti BOM manual.
#
# Test records carry the PPA prefix; rolled back by the framework.

import frappe
from frappe.tests import IntegrationTestCase

from production_app.api import production_plan as pp_api

PREFIX = "PPA"
PACK = "Pack (PPA)"


def _ensure_uom():
	if not frappe.db.exists("UOM", PACK):
		frappe.get_doc({"doctype": "UOM", "uom_name": PACK, "enabled": 1}).insert()
	return PACK


def _make_item(suffix, with_default_uom=True):
	uom = _ensure_uom()
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": f"{PREFIX}-{suffix}",
			"item_name": f"PPA test item {suffix}",
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"custom_default_uom_warehouse": uom if with_default_uom else None,
			"uoms": [{"uom": uom, "conversion_factor": 12}],
		}
	).insert()
	return doc


def _make_raw_item():
	if not frappe.db.exists("Item", f"{PREFIX}-RAW"):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"{PREFIX}-RAW",
				"item_name": "PPA raw material",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert()
	return f"{PREFIX}-RAW"


def _make_bom(item):
	company = frappe.db.get_value("Company", {}, "name")
	return frappe.get_doc(
		{
			"doctype": "BOM",
			"item": item.item_code,
			"quantity": 24,
			"company": company,
			"is_active": 1,
			"is_default": 1,
			"with_operations": 0,
			# validasi native: "Raw Materials cannot be blank" (bom.py validate_materials)
			"items": [{"item_code": _make_raw_item(), "qty": 1}],
		}
	).insert()


class TestUomConversion(IntegrationTestCase):
	def test_stock_uom_identity_is_always_found(self):
		item = _make_item("ident")
		out = pp_api.get_uom_conversion_factor(item.item_code, item.stock_uom)
		self.assertEqual(out["conversion_factor"], 1.0)
		self.assertTrue(out["found"])
		self.assertEqual(out["stock_uom"], "Nos")

	def test_item_specific_conversion_found(self):
		item = _make_item("found")
		out = pp_api.get_uom_conversion_factor(item.item_code, PACK)
		self.assertEqual(out["conversion_factor"], 12.0)
		self.assertTrue(out["found"])

	def test_missing_conversion_defaults_to_one_with_found_false(self):
		item = _make_item("miss")
		out = pp_api.get_uom_conversion_factor(item.item_code, "Kg")
		self.assertEqual(out["conversion_factor"], 1.0)
		self.assertFalse(out["found"])


class TestItemPlanInfo(IntegrationTestCase):
	def test_default_uom_chain_and_default_bom(self):
		item = _make_item("chain")
		bom = _make_bom(item)
		out = pp_api.item_plan_info(item.item_code)
		self.assertEqual(out["default_uom"], PACK)
		self.assertEqual(out["conversion_factor"], 12.0)
		self.assertTrue(out["found"])
		self.assertEqual(out["bom"]["name"], bom.name)
		self.assertEqual(out["bom"]["quantity"], 24.0)

	def test_fallback_to_stock_uom_is_found(self):
		item = _make_item("fallback", with_default_uom=False)
		_make_bom(item)
		out = pp_api.item_plan_info(item.item_code)
		self.assertEqual(out["default_uom"], item.stock_uom)
		self.assertEqual(out["conversion_factor"], 1.0)
		self.assertTrue(out["found"])

	def test_bom_info(self):
		item = _make_item("bominfo")
		bom = _make_bom(item)
		out = pp_api.bom_info(bom.name)
		self.assertEqual(out["name"], bom.name)
		self.assertEqual(out["quantity"], 24.0)
