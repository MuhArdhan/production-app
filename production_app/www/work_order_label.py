"""Read-only printable view of previously issued Work Order QR labels."""

from base64 import b64encode
from io import BytesIO

import frappe
from frappe import _
from pyqrcode import create as create_qr

from production_app.api.work_order_labels import label_data


def get_context(context):
    name = frappe.form_dict.get("name")
    if not name:
        frappe.throw(_("Work Order wajib dipilih untuk mencetak label."))

    data = label_data(name, frappe.form_dict.get("extra") or 0)
    labels = []
    for row in data["labels"]:
        stream = BytesIO()
        create_qr(row["qr_value"]).svg(stream, scale=4, quiet_zone=0)
        labels.append({
            "serial_no": row["serial_no"],
            "qr_value": row["qr_value"],
            "qr_svg_base64": b64encode(stream.getvalue()).decode("ascii"),
        })

    context.no_cache = 1
    for key, value in data.items():
        context[key] = value
    context.labels = labels
    return context
