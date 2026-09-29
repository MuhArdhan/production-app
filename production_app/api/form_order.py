# Form Order (FO 2026-09-18, TASKS.md section I) — produksi meminta barang
# dari gudang lewat Material Request native (Material Transfer). Free-form:
# TIDAK pernah menulis custom_work_order (binding key papan tiga lajur
# api/handover.py) — penanda custom_is_form_order (upgrade.py) yang
# memisahkan MR Form Order dari MR Desk/serah terima. Status selalu
# diturunkan dari dokumen (SE tersedia = terkirim); tidak ada field status
# baru. Item ber-batch ditolak di v1 (pilih batch = jalur Desk) — 8/403 item
# stock live yang berbatch semuanya produk jadi/peralatan (FO-1).

import math

import frappe
from frappe import _
from frappe.defaults import get_user_default, set_user_default
from frappe.utils import add_days, cint, flt, get_link_to_form, getdate, now

from erpnext.stock.doctype.material_request.material_request import (
	make_stock_entry as make_mr_stock_entry,
)

from production_app.api.handover import (
	ROLE_MANAJER_PRODUKSI,
	ROLE_PRODUKSI,
	ROLES_GUDANG,
	_require_role,
	_retry_on_deadlock,
	_sent_se_by_mr,
	_time_str,
	_user_names,
)
from production_app.api.work_order import (
	FORM_ORDER_SOURCE_FIELD,
	FORM_ORDER_TARGET_FIELD,
	_ensure_settings_fields,
)

DOCTYPE = "Material Request"
ROLES_PEMBUAT = (ROLE_PRODUKSI, ROLE_MANAJER_PRODUKSI)

STATUS_DRAF = "draf"
STATUS_MENUNGGU = "menunggu"
STATUS_TERKIRIM = "terkirim"
STATUS_BATAL = "batal"

# FU48c: satuan terakhir dipakai user per item — user default JSON (pola
# LIST_PREFERENCES_KEY di api/work_order.py), dibaca item_info, ditulis
# create_form_order setelah MR submit sukses.
UOM_DEFAULT_KEY = "production_app_form_order_uom"
LAST_UOM_CAP = 200


def _orders():
	"""Baris Form Order untuk session user. Gudang & Manufacturing Manager
	melihat semua; Manufacturing User biasa hanya buuatannya sendiri.
	Permission-filtered lewat frappe.get_list (tanpa ignore_permissions);
	tanpa izin baca MR daftar kosong, bukan error (pola papan serah terima)."""
	roles = frappe.get_roles()
	scoped = any(r in roles for r in ROLES_GUDANG) or ROLE_MANAJER_PRODUKSI in roles
	filters = {"custom_is_form_order": 1}
	if not scoped:
		filters["owner"] = frappe.session.user
	try:
		mrs = frappe.get_list(
			DOCTYPE,
			filters=filters,
			fields=[
				"name", "docstatus", "status", "owner", "creation",
				"schedule_date", "custom_note",
				"set_from_warehouse", "set_warehouse",
			],
			order_by="creation desc",
			limit_page_length=0,
		)
	except frappe.PermissionError:
		return []
	if not mrs:
		return []

	rows_by_mr = {}
	for it in frappe.get_all(
		"Material Request Item",
		filters={"parent": ("in", [m.name for m in mrs])},
		fields=["parent", "item_code", "item_name", "qty", "uom", "stock_uom", "stock_qty"],
		order_by="parent asc, idx asc",
	):
		rows_by_mr.setdefault(it.parent, []).append(it)

	# lane serah terima butuh SE read utk jujur (pola _requests)
	sent = (
		_sent_se_by_mr([m.name for m in mrs])
		if frappe.has_permission("Stock Entry", "read")
		else {}
	)
	users = _user_names({m.owner for m in mrs})

	orders = []
	for m in mrs:
		se = sent.get(m.name)
		if se:
			status = STATUS_TERKIRIM
		elif m.docstatus == 2:
			status = STATUS_BATAL
		elif m.docstatus == 0:
			status = STATUS_DRAF
		else:
			status = STATUS_MENUNGGU
		orders.append(
			{
				"mr": m.name,
				"docstatus": m.docstatus,
				"status": status,
				"items": [
					{
						"item_code": it.item_code,
						"item_name": it.item_name or it.item_code,
						"qty": flt(it.stock_qty or it.qty),
						"uom": it.stock_uom or it.uom,
					}
					for it in rows_by_mr.get(m.name, [])
				],
				"note": m.custom_note or None,
				"schedule_date": str(m.schedule_date) if m.schedule_date else None,
				"from_warehouse": m.set_from_warehouse or None,
				"to_warehouse": m.set_warehouse or None,
				"stock_entry": se.name if se else None,
				"sent_at": f"{se.posting_date} {_time_str(se.posting_time)}" if se else None,
				"owner": m.owner,
				"owner_name": users.get(m.owner),
				"creation": str(m.creation),
			}
		)
	return orders


