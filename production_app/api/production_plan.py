# FU62 — API untuk wizard "Tambah Item" di form Production Plan.
#
# Dipakai public/js/production_plan.js (hooks.doctype_js). Perancangan:
# - Satu round-trip per item terpilih (item_plan_info) menggantikan 3 async
#   berantai di Client Script lama — lebih sedikit permintaan, lebih kecil
#   peluang race.
# - get_uom_conversion_factor menggantikan Server Script lama prod dengan
#   bentuk balasan sama persis (drop-in); dipanggil via dotted path sehingga
#   keduanya boleh hidup berdampingan sampai Server Script dipensiunkan.
# - Semua bacaan custom field lewat get_cached_doc + .get() (SELECT *), bukan
#   db.get_value dengan daftar kolom — kolom custom yang belum termigrate
#   tidak pernah tersentuh (pelajaran insiden InvalidColumnName FU58).

import math

import frappe
from frappe import _
from frappe.utils import flt


def _default_uom(item):
	"""Rantai UOM default input — sama dengan _enrich_units (work_order):
	field milik warehouse_app (W21), lalu stock UOM. .get() aman di site
	tanpa field warehouse_app. Field warisan production_app
	(custom_default_uom_warehouse) dipensiunkan dari rantai ini; nilainya
	yang dimigrasikan warehouse_app ke field baru."""
	return (
		item.get("custom_default_inventory_unit_of_measure")
		or item.stock_uom
	)


def _conversion_factor(item, uom):
	"""Faktor konversi item-specific (UOM Conversion Detail + varian).
	Identitas stock UOM selalu valid (child table jarang punya baris untuk
	stock UOM sendiri). None bila tidak ada baris valid."""
	if uom == item.stock_uom:
		return 1.0
	rows = list(item.uoms)
	if item.variant_of:
		rows += frappe.get_cached_doc("Item", item.variant_of).uoms
	for row in rows:
		if row.uom == uom:
			value = flt(row.conversion_factor)
			if math.isfinite(value) and value > 0:
				return value
	return None


@frappe.whitelist()
def item_plan_info(item_code):
	"""Langkah 1 wizard: nama item, UOM default (rantai W21), faktornya, dan
	BOM default aktif — semuanya satu round-trip."""
	if not frappe.has_permission("Item", "read", doc=item_code):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	item = frappe.get_cached_doc("Item", item_code)
	default_uom = _default_uom(item)
	factor = _conversion_factor(item, default_uom)

	out = {
		"item_name": item.item_name,
		"stock_uom": item.stock_uom,
		"default_uom": default_uom,
		"conversion_factor": factor if factor is not None else 1.0,
		"found": factor is not None,
		"bom": None,
	}

	bom_name = frappe.db.get_value(
		"BOM", {"item": item_code, "is_default": 1, "is_active": 1}, "name"
	)
	if bom_name:
		bom = frappe.get_cached_doc("BOM", bom_name)
		out["bom"] = {
			"name": bom.name,
			"bom_name": bom.get("custom_bom_name"),
			"quantity": flt(bom.quantity),
		}
	return out


@frappe.whitelist()
def get_uom_conversion_factor(item_code, uom):
	"""Faktor konversi untuk UOM mana pun; bentuk balasan sama dengan Server
	Script lama supaya jadi pengganti langsung di site mana pun."""
	if not frappe.has_permission("Item", "read", doc=item_code):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	item = frappe.get_cached_doc("Item", item_code)
	factor = _conversion_factor(item, uom)
	return {
		"conversion_factor": factor if factor is not None else 1.0,
		"found": factor is not None,
		"stock_uom": item.stock_uom,
	}


@frappe.whitelist()
def bom_info(bom_no):
	"""Nama & quantity BOM untuk ganti BOM manual di Langkah 1 (db.get_value
	klien lama diganti supaya kolom custom pre-migrate tidak pernah dibaca)."""
	if not frappe.has_permission("BOM", "read", doc=bom_no):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	bom = frappe.get_cached_doc("BOM", bom_no)
	return {
		"name": bom.name,
		"bom_name": bom.get("custom_bom_name"),
		"quantity": flt(bom.quantity),
	}
