"""Printable SKU labels for a confirmed Work Order Pre-Packing result."""

import math
import re
from base64 import b64encode
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate
from pyqrcode import create as create_qr

from production_app.api.work_order import _enrich_units

MAX_LABELS = 10000


def get_context(context):
    name = frappe.form_dict.get("name")
    if not name:
        frappe.throw(_("Work Order wajib dipilih untuk mencetak label."))

    wo = frappe.get_doc("Work Order", name)
    wo.check_permission("read")
    if not wo.get("custom_prepacking_confirmed"):
        frappe.throw(_("Simpan Pre-Packing sebelum mencetak label Work Order {0}.").format(name))

    good_qty = flt(wo.get("custom_good_qty_prepacking"))
    if not math.isfinite(good_qty) or good_qty <= 0:
        frappe.throw(_("Good Qty Pre-Packing harus lebih besar dari nol untuk mencetak label."))

    units = frappe._dict(production_item=wo.production_item, custom_uom=wo.get("custom_uom"))
    _enrich_units([units])
    factor = units.display_conversion_factor
    if factor is None:
        frappe.throw(units.uom_warning or _("Konversi satuan Item belum valid untuk mencetak label."))
    qty_per_label = good_qty / factor
    if not math.isfinite(qty_per_label):
        frappe.throw(_("Good Qty Pre-Packing tidak valid untuk mencetak label."))
    extra_labels = str(frappe.form_dict.get("extra") or "0")
    if len(extra_labels) > 5 or not re.fullmatch(r"[0-9]+", extra_labels):
        frappe.throw(_("Jumlah label tambahan harus berupa bilangan bulat 0 atau lebih, maksimal {0}.").format(MAX_LABELS))
    extra_labels = int(extra_labels)
    label_count = max(1, math.ceil(qty_per_label - 1e-8)) + extra_labels
    if label_count > MAX_LABELS:
        frappe.throw(_("Jumlah label melebihi batas {0} dalam satu cetakan.").format(MAX_LABELS))

    item = frappe.db.get_value(
        "Item", wo.production_item,
        ["item_name", "shelf_life_in_days"], as_dict=True,
    )
    batch = frappe.get_all(
        "Batch",
        filters={
            "reference_doctype": "Work Order",
            "reference_name": wo.name,
            "item": wo.production_item,
        },
        fields=["manufacturing_date", "expiry_date"],
        order_by="creation asc",
        limit=1,
    )
    batch = batch[0] if batch else {}
    manufacturing_date = batch.get("manufacturing_date") or getdate(
        wo.actual_start_date or wo.planned_start_date or wo.creation
    )
    expiry_date = batch.get("expiry_date")
    if not expiry_date and item and item.shelf_life_in_days:
        expiry_date = add_days(manufacturing_date, int(item.shelf_life_in_days))

    stream = BytesIO()
    create_qr(wo.production_item).svg(stream, scale=4, quiet_zone=0)

    context.no_cache = 1
    context.work_order = wo.name
    context.sku = wo.production_item
    item_name = item.item_name if item else wo.production_item
    dough_match = re.search(r"\bdough\b", item_name, flags=re.IGNORECASE)
    dough_tail = item_name[dough_match.end():].strip() if dough_match else ""
    context.item_name = item_name
    context.item_name_prefix = item_name[:dough_match.end()].strip() if dough_tail else ""
    context.item_name_main = dough_tail if dough_tail else item_name
    context.manufacturing_date = manufacturing_date
    context.expiry_date = expiry_date
    context.qr_svg_base64 = b64encode(stream.getvalue()).decode("ascii")
    context.label_count = label_count
    context.labels = range(label_count)
    return context