@frappe.whitelist()
def form_order_list():
	"""Daftar Form Order (server-derived) untuk session user."""
	return {"orders": _orders()}


def _warehouses_or_throw():
	"""Rute default dari Pengaturan — kosong = error jelas dengan arah ke
	menu Pengaturan; Form Order tidak pernah menebak gudang.
	FU61: gate company dipensiunkan.
	FU60: konvergensi field dulu — self-heal pemasangan field settings
	sebelum baris di bawah membacanya (ops update-kode-tanpa-migrate)."""
	_ensure_settings_fields()
	source = frappe.db.get_single_value("Manufacturing Settings", FORM_ORDER_SOURCE_FIELD)
	target = frappe.db.get_single_value("Manufacturing Settings", FORM_ORDER_TARGET_FIELD)
	missing = [
		label
		for label, value in (
			("Gudang Asal Form Order", source),
			("Gudang Tujuan Form Order", target),
		)
		if not value
	]
	if missing:
		frappe.throw(
			_("{0} belum diatur — isi lewat menu Pengaturan.").format(", ".join(missing))
		)
	return source, target


def _valid_uoms(item_code):
	"""Pilihan satuan item dari child table konversi (UOM Conversion Detail di
	Item.uoms): faktor finite > 0, hanya UOM enabled (field `enabled` ada di
	UOM master v16 — terverifikasi di source terpasang). Item tanpa baris →
	list kosong (artinya hanya stock UOM)."""
	rows = frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": item_code, "parenttype": "Item"},
		fields=["uom", "conversion_factor"],
		order_by="idx asc",
	)
	valid = []
	for row in rows:
		if not row.uom:
			continue
		try:
			factor = float(row.conversion_factor)
		except (TypeError, ValueError):
			continue
		if math.isfinite(factor) and factor > 0:
			valid.append({"uom": row.uom, "conversion_factor": factor})
	names = [v["uom"] for v in valid]
	if names:
		enabled = set(frappe.get_all("UOM", filters={"name": ("in", names), "enabled": 1}, pluck="name"))
		valid = [v for v in valid if v["uom"] in enabled]
	return valid


def _user_uom_map():
	"""Peta {item_code: satuan terakhir dipakai user} dari user default JSON
	session user (pola LIST_PREFERENCES_KEY api/work_order.py)."""
	return frappe.parse_json(get_user_default(UOM_DEFAULT_KEY) or "{}") or {}


def _remember_last_uoms(rows):
	"""FU48c: merge peta {item_code: satuan terpilih} ke user default JSON,
	di transaksi yang sama SETELAH MR submit sukses. Cap 200 entri — bila
	lebih, entri tertua (urutan sisip) dibuang."""
	# ponytail: eviction urutan sisip — item dipakai ulang tidak naik urutan;
	# ganti ke LRU eksplisit bila urutan pemakaian jadi penting.
	prefs = _user_uom_map()
	prefs.update({row["item_code"]: row["uom"] for row in rows})
	if len(prefs) > LAST_UOM_CAP:
		prefs = dict(list(prefs.items())[-LAST_UOM_CAP:])
	set_user_default(UOM_DEFAULT_KEY, frappe.as_json(prefs))


