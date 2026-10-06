"""Issue and read Work Order labels backed by product_qr serial records."""

import math
import re

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate

from production_app.api.work_order import _enrich_units

MAX_LABELS = 10000
MAPPING_DOCTYPE = "Work Order QR Label"
SERIAL_DOCTYPE = "Product QR Serial"


def _label_count(wo, extra):
	good_qty = flt(wo.get("custom_good_qty_prepacking"))
	if not wo.get("custom_prepacking_confirmed") or not math.isfinite(good_qty) or good_qty <= 0:
		frappe.throw(_("Simpan Pre-Packing dengan Good Qty lebih dari nol sebelum mencetak label."))

	units = frappe._dict(production_item=wo.production_item, custom_uom=wo.get("custom_uom"))
	_enrich_units([units])
	factor = units.display_conversion_factor
	if factor is None or not math.isfinite(flt(factor)) or flt(factor) <= 0:
		frappe.throw(units.uom_warning or _("Konversi satuan Item belum valid untuk mencetak label."))

	extra = str(extra or "0")
	if len(extra) > 5 or not re.fullmatch(r"[0-9]+", extra):
		frappe.throw(_("Jumlah label tambahan harus berupa bilangan bulat 0 atau lebih."))
	count = max(1, math.ceil(good_qty / flt(factor) - 1e-8)) + int(extra)
	if count > MAX_LABELS:
		frappe.throw(_("Jumlah label melebihi batas {0} dalam satu cetakan.").format(MAX_LABELS))
	return count


def _single_batch(wo):
	batches = frappe.get_all(
		"Batch",
		filters={
			"reference_doctype": "Work Order",
			"reference_name": wo.name,
			"item": wo.production_item,
		},
		fields=["name", "manufacturing_date", "expiry_date"],
		order_by="creation asc",
		limit=2,
	)
	if len(batches) > 1:
		frappe.throw(_("Work Order {0} memiliki lebih dari satu Batch; label QR perlu satu Batch yang pasti.").format(wo.name))
	return batches[0] if batches else None


def label_data(name, extra=0, *, issue=False):
	"""Build one QR per label; issue missing serials only in an authorized POST."""
	if not frappe.db.table_exists(SERIAL_DOCTYPE):
		frappe.throw(_("App product_qr harus dipasang dan dimigrasi sebelum mencetak label."))
	if not frappe.db.table_exists(MAPPING_DOCTYPE):
		frappe.throw(_("DocType Work Order QR Label belum dimigrasi."))

	wo = frappe.get_doc("Work Order", name)
	wo.check_permission("write" if issue else "read")
	if issue:
		# Serialize concurrent print requests so they reuse the same first N labels.
		frappe.db.get_value("Work Order", name, "name", for_update=True)
		wo.reload()
	count = _label_count(wo, extra)
	rows = frappe.get_all(
		MAPPING_DOCTYPE,
		filters={"work_order": wo.name},
		fields=["product_qr_serial"],
		order_by="creation asc, name asc",
		limit_page_length=0,
	)
	serial_names = [row.product_qr_serial for row in rows]
	if serial_names:
		# An issued label keeps its original Batch identity on every reprint.
		first = frappe.db.get_value(SERIAL_DOCTYPE, serial_names[0], ["item_code", "batch_no"], as_dict=True)
		if not first or first.item_code != wo.production_item:
			frappe.throw(_("Serial label Work Order tidak cocok dengan Item saat ini."))
		batch = (
			frappe.db.get_value("Batch", first.batch_no, ["name", "manufacturing_date", "expiry_date"], as_dict=True)
			if first.batch_no else None
		)
		if first.batch_no and not batch:
			frappe.throw(_("Batch pada serial label Work Order tidak ditemukan."))
	else:
		batch = _single_batch(wo)
	batch_no = batch.name if batch else None
	if issue:
		for _ in range(max(0, count - len(serial_names))):
			serial = frappe.get_doc({
				"doctype": SERIAL_DOCTYPE,
				"item_code": wo.production_item,
				"batch_no": batch_no,
			}).insert(ignore_permissions=True)
			frappe.get_doc({
				"doctype": MAPPING_DOCTYPE,
				"work_order": wo.name,
				"product_qr_serial": serial.name,
			}).insert(ignore_permissions=True)
			serial_names.append(serial.name)
	elif len(serial_names) < count:
		frappe.throw(_("Label QR belum dibuat. Cetak terlebih dahulu dari Production Workspace."))

	labels = []
	for serial_name in serial_names[:count]:
		serial = frappe.db.get_value(
			SERIAL_DOCTYPE, serial_name, ["item_code", "batch_no", "serial_no", "qr_payload"], as_dict=True
		)
		if not serial or (serial.item_code, serial.batch_no or None) != (wo.production_item, batch_no):
			frappe.throw(_("Serial label Work Order tidak cocok dengan Item atau Batch saat ini."))
		labels.append({"serial_no": serial.serial_no, "qr_value": serial.qr_payload})

	item = frappe.db.get_value(
		"Item", wo.production_item, ["item_name", "shelf_life_in_days"], as_dict=True
	)
	item_name = item.item_name if item else wo.production_item
	dough_match = re.search(r"\bdough\b", item_name, flags=re.IGNORECASE)
	dough_tail = item_name[dough_match.end():].strip() if dough_match else ""
	manufacturing_date = (batch.manufacturing_date if batch else None) or getdate(
		wo.actual_start_date or wo.planned_start_date or wo.creation
	)
	expiry_date = batch.expiry_date if batch else None
	if not expiry_date and item and item.shelf_life_in_days:
		expiry_date = add_days(manufacturing_date, int(item.shelf_life_in_days))

	return {
		"work_order": wo.name,
		"sku": wo.production_item,
		"batch_no": batch_no,
		"item_name": item_name,
		"item_name_prefix": item_name[:dough_match.end()].strip() if dough_tail else "",
		"item_name_main": dough_tail if dough_tail else item_name,
		"manufacturing_date": manufacturing_date,
		"expiry_date": expiry_date,
		"label_count": count,
		"labels": labels,
	}