def _validated_items(raw):
	"""[{item_code, qty, stock_uom, uom, factor}] — item stok non-batch, qty
	finite > 0. `uom` opsional: kosong → stock_uom (faktor 1); selain stock_uom
	harus anggota konversi item — SELAINNYA frappe.throw (tanpa fallback
	faktor-1 senyap, pola T39). Semua validasi selesai SEBELUM tulisan
	pertama (kontrak zero-write)."""
	rows = frappe.parse_json(raw) if isinstance(raw, str) else raw
	if not rows:
		frappe.throw(_("Form Order minimal satu baris item."))
	cleaned = []
	for row in rows:
		code = str(row.get("item_code") or "").strip() if isinstance(row, dict) else ""
		if not code:
			frappe.throw(_("Baris item tanpa item tidak diterima."))
		try:
			qty = float(row.get("qty"))
		except (TypeError, ValueError):
			qty = 0.0
		if not math.isfinite(qty) or qty <= 0:
			frappe.throw(_("Qty untuk {0} harus angka positif.").format(code))
		uom = str(row.get("uom") or "").strip() if isinstance(row, dict) else ""
		item = frappe.db.get_value(
			"Item", code, ["is_stock_item", "has_batch_no", "stock_uom"], as_dict=True
		)
		if not item:
			frappe.throw(_("Item {0} tidak ditemukan.").format(code))
		if not cint(item.is_stock_item):
			frappe.throw(_("Item {0} bukan item stok.").format(code))
		if item.has_batch_no:
			frappe.throw(
				_("Item {0} ber-batch — untuk sementara minta lewat Desk ERPNext.").format(code)
			)
		factor = 1.0
		if uom and uom != item.stock_uom:
			factors = {v["uom"]: v["conversion_factor"] for v in _valid_uoms(code)}
			if uom not in factors:
				frappe.throw(
					_("Satuan {0} tidak dikenal untuk item {1} — pilih dari pilihan satuan item.").format(
						uom, code
					)
				)
			factor = factors[uom]
		cleaned.append(
			{
				"item_code": code,
				"qty": qty,
				"stock_uom": item.stock_uom,
				"uom": uom or item.stock_uom,
				"factor": factor,
			}
		)
	return cleaned


@frappe.whitelist()
def item_info(item_code):
	"""Info tampilan untuk satu baris grid Form Order (nama, satuan, penanda
	non-stok/ber-batch) + pilihan satuan (`uoms`) + `last_uom` — satuan
	terakhir dipakai user utk item ini, DIVALIDASI ULANG terhadap pilihan
	aktual (basi → null). UX dini saja; validasi otoritatif tetap di
	create_form_order. Tanpa izin baca Item → None (bukan error)."""
	if not frappe.has_permission("Item", "read"):
		return None
	info = frappe.db.get_value(
		"Item", item_code, ["item_name", "stock_uom", "is_stock_item", "has_batch_no"], as_dict=True
	)
	if info:
		info["uoms"] = _valid_uoms(item_code)
		last = _user_uom_map().get(item_code)
		info["last_uom"] = (
			last
			if last and (last == info.stock_uom or any(v["uom"] == last for v in info["uoms"]))
			else None
		)
	return info


@frappe.whitelist()
@_retry_on_deadlock
def create_form_order(items, schedule_date=None, note=None):
	"""Produksi (Manufacturing User / Manufacturing Manager): buat + submit MR
	Material Transfer free-form dari gudang asal ke tujuan (Pengaturan), sebagai
	user sesi tanpa ignore_permissions. Satu transaksi: kegagalan validasi =
	nol tulisan."""
	_require_role(
		ROLES_PEMBUAT,
		_("Hanya Manufacturing User / Manufacturing Manager yang dapat membuat Form Order."),
	)
	frappe.has_permission(DOCTYPE, "create", throw=True)
	source, target = _warehouses_or_throw()
	rows = _validated_items(items)

	if schedule_date in (None, ""):
		schedule_date = add_days(now(), 1)  # default besok (kontrak papan serah terima)
	else:
		try:
			schedule_date = getdate(schedule_date)
		except Exception:
			frappe.throw(_("Tanggal dibutuhkan tidak valid."))

	mr = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"material_request_type": "Material Transfer",
			"company": frappe.db.get_value("Warehouse", source, "company"),
			"transaction_date": now(),
			"schedule_date": schedule_date,
			"set_from_warehouse": source,
			"set_warehouse": target,
			"custom_is_form_order": 1,
			"custom_note": str(note or "").strip() or None,
			"items": [
				{
					"item_code": row["item_code"],
					# FU48c: qty dihitung dalam satuan terpilih; konversi ditulis
					# eksplisit (native hanya mengisi field kosong — nilai eksplisit
					# bertahan lewat validate). stock_qty = qty × conversion_factor
					# dalam stock_uom; tanpa recompute native.
					"qty": row["qty"],
					"uom": row["uom"],
					"stock_uom": row["stock_uom"],
					"conversion_factor": row["factor"],
					"stock_qty": row["qty"] * row["factor"],
					"from_warehouse": source,
					"warehouse": target,
					"schedule_date": schedule_date,
				}
				for row in rows
			],
		}
	)
	mr.insert()  # session user; native permission + validation
	mr.submit()
	_remember_last_uoms(rows)  # transaksi yang sama; MR sudah pasti tersubmit
	return {"ok": True, "material_request": mr.name, "orders": _orders()}


@frappe.whitelist()
@_retry_on_deadlock
def cancel_form_order(material_request):
	"""Pembuat (atau Manufacturing Manager): batalkan Form Order yang belum
	diproses. Lock baris MR dulu, bukti SE dicek ulang di bawah lock (pola
	send_handover); cancel native jalan sebagai user sesi."""
	mr = frappe.get_doc(DOCTYPE, material_request)
	frappe.has_permission(DOCTYPE, "cancel", doc=mr, throw=True)
	if not mr.custom_is_form_order:
		frappe.throw(_("{0} bukan Form Order.").format(material_request))
	if (
		frappe.session.user != mr.owner
		and ROLE_MANAJER_PRODUKSI not in frappe.get_roles()
	):
		frappe.throw(
			_("Hanya pembuat Form Order atau Manufacturing Manager yang dapat membatalkan."),
			frappe.PermissionError,
		)

	frappe.db.get_value(DOCTYPE, material_request, "name", for_update=True)  # row lock
	mr = frappe.get_doc(DOCTYPE, material_request)  # re-read under the lock
	if mr.docstatus != 1:
		frappe.throw(
			_("Form Order {0} tidak bisa dibatalkan (docstatus {1}).").format(
				material_request, mr.docstatus
			)
		)
	_se_or_throw(material_request)
	mr.cancel()
	return {"ok": True, "material_request": material_request, "orders": _orders()}


def _se_or_throw(material_request):
	"""Bukti SE diproses, dicek dengan locking re-read (invisible competitor
	pasca-snapshot tidak lolos — pola T36 send_handover)."""
	se_now = frappe.db.get_value(
		"Stock Entry Detail",
		{"material_request": material_request, "docstatus": 1, "parenttype": "Stock Entry"},
		"parent",
		for_update=True,
	)
	if se_now:
		frappe.throw(
			_("Form Order {0} sudah diproses ({1}); tidak bisa diubah.").format(
				material_request, get_link_to_form("Stock Entry", se_now)
			)
		)


@frappe.whitelist()
@_retry_on_deadlock
def fulfill_form_order(material_request):
	"""Gudang (Stock User / Stock Manager / Gudang Barang Jadi): proses Form Order — Stock
	Entry Material Transfer dari builder native MR->SE, insert + submit dalam
	satu transaksi (kekurangan stok gagal di validasi native, rollback penuh).
	Anti-dobel: lock MR + recheck bukti SE di bawah lock."""
	_require_role(
		ROLES_GUDANG,
		_("Hanya peran gudang (Stock User / Stock Manager / Gudang Barang Jadi) yang dapat memproses Form Order."),
	)
	frappe.has_permission("Stock Entry", "create", throw=True)
	frappe.has_permission("Stock Entry", "submit", throw=True)

	frappe.db.get_value(DOCTYPE, material_request, "name", for_update=True)  # row lock
	mr = frappe.get_doc(DOCTYPE, material_request)  # re-read under the lock
	frappe.has_permission(DOCTYPE, "read", doc=mr, throw=True)
	if not mr.custom_is_form_order:
		frappe.throw(_("{0} bukan Form Order.").format(material_request))
	if mr.docstatus != 1:
		frappe.throw(
			_("Form Order {0} tidak aktif (docstatus {1}).").format(material_request, mr.docstatus)
		)
	if mr.status == "Stopped":
		frappe.throw(
			_("Form Order {0} berstatus Stopped; aktifkan kembali lewat Desk.").format(
				material_request
			)
		)
	_se_or_throw(material_request)

	se = frappe.get_doc(make_mr_stock_entry(material_request))
	se.insert()
	se.submit()  # native shortage/validation failures roll the whole request back
	return {
		"ok": True,
		"material_request": material_request,
		"stock_entry": se.name,
		"orders": _orders(),
	}
