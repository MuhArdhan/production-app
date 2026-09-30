# Serah Terima (handover) board — read API (T23).
#
# SERVER-DERIVED truth (HANDOVER_PLAN §4.1): every lane, quantity and flag is
# recomputed from current MR/SE/batch documents on each call; the UI never
# writes state. T24 mutations reuse _build_board() as their refresh response;
# T25 consumes the payload documented in
# .superpowers/sdd/HANDOVER_PLAN/task-23-report.md.
#
# - MR.transfer_status is never read (not maintained by the MR->SE flow, T21).
# - Permission-filtered per session user, no ignore_permissions: without
#   Work Order / Material Request read the board is empty, not an error (§4.8).
# - No hardcoded warehouses: the lot warehouse is the WO's Manufacture SE
#   finished-row t_warehouse (WO fg_warehouse fallback); the send target comes
#   from Manufacturing Settings custom_default_handover_warehouse (§4.10).
# - T31 (ruling R2): the batchless pool source and new MRs' from_warehouse come
#   from Manufacturing Settings custom_default_handover_source_warehouse when
#   set, else the SE-derived lot warehouse; batch-tracked lots stay SE-derived.
#   Requests carry the Work Order's FULL produced_qty (R3); send moves the MR's
#   requested qty and never stops the MR (R6).
# - T35 three-lane cutover: TWO lanes derive from native documents only —
#   request (submitted MR without SE evidence) and terkirim (submitted Stock
#   Entry evidence, which outranks even an abnormally cancelled MR). The
#   retired postpacking confirmation advances nothing. Box kg + whole count
#   allocations live on the Work Order summary (Link + 4 fields), written
#   atomically by create_request behind the WO lock; the MR stays a pure
#   request document. T39: the count unit is UNIVERSAL — the item's warehouse
#   display UOM (the stock UOM itself at exact factor 1, or any alternate
#   display UOM with a valid conversion row). Count math uses the RAW
#   _enrich_units factor — never the `or 1` display fallback.

from collections import defaultdict

import functools

import json

import math

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_link_to_form, now

from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	get_available_batches,
	get_stock_ledgers_batches,
	update_available_batches,
)

from production_app.api.work_order import (
	_enrich_units,
)

# Sisi gudang serah terima: role legacy "Gudang Barang Jadi" ATAU pasangan
# native Stock User/Stock Manager (keputusan user 2026-09-14; FU64 2026-09-29
# gudang resmi pindah ke role native — Stock Manager diterima di SEMUA gate;
# dulu tidak, sehingga create/cancel request & fulfill Form Order ditolak
# untuk pemegang SM). Data access mereka lewat hak native + matrix upgrade.
ROLE_GUDANG = "Gudang Barang Jadi"
ROLES_GUDANG = ("Gudang Barang Jadi", "Stock User", "Stock Manager")
ROLE_PRODUKSI = "Manufacturing User"
# FO 2026-09-18: flag terpisah untuk Manufacturing Manager — is_produksi TIDAK
# dilebarkan (isGudangOnly/landing & aksi kirim di UI tidak berubah); menu Form
# Order tampil untuk produksi ATAU manager, guard server tetang otoritatif.
ROLE_MANAJER_PRODUKSI = "Manufacturing Manager"

LANE_REQUEST = "request"
LANE_KIRIM = "terkirim"


def _time_str(value):
    """Zero-padded HH:MM:SS for DB Time values (arrive as timedelta; str()
    renders single-digit hours, which would mis-sort FIFO across hours)."""
    if hasattr(value, "total_seconds"):
        total = int(value.total_seconds())
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return str(value)


@frappe.whitelist()
def handover_board():
    """Three-lane Serah Terima board (Cold Storage lots -> Request Gudang ->
    Terkirim) for the session user."""
    return _build_board()


def _build_board():
    """Board payload; T24 actions return this as the refreshed board."""
    wo_rows = _wo_lot_rows()
    requests = _requests(wo_rows)
    return {
        "target_warehouse": _target_warehouse(),
        "source_warehouse": _source_warehouse(),
        "roles": _roles(),
        "lots": _lots(wo_rows, requests),
        "requests": requests,
    }


def _target_warehouse():
	return (
		frappe.db.get_single_value(
			"Manufacturing Settings", "custom_default_handover_warehouse"
		)
		or None
	)


def _source_warehouse():
	"""T31 (R2): gudang asal serah terima. Empty -> None; every caller falls
	back to the SE-derived lot warehouse (the board never hard-fails)."""
	return (
		frappe.db.get_single_value(
			"Manufacturing Settings", "custom_default_handover_source_warehouse"
		)
		or None
	)


def _pool_warehouse(lot_row):
    """The batchless pool source: the source setting overrides the SE-derived
    lot warehouse; when unset the fallback keeps current behavior (R2).
    FU61: gate company dipensiunkan — setting berlaku langsung."""
    return _source_warehouse() or lot_row.warehouse


def _roles():
    roles = frappe.get_roles()
    return {
        "is_gudang": any(r in roles for r in ROLES_GUDANG),
        "is_produksi": ROLE_PRODUKSI in roles,
        "is_manajer_produksi": ROLE_MANAJER_PRODUKSI in roles,
        # FU64: kapabilitas, bukan nama role — menu Pengaturan mengikuti
        # izin write Manufacturing Settings yang sama yang di-cek server
        # saat simpan (native: Manufacturing Manager).
        "can_settings": frappe.has_permission("Manufacturing Settings", "write"),
    }


def _batch_quantities(requirements):
	"""Physical batch quantity by (batch, warehouse), preserving the old
	get_batch_qty(batch, warehouse) semantics without its per-lot queries."""
	requirements = {
		(item_code, batch_no, warehouse)
		for item_code, batch_no, warehouse in requirements
		if item_code and batch_no and warehouse
	}
	if not requirements:
		return defaultdict(float)

	kwargs = frappe._dict(
		item_code=sorted({item_code for item_code, _batch, _warehouse in requirements}),
		batch_no=sorted({batch_no for _item, batch_no, _warehouse in requirements}),
		warehouse=sorted({warehouse for _item, _batch, warehouse in requirements}),
		based_on=frappe.get_single_value("Stock Settings", "pick_serial_and_batch_based_on"),
		for_stock_levels=False,
		consider_negative_batches=False,
	)
	batches = get_available_batches(kwargs)
	update_available_batches(batches, get_stock_ledgers_batches(kwargs))

	wanted = {(batch_no, warehouse) for _item, batch_no, warehouse in requirements}
	quantities = defaultdict(float)
	for row in batches:
		key = (row.batch_no, row.warehouse)
		if key in wanted and flt(row.qty) > 0:
			quantities[key] += flt(row.qty)
	return quantities


def _wo_lot_rows(wo_names=None):
	"""One lot row per Work Order that has Manufacture history, enriched with
	the shared qtyInPack logic (_enrich_units) and item display names.

	Lot identity (§3): the FG batch of the WO's single Manufacture SE in that
	SE's finished-item warehouse; entered_at = the SE posting datetime (FIFO).
	A WO with multiple Manufacture SEs / batches (legacy partial history) gets
	an explicit unsupported card instead of an error or silent flattening
	(§4.9). Follow-up 8: an FG item WITHOUT batch tracking yields a BATCHLESS
	lot (batch=None) — all its WOs share one item-level stock pool (see
	_lots), and multiple Manufactures are harmless. Numeric lot fields stay
	None until _lots() applies the reservation. T31 (R7): produced_qty is the
	WO's finished-goods total and completed_at the LATEST Manufacture posting
	(rows iterate ascending, so the stamp is overwritten each iteration).
	"""
	filters = {"docstatus": 1}
	if wo_names is not None:
		wo_names = sorted({name for name in wo_names if name})
		if not wo_names:
			return []
		filters["name"] = ("in", wo_names)
	try:
		wos = frappe.get_list(
			"Work Order",
			filters=filters,
			fields=[
				"name", "production_item", "qty", "produced_qty", "custom_adonan_ke",
				"fg_warehouse", "creation", "company",
				# T35 summary block: bulk-loaded once, mapped onto request rows
				"custom_handover_material_request",
				"custom_box_1", "custom_box_1_qty", "custom_box_2", "custom_box_2_qty",
				"custom_box_3", "custom_box_3_qty",
			],
			order_by="creation desc",
			limit_page_length=0,
		)
	except frappe.PermissionError:
		return []
	if not wos:
		return []
	# lots are SE/batch derivatives; both roles that read WOs also read these
	# (T22 matrix) — without them the Cold Storage lane is not derivable
	if not frappe.has_permission("Stock Entry", "read") or not frappe.has_permission(
		"Batch", "read"
	):
		return []

	ses = frappe.get_all(
		"Stock Entry",
		filters={
			"work_order": ("in", [w.name for w in wos]),
			"purpose": "Manufacture",
			"docstatus": 1,
		},
		fields=["name", "work_order", "posting_date", "posting_time"],
		order_by="posting_date asc, posting_time asc, creation asc",
	)
	if not ses:
		return []
	fg_rows = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": ("in", [s.name for s in ses]), "is_finished_item": 1},
		fields=["parent", "t_warehouse", "serial_and_batch_bundle"],
	)
	rows_by_se = {}
	for r in fg_rows:
		rows_by_se.setdefault(r.parent, []).append(r)
	batches_by_bundle = {}
	bundles = [r.serial_and_batch_bundle for r in fg_rows if r.serial_and_batch_bundle]
	if bundles:
		for e in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": ("in", bundles)},
			fields=["parent", "batch_no"],
		):
			batches_by_bundle.setdefault(e.parent, []).append(e.batch_no)

	history = {}
	for s in ses:  # ascending posting -> first seen is the earliest entry
		cur = history.setdefault(
			s.work_order,
			{
				"manufactures": 0,
				"batches": [],
				"warehouse": None,
				"entered_at": None,
				"completed_at": None,
			},
		)
		cur["manufactures"] += 1
		stamp = f"{s.posting_date} {_time_str(s.posting_time)}"
		if cur["entered_at"] is None:
			cur["entered_at"] = stamp
		cur["completed_at"] = stamp  # ascending: the last write is the LATEST
		for r in rows_by_se.get(s.name, []):
			cur["warehouse"] = cur["warehouse"] or r.t_warehouse
			if r.serial_and_batch_bundle:
				cur["batches"].extend(batches_by_bundle.get(r.serial_and_batch_bundle, []))
			else:
				cur["batches"].append(None)

	# follow-up 8: an FG item without batch tracking produces bundle-less rows —
	# those WOs become BATCHLESS lots (stock pool per item, see _lots). The flag
	# comes from the Item master, not the rows, so a legacy bundle-less history
	# on an item that IS batch-tracked stays unsupported (transition guard).
	try:
		item_batch_flags = {
			r["name"]: r["has_batch_no"]
			for r in frappe.get_all(
				"Item",
				filters={"name": ("in", sorted({w.production_item for w in wos}))},
				fields=["name", "has_batch_no"],
			)
		}
	except frappe.PermissionError:
		return []  # same policy as the SE/Batch gate: no read, empty board

	rows = []
	for w in wos:
		cur = history.get(w.name)
		if not cur:
			continue  # never manufactured — the WO workspace's domain, not a lot
		row = frappe._dict(
			{
				"batch": None,
				"batchless": False,
				"work_order": w.name,
				# FU60: WAJIB ikut — gate company FU58 (_pool_warehouse dst.)
				# membaca lot_row.company; tanpa ini selalu None dan setiap
				# setting company non-kosong mematikan pool override.
				"company": w.company,
				"item_code": w.production_item,
				"production_item": w.production_item,  # consumed by _enrich_units
				"custom_uom": None,
				"qty": flt(w.qty),
				"produced_qty": flt(w.produced_qty),
				"adonan_ke": w.custom_adonan_ke or None,  # FU69: 0 = kosong
				"warehouse": cur["warehouse"] or w.fg_warehouse,
				"entered_at": str(cur["entered_at"]),
				"completed_at": cur["completed_at"],
				"physical_qty": None,
				"reserved_qty": None,
				"available_qty": None,
				"unsupported": False,
				"unsupported_reason": None,
				"has_older_lot_same_item": False,
				"custom_handover_material_request": w.custom_handover_material_request,
				"custom_box_1": w.custom_box_1,
				"custom_box_1_qty": w.custom_box_1_qty,
				"custom_box_2": w.custom_box_2,
				"custom_box_2_qty": w.custom_box_2_qty,
				"custom_box_3": w.custom_box_3,
				"custom_box_3_qty": w.custom_box_3_qty,
			}
		)
		unique = {b for b in cur["batches"] if b}
		batchless_rows = None in cur["batches"]
		if batchless_rows and not unique and not item_batch_flags.get(w.production_item):
			# no batch dimension exists for this item: every Manufacture lands in
			# one ITEM-level stock pool, so multiple Manufactures flatten honestly
			row.batchless = True
		elif batchless_rows and not unique:
			row.unsupported = True
			row.unsupported_reason = _(
				"Work Order {0}: FG tanpa batch padahal itemnya kini berbatch (transisi) — selaraskan stok lewat Desk."
			).format(w.name)
		elif cur["manufactures"] > 1 or batchless_rows or len(unique) != 1:
			parts = [_("{0} Manufacture").format(cur["manufactures"])]
			if unique:
				parts.append(_("{0} batch").format(len(unique)))
			if batchless_rows:
				parts.append(
					_("{0} baris FG tanpa batch").format(sum(1 for b in cur["batches"] if b is None))
				)
			row.unsupported = True
			row.unsupported_reason = _(
				"Work Order {0}: {1} — tidak bisa dipetakan ke satu lot; selesaikan lewat Desk."
			).format(w.name, " / ".join(parts))
		else:
			row.batch = next(iter(unique))
		rows.append(row)

	batch_qty = _batch_quantities(
		(r.item_code, r.batch, r.warehouse)
		for r in rows
		if r.batch and not r.unsupported
	)
	for row in rows:
		if row.batch and not row.unsupported:
			row.physical_qty = flt(batch_qty.get((row.batch, row.warehouse), 0))

	_enrich_units(rows)  # shared qtyInPack source — UOM logic not duplicated
	names = _item_names({r.item_code for r in rows})
	for row in rows:
		row.item_name = names.get(row.item_code) or row.item_code
		row.qty_in_pack = flt(row.display_conversion_factor or 1)
	return rows


def _requests(wo_rows, wo_names=None, item_codes=None):
	"""Handover request rows — one per Material Request bound to a Work Order
	via Material Request Item.custom_work_order (T22 field).

	Lane mapping (documents only, T35): terkirim = a submitted Stock Entry
	exists against the MR (Stock Entry Detail `material_request` link, T21) —
	even if the MR is abnormally cancelled AFTER that send; else request =
	submitted and not stopped. The retired postpacking confirmation advances
	nothing. Edge states are never omitted and never raise: draft and
	cancelled MRs come back with lane=None plus a flag; a stopped MR without
	SE is a dead request (needs a manual desk unstop) parked in the request
	lane with flag="stopped" — visible, but reserving nothing (§4.3).

	Box summary (T35): kg + whole count (item's warehouse display UOM, T39)
	come from the BULK Work Order lot rows while the WO Link points at the MR;
	pre-cutover MRs (empty Link) fall back to their own kg fields with BOTH
	count values empty. No per-card Work Order lookup happens in this loop.
	"""
	item_filters = {"custom_work_order": ("is", "set")}
	if wo_names is not None:
		wo_names = sorted({name for name in wo_names if name})
		if not wo_names:
			return []
		item_filters["custom_work_order"] = ("in", wo_names)
	if item_codes is not None:
		item_codes = sorted({code for code in item_codes if code})
		if not item_codes:
			return []
		item_filters["item_code"] = ("in", item_codes)
	mr_names = frappe.get_all("Material Request Item", filters=item_filters, pluck="parent")
	if not mr_names:
		return []
	try:
		mrs = frappe.get_list(
			"Material Request",
			filters={
				"name": ("in", sorted(set(mr_names))),
				"material_request_type": "Material Transfer",
			},
			fields=[
				"name", "docstatus", "status", "owner", "creation",
				"set_from_warehouse", "set_warehouse",
				"custom_box_1", "custom_box_2",
				"custom_good_qty_postpacking", "custom_reject_qty_postpacking",
				"custom_trial_qty_postpacking", "custom_sisa_qty_postpacking",
				"custom_jam_packing", "custom_qc_packing",
				"custom_postpacking_confirmed",
				"custom_handover_box_plan",
			],
			order_by="creation desc",
			limit_page_length=0,
		)
	except frappe.PermissionError:
		return []
	if not mrs or not frappe.has_permission("Stock Entry", "read"):
		return []  # lanes cannot be derived truthfully without SE visibility

	bound_items = {}
	for it in frappe.get_all(
		"Material Request Item",
		filters={
			"parent": ("in", [m.name for m in mrs]),
			"custom_work_order": ("is", "set"),
		},
		fields=[
			"parent", "item_code", "item_name", "qty", "stock_qty", "stock_uom",
			"custom_work_order",
		],
		order_by="idx asc",
	):
		bound_items.setdefault(it.parent, []).append(it)
	if not bound_items:
		return []

	wo_by_name = {r.work_order: r for r in wo_rows}
	sent = _sent_se_by_mr([m.name for m in mrs])
	users = _user_names(
		{m.owner for m in mrs} | {m.custom_qc_packing for m in mrs if m.custom_qc_packing}
	)

	# W19 shared-box groups: the plan's physical rows + live member count are
	# bulk-loaded ONCE per call when any MR references a plan (per-WO box chips
	# do not exist for groups — kg was weighed per physical box, not per WO).
	group_boxes_by_plan, group_size_by_plan = {}, {}
	box_plan_names = sorted({m.custom_handover_box_plan for m in mrs if m.custom_handover_box_plan})
	if box_plan_names:
		for r in frappe.get_all(
			"Handover Box Plan Item",
			filters={"parent": ("in", box_plan_names)},
			fields=["parent", "kg", "qty"],
			order_by="idx asc",
			limit=0,
		):
			group_boxes_by_plan.setdefault(r.parent, []).append(
				{"kg": flt(r.kg), "qty": int(r.qty)}
			)
		# group_size = LIVE members only: submitted (active request OR shipped);
		# cancelled/draft members never counted (one grouped query)
		for r in frappe.get_all(
			"Material Request",
			filters={"custom_handover_box_plan": ("in", box_plan_names), "docstatus": 1},
			fields=[{"COUNT": "name", "as": "group_size"}, "custom_handover_box_plan"],
			group_by="custom_handover_box_plan",
			limit=0,
		):
			group_size_by_plan[r.custom_handover_box_plan] = r.group_size

	rows = []
	for m in mrs:
		items = bound_items.get(m.name)
		if not items:
			continue  # not a handover MR (no Work Order binding)
		first = items[0]
		se = sent.get(m.name)
		lane, flag = None, None
		if se:
			lane = LANE_KIRIM  # document evidence outranks an abnormal docstatus
		elif m.docstatus == 2:
			flag = "cancelled"  # leaves the lanes, stays on the board (audit)
		elif m.docstatus == 0:
			flag = "draft"  # not yet submitted — no reservation, no action
		elif m.status == "Stopped":
			lane, flag = LANE_REQUEST, "stopped"
		else:
			lane = LANE_REQUEST
		lot = wo_by_name.get(first.custom_work_order)
		qty_in_pack, adonan_ke = (lot.qty_in_pack, lot.adonan_ke) if lot else (None, None)
		if lot is None:
			# WO has no lot row (never manufactured / not submitted): the mockup
			# dialogs still read qtyInPack + adonan off the request's item, so
			# resolve it the same way instead of handing the UI a blank factor
			enriched = frappe._dict(production_item=first.item_code, custom_uom=None)
			_enrich_units([enriched])
			qty_in_pack = flt(enriched.display_conversion_factor or 1)
			display_uom = enriched.display_uom or enriched.stock_uom
		else:
			display_uom = lot.display_uom or lot.stock_uom
		if lot is not None and lot.custom_handover_material_request == m.name:
			# the WO summary owns the boxes while its Link points here
			box_1, box_1_qty = lot.custom_box_1 or None, lot.custom_box_1_qty or None
			box_2, box_2_qty = lot.custom_box_2 or None, lot.custom_box_2_qty or None
			box_3, box_3_qty = lot.custom_box_3 or None, lot.custom_box_3_qty or None
		else:
			# pre-cutover MR: legacy MR kg, count values never invented
			box_1, box_1_qty = m.custom_box_1 or None, None
			box_2, box_2_qty = m.custom_box_2 or None, None
			box_3, box_3_qty = None, None  # MR never carried a Box 3 field
		rows.append(
			{
				"mr": m.name,
				"docstatus": m.docstatus,
				"status": m.status,
				"lane": lane,
				"flag": flag,
				"item_code": first.item_code,
				"item_name": first.item_name or first.item_code,
				"qty": flt(first.stock_qty or first.qty),  # stock UOM (Pcs)
				"stock_uom": first.stock_uom,
				"work_order": first.custom_work_order,
				"batch": lot.batch if lot and not lot.unsupported else None,
				"qty_in_pack": qty_in_pack,
				"display_uom": display_uom,
				"adonan_ke": adonan_ke,
				"box_1": box_1,
				"box_1_qty": box_1_qty,
				"box_2": box_2,
				"box_2_qty": box_2_qty,
				"box_3": box_3,
				"box_3_qty": box_3_qty,
				"boxes": [b for b in (box_1, box_2, box_3) if b],
				"box_plan": m.custom_handover_box_plan or None,
				"group_boxes": group_boxes_by_plan.get(m.custom_handover_box_plan, []),
				# plan rows always carry an int count (0 = no live member);
				# non-group rows stay None
				"group_size": (
					group_size_by_plan.get(m.custom_handover_box_plan, 0)
					if m.custom_handover_box_plan
					else None
				),
				"from_warehouse": m.set_from_warehouse or None,
				"to_warehouse": m.set_warehouse or None,
				"postpacking": {
					"good": flt(m.custom_good_qty_postpacking),
					"reject": flt(m.custom_reject_qty_postpacking),
					"trial": flt(m.custom_trial_qty_postpacking),
					"sisa": flt(m.custom_sisa_qty_postpacking),
					"jam_packing": _time_str(m.custom_jam_packing)
					if m.custom_jam_packing
					else None,
					"qc_packing": m.custom_qc_packing or None,
					"qc_packing_name": users.get(m.custom_qc_packing),
					"confirmed": bool(m.custom_postpacking_confirmed),
				},
			"stock_entry": se.name if se else None,
			"sent_at": f"{se.posting_date} {_time_str(se.posting_time)}" if se else None,
			"owner": m.owner,
			"owner_name": users.get(m.owner),
			"creation": str(m.creation),
		}
		)

	# FU29: live stock at the ROUTE origin (MR from_warehouse) so request
	# cards can warn BEFORE a send is attempted; cached per (kind, key) — the
	# same quantities send_handover re-checks under the lock.
	route_cache = {}
	for r in rows:
		if r["lane"] != LANE_REQUEST or not r["from_warehouse"]:
			continue
		lot = wo_by_name.get(r["work_order"])
		if not lot or lot.unsupported or (not lot.batchless and not r["batch"]):
			continue
		if lot.batchless:
			key = ("i", r["item_code"], r["from_warehouse"])
			if key not in route_cache:
				route_cache[key] = _item_stock(r["item_code"], r["from_warehouse"])
		else:
			key = ("b", r["batch"], r["from_warehouse"])
			if key not in route_cache:
				route_cache[key] = flt(get_batch_qty(r["batch"], r["from_warehouse"]) or 0)
		r["route_available"] = route_cache[key]
	return rows


def _sent_se_by_mr(mr_names):
	"""Earliest submitted Stock Entry per MR. `material_request` lives on
	Stock Entry Detail (the link the native MR->SE builder sets, T21), so the
	lookup goes through the detail rows and re-checks the submitted headers."""
	links = frappe.get_all(
		"Stock Entry Detail",
		filters={
			"material_request": ("in", mr_names),
			"docstatus": 1,
			"parenttype": "Stock Entry",
		},
		fields=["parent", "material_request"],
	)
	if not links:
		return {}
	link_by_se = {d.parent: d.material_request for d in links}
	sent = {}
	for r in frappe.get_all(
		"Stock Entry",
		filters={"name": ("in", list(link_by_se)), "docstatus": 1},
		fields=["name", "posting_date", "posting_time"],
		order_by="posting_date asc, posting_time asc, creation asc",
	):
		sent.setdefault(link_by_se[r.name], r)
	return sent


# ------------------------------------------------ FU23: field native di WO
# Penanda serah terima di form Work Order Desk (custom_handover_status).
# Workspace TETAP memakai derivasi live (work_order._handover_enrich) supaya
# tidak pernah drift; field ini adalah cermin yang disinkronkan doc_events.

HANDOVER_STATUS_FIELD = "custom_handover_status"
# nilai Select = label Indonesia (persis options Custom Field upgrade.py)
HANDOVER_STATUS_LABEL = {
	LANE_REQUEST: "Diminta Gudang",
	LANE_KIRIM: "Terkirim",
}

# FU37: judul Error Log untuk kegagalan mirror doc_events (tidak dinaikkan).
SYNC_ERROR_TITLE = "Production App: sinkronisasi status serah terima gagal"


def _handover_sync_ready():
	"""FU37 (insiden MAT-STE-2026-06920): doc_events mirror butuh metadata
	app — MRI.custom_work_order + WO.custom_handover_status. Saat kode sudah
	terpasang tetapi migrate belum dijalankan, keduanya absen dari meta dan
	query mirror meledak "Unknown column" yang membatalkan submit Stock
	Entry native. Mirror adalah data TURUNAN: kalau meta belum siap, sync
	dilewati, bukan menggagalkan transaksi dokumen lain."""
	return (
		frappe.get_meta("Material Request Item").has_field("custom_work_order")
		and frappe.get_meta("Work Order").has_field(HANDOVER_STATUS_FIELD)
	)


def _handover_state(wo_names):
	"""SATU resolver status+Link serah terima per Work Order (dipakai
	_handover_lanes, sinkronisasi ringkasan, dan migrasi T35 supaya preseden
	lane dan Link tidak bisa drift). Kandidat diiterasi TERBARU lebih dulu dan
	kandidat PERTAMA yang relevan memiliki state — sebuah kandidat lebih LAMA
	(tinggal SE-nya pun) tidak pernah menurunkan MR terpilih yang sama-atau-lebih
	baru. Preseden dari dokumen:

	- evidence Stock Entry SUBMITTED menentukan lane HANYA saat ia evidence
	  terbaru yang relevan — termasuk MR yang anehnya sudah cancel setelah
	  kirim (state = terkirim, Link = MR-nya);
	- tanpa SE terbaru: MR submitted non-cancelled TERBARU memiliki Link
	  (state = request; termasuk Stopped tanpa SE — pernah diminta);
	- MR draft/cancelled tanpa SE tidak memiliki apa pun.

	Mengembalikan {wo_name: {"lane": ..., "mr": ...}}; {} saat kosong/tanpa
	izin (pemanggil menampilkan tanpa flag, tanpa error)."""
	wo_names = [n for n in wo_names if n]
	if not wo_names:
		return {}
	try:
		items = frappe.get_all(
			"Material Request Item",
			filters={"custom_work_order": ("in", wo_names)},
			fields=["parent", "custom_work_order"],
			limit=0,
		)
		mr_names = sorted({i.parent for i in items})
		mrs = (
			frappe.get_all(
				"Material Request",
				filters={
					"name": ("in", mr_names),
					"material_request_type": "Material Transfer",
				},
				fields=["name", "docstatus", "creation"],
				order_by="creation desc",
				limit=0,
			)
			if mr_names
			else []
		)
	except frappe.PermissionError:
		return {}
	if not mrs:
		return {}
	sent = _sent_se_by_mr(mr_names)
	wos_by_mr = defaultdict(list)
	for i in items:
		wos_by_mr[i.parent].append(i.custom_work_order)
	state = {}
	for m in mrs:  # newest first — the newest relevant candidate owns a WO
		for wo_name in wos_by_mr.get(m.name, ()):
			if wo_name in state:
				# fix round 1: an OLDER candidate (even one carrying submitted-SE
				# evidence) never demotes an equal-or-newer selected MR — the
				# Link must follow the newest request, or the board would hang
				# the newer request's boxes on the older, already-sent MR
				continue
			if m.name in sent:
				# SE evidence decides the lane only when it is the newest
				# relevant evidence (incl. an MR abnormally cancelled AFTER send)
				state[wo_name] = {"lane": LANE_KIRIM, "mr": m.name}
			elif m.docstatus == 1:
				state[wo_name] = {"lane": LANE_REQUEST, "mr": m.name}
	return state


def _handover_lanes(wo_names):
	"""FU23: lane serah terima paling maju per Work Order — kini pembungkus
	tipis di atas _handover_state bersama (satu derivasi untuk flag workspace,
	field native, dan ringkasan Link). Tanpa kandidat → {}."""
	return {
		wo_name: state["lane"]
		for wo_name, state in _handover_state(wo_names).items()
	}


WO_SUMMARY_FIELDS = (
	"custom_handover_material_request",
	"custom_box_1", "custom_box_1_qty", "custom_box_2", "custom_box_2_qty",
	"custom_box_3", "custom_box_3_qty",
)


def _sync_handover_summary(wo_names):
	"""Fail-honest summary writer: derive status + Link per Work Order from
	documents (_handover_state) and write ONLY actual diffs. The summary
	fields are read_only on the form — this controlled db write is the gate
	(the role-gated actions + this sync + the migration share it). Clearing
	rules (T35): a WO whose only evidence vanished (unsent cancelled MR, no
	replacement) loses the Link and all box values; a live or sent
	request keeps the kg and never gains invented count values. Returns the
	WO names actually written."""
	wo_names = sorted({n for n in wo_names if n})
	if not wo_names:
		return []
	states = _handover_state(wo_names)
	priors = frappe.get_all(
		"Work Order",
		filters={"name": ("in", wo_names)},
		fields=["name", HANDOVER_STATUS_FIELD, *WO_SUMMARY_FIELDS],
		limit=0,
	)
	written = []
	for prior in priors:
		link = prior.custom_handover_material_request or None
		status = prior.get(HANDOVER_STATUS_FIELD) or None
		state = states.get(prior.name)
		values = {}
		if state:
			if link != state["mr"]:
				values["custom_handover_material_request"] = state["mr"]
				# T36 ruling: the box values belong to the request that owned
				# the outgoing Link. Falling back to another MR (cancelled-unsent
				# M2 above a sent M1) must never inherit them — and the MR is a
				# pure request document, so the previous allocation is unrecoverable:
				# cleared is the honest value.
				if any(flt(prior.get(f)) for f in WO_SUMMARY_FIELDS[1:]):
					values.update({f: 0 for f in WO_SUMMARY_FIELDS[1:]})
			label = HANDOVER_STATUS_LABEL[state["lane"]]
			if status != label:
				values[HANDOVER_STATUS_FIELD] = label
		else:
			if status is not None:
				values[HANDOVER_STATUS_FIELD] = None
			if link:
				# the current Link has no SE evidence (else state would exist):
				# cancelled unsent request with no replacement -> clear all
				values["custom_handover_material_request"] = None
				values.update({f: 0 for f in WO_SUMMARY_FIELDS[1:]})
		if values:
			frappe.db.set_value("Work Order", prior.name, values, update_modified=False)
			written.append(prior.name)
	return written


def sync_handover_status(wo_names):
	"""Kompatibilitas (doc_events + migrasi T35): tulis status + Link ringkasan
	serah terima di Work Order dari derivasi dokumen. WO tanpa lane aktif
	dikosongkan (semua MR batal). Mengembalikan nama WO yang berubah."""
	return _sync_handover_summary(wo_names)


def sync_from_material_request(doc, method=None):
	"""doc_events Material Request (submit/cancel/update) → WO terikat item.
	FU37 fail-safe: mirror tidak boleh menggagalkan transaksi MR native —
	skip saat metadata app belum termigrasi; error lain (termasuk dari
	pemeriksaan metadata itu sendiri) dicatat ke Error Log, tidak dinaikkan."""
	try:
		if not _handover_sync_ready():
			return
		sync_handover_status(
			[i.custom_work_order for i in (doc.items or []) if i.get("custom_work_order")]
		)
	except Exception:
		frappe.log_error(
			title=SYNC_ERROR_TITLE,
			message=f"Material Request {doc.name}\n\n{frappe.get_traceback()}",
		)


def sync_from_stock_entry(doc, method=None):
	"""doc_events Stock Entry (submit/cancel) → MR terikat → WO terikat.
	FU37 fail-safe: Stock Entry manual/reguler tidak pernah gagal karena
	mirror serah terima — skip saat metadata app belum termigrasi; error
	lain (termasuk dari pemeriksaan metadata itu sendiri) dicatat ke Error
	Log, tidak dinaikkan."""
	try:
		if not _handover_sync_ready():
			return
		mrs = sorted({d.material_request for d in (doc.items or []) if d.get("material_request")})
		if not mrs:
			return
		sync_handover_status(frappe.get_all(
			"Material Request Item",
			filters={"parent": ("in", mrs), "custom_work_order": ("is", "set")},
			pluck="custom_work_order",
			distinct=True,
			limit=0,
		))
	except Exception:
		frappe.log_error(
			title=SYNC_ERROR_TITLE,
			message=f"Stock Entry {doc.name}\n\n{frappe.get_traceback()}",
		)


def backfill_handover_status():
	"""Sekali (bench execute): sinkronkan field untuk WO yang pernah terikat MR."""
	wos = frappe.get_all(
		"Material Request Item",
		filters={"custom_work_order": ("is", "set")},
		pluck="custom_work_order",
		distinct=True,
		limit=0,
	)
	sync_handover_status(wos)
	return len(wos)


def _reserved_by_wo(requests):
	"""§4.3: reservation = Σ qty of submitted, not stopped/cancelled MRs
	without a Stock Entry, bound to the Work Order — exactly the request-lane
	rows carrying no edge flag (draft/cancelled have no lane, stopped is
	flagged)."""
	reserved = {}
	for r in requests:
		if r["lane"] == LANE_REQUEST and not r["flag"]:
			reserved[r["work_order"]] = reserved.get(r["work_order"], 0.0) + r["qty"]
	return reserved


def _reserved_by_item(requests, wo_rows):
	"""Follow-up 8 — batchless pool reservation: batchless stock has no batch
	dimension, so every active MR of the ITEM (any WO) holds from the same
	pool. T31 (R2): the pool warehouse is the WO lot row's POOL warehouse
	(source setting overrides), NOT the MR's stored from_warehouse — so MRs
	created before the setting existed still reserve the pool after it appears.
	Fallback when the WO has no lot row: the request's own from_warehouse."""
	wo_by_name = {r.work_order: r for r in wo_rows}
	reserved = {}
	for r in requests:
		if r["lane"] == LANE_REQUEST and not r["flag"]:
			lot = wo_by_name.get(r["work_order"])
			warehouse = _pool_warehouse(lot) if lot else r["from_warehouse"]
			key = (r["item_code"], warehouse)
			reserved[key] = reserved.get(key, 0.0) + r["qty"]
	return reserved


def _item_stock(item_code, warehouse):
	"""Live item+warehouse stock straight off the ledger (batchless pools have
	no batch dimension to ask get_batch_qty about)."""
	return flt(
		frappe.db.sql(
			"select sum(actual_qty) from `tabStock Ledger Entry`"
			" where item_code=%s and warehouse=%s and is_cancelled=0",
			(item_code, warehouse),
		)[0][0]
		or 0
	)


def _lots(wo_rows, requests):
	"""Finish the lot cards: live reserved/available math, the drop rule and
	the FIFO hint. physical_qty is the live ledger quantity of the batch in the
	lot warehouse (§4.2 — never a stored counter); available is never clamped,
	negative availability surfaces as-is.

	Follow-up 8: a BATCHLESS lot (FG item without batch tracking) cannot own
	stock — the item's whole balance in the POOL warehouse (source setting
	override, R2) is one pool shared by every WO of that item, and reservations
	pool the same way. All batchless WO-cards of an item therefore show the
	same honest pool numbers."""
	reserved = _reserved_by_wo(requests)
	reserved_item = _reserved_by_item(requests, wo_rows)
	pool_stock = {}
	# a request bound to a lane keeps its (even empty) lot listed
	bound = {r["work_order"] for r in requests if r["lane"]}
	lots = []
	for row in wo_rows:
		if row.unsupported:
			lots.append(row)
			continue
		if row.batchless:
			pool_wh = _pool_warehouse(row)
			key = (row.item_code, pool_wh)
			if key not in pool_stock:
				pool_stock[key] = _item_stock(row.item_code, pool_wh)
			row.physical_qty = pool_stock[key]
			row.reserved_qty = flt(reserved_item.get(key, 0.0))
		else:
			row.reserved_qty = reserved.get(row.work_order, 0.0)
		row.available_qty = row.physical_qty - row.reserved_qty
		# FU30: penanda manual "Gudang Confirmed" dipensiunkan — lot turun murni
		# dari stok (physical 0) / Status Serah Terima; kecuali masih diikat
		# permintaan aktif (dialognya butuh data lot).
		if row.physical_qty <= 0 and row.work_order not in bound:
			continue  # empty lot nobody references — drops off the lane
		lots.append(row)

	lots.sort(key=lambda r: (r.unsupported, r.entered_at, r.work_order))
	seen_item = set()
	for row in lots:  # FIFO walk: a still-listed earlier lot hints newer ones
		if row.unsupported:
			continue
		row.has_older_lot_same_item = row.item_code in seen_item
		seen_item.add(row.item_code)
	return lots


def _item_names(codes):
	if not codes:
		return {}
	return {
		d.name: d.item_name
		for d in frappe.get_all(
			"Item", filters={"name": ("in", list(codes))}, fields=["name", "item_name"], limit=0
		)
	}


def _user_names(names):
    names = [n for n in names if n]
    if not names:
        return {}
    return {
        u.name: u.full_name
        for u in frappe.get_all(
            "User", filters={"name": ("in", names)}, fields=["name", "full_name"]
        )
    }


# ----------------------------------------------------- T24 mutation actions
#
# Contracts (task-24-brief): validate EVERYTHING before any write (T10 style);
# each action runs as the session user through ordinary permission checks (no
# ignore_permissions) behind an explicit role gate; success returns
# {"ok": True, ...refs, "board": <full _build_board() payload>}.
# Availability/lot truth is re-derived from the same board builders the read
# API uses (§4.1 server truth) AFTER the Work Order row is locked for update
# (the finish() pattern), so racing requests serialize on the row lock and the
# second one re-derives the reservation from the first one's committed MR.

from erpnext.stock.doctype.material_request.material_request import (
    make_stock_entry as make_mr_stock_entry,
)


def _require_role(role, message):
    """role: satu nama atau tuple nama — lolos jika session user punya salah satunya."""
    roles = frappe.get_roles()
    allowed = (role,) if isinstance(role, str) else tuple(role)
    if not any(r in roles for r in allowed):
        frappe.throw(message, frappe.PermissionError)


def _retry_on_deadlock(action):
    """T36: snapshot REPEATABLE-READ tidak melihat kompetitor yang commit
    SELAMA request ini menunggu lock baris — dan MariaDB menjawab locking
    read dengan ER 1020/1213 (frappe.QueryDeadlockError). Rollback + jalankan
    ulang aksi memberi setiap guard snapshot BARU: guard dihitung ulang,
    tidak pernah dilewati. Tiga percobaan cukup untuk duel dua sesi —
    kompetitor hanya commit sekali, sehingga percobaan berikutnya pasti
    melihatnya; kegagahan terakhir tetap raise jujur."""
    @functools.wraps(action)
    def wrapper(*args, **kwargs):
        for attempt in (0, 1, 2):
            try:
                return action(*args, **kwargs)
            except frappe.QueryDeadlockError:
                if attempt == 2:
                    raise
                frappe.db.rollback()
    return wrapper


def _active_mr_now(mr_name):
    """CURRENT (locking) active-request check for one MR: submitted, not
    stopped, without a submitted Stock Entry — the same semantics as the
    snapshot-based duplicate scan, but authoritative across lock waits."""
    docstatus, status = frappe.db.get_value(
        "Material Request", mr_name, ["docstatus", "status"], for_update=True
    ) or (None, None)
    if docstatus != 1 or status == "Stopped":
        return False
    return not frappe.db.get_value(
        "Stock Entry Detail",
        {"material_request": mr_name, "docstatus": 1, "parenttype": "Stock Entry"},
        "parent",
        for_update=True,
    )


def _pool_reserved_now(item_code):
    """T36 race guard: CURRENT total of ACTIVE pool reservations for the item
    (submitted, not stopped, SE-less Material Transfers). Locking reads see
    siblings that committed while this request waited on the pool lock — the
    REPEATABLE-READ snapshot cannot. Item-wide by design: the pool IS the
    item's stock (one source setting in practice); counting a request from
    another pool warehouse only ever makes the guard stricter.
    # ponytail: item-wide sum; split by (item, warehouse) if a second pool
    # per item ever becomes real"""
    return flt(
        frappe.db.sql(
            """select coalesce(sum(mri.stock_qty), 0)
                   from `tabMaterial Request Item` mri
                   join `tabMaterial Request` mr on mr.name = mri.parent
                  where mri.item_code = %s
                    and mr.docstatus = 1
                    and mr.status != 'Stopped'
                    and mr.material_request_type = 'Material Transfer'
                    and not exists (
                        select 1 from `tabStock Entry Detail` sed
                         where sed.material_request = mr.name and sed.docstatus = 1
                    )
                  for update""",
            (item_code,),
        )[0][0]
    )


def _enforce_whole_uom(amount, stock_uom, label):
    """Whole-PCS per the QtyInput/UOM contract: integer when the stock UOM is
    marked must_be_whole_number (Pcs); free UOMs are untouched."""
    if frappe.get_cached_value("UOM", stock_uom, "must_be_whole_number") and amount != int(
        amount
    ):
        frappe.throw(_("{0} harus bilangan bulat dalam {1} (dapat {2}).").format(label, stock_uom, amount))


def _lot_for_wo(board, wo_name):
    return next((l for l in board["lots"] if l["work_order"] == wo_name), None)


def _checked_lot(wo_name):
    """Server-truth lot for one locked Work Order. Batchless reservations expand
    only to Work Orders sharing the item, instead of rebuilding the whole board."""
    wo_rows = _wo_lot_rows([wo_name])
    raw_lot = next((row for row in wo_rows if row.work_order == wo_name), None)
    if raw_lot is not None and raw_lot.unsupported:
        _throw_unsupported(raw_lot)

    requests = _requests(wo_rows, wo_names=[wo_name])
    if raw_lot is not None and raw_lot.batchless:
        requests = _requests(wo_rows, item_codes=[raw_lot.item_code])
        pool_wos = {
            r["work_order"]
            for r in requests
            if r["lane"] == LANE_REQUEST and not r["flag"]
        }
        pool_wos.add(wo_name)
        wo_rows = _wo_lot_rows(pool_wos)
        requests = _requests(wo_rows, item_codes=[raw_lot.item_code])

    lot = next((row for row in _lots(wo_rows, requests) if row.work_order == wo_name), None)
    if lot is not None and not lot.unsupported:
        return lot
    if lot is not None and lot.unsupported:
        _throw_unsupported(lot)
    frappe.throw(
        _("Work Order {0} tidak punya lot tersedia untuk diminta (belum ada Manufacture atau stok habis).").format(
            wo_name
        )
    )


def _throw_unsupported(lot):
    ses = frappe.get_all(
        "Stock Entry",
        filters={"work_order": lot.work_order, "purpose": "Manufacture", "docstatus": 1},
        pluck="name",
    )
    links = ", ".join(get_link_to_form("Stock Entry", s) for s in ses)
    frappe.throw(_("{0} ({1})").format(lot.unsupported_reason, links or "tanpa SE Manufacture"))


def _target_warehouse_or_throw(company=None):
    target = _target_warehouse()
    if not target:
        frappe.throw(
            _("Gudang tujuan serah terima belum diatur; isi 'Gudang Serah Terima' di menu Pengaturan.")
        )
    return target


# Box kg columns are decimal(18,6): 12 integer + 6 fractional digits. Decimal
# (not float) because float("999999999999.999999") == 1e12 — a float bound
# would let exactly that value through to an out-of-range db write.
MAX_BOX_KG = Decimal("999999999999.999999")


def _finite_kg(value, label, allow_blank=False):
    """T35 box kg parser: a finite, non-negative number is REQUIRED for Box 1;
    Box 2 may be blank (= 0 kg). The upper bound is the decimal(18,6) column
    capacity — a finite float beyond it (e.g. 1e308) must die HERE with an
    actionable message, never at the db write as a driver error."""
    if value is None or str(value).strip() == "":
        if allow_blank:
            return 0.0
        frappe.throw(_("{0} harus angka kg yang valid").format(label))
    try:
        amount = float(value)
    except (TypeError, ValueError):
        frappe.throw(_("{0} harus angka kg yang valid").format(label))
    if not math.isfinite(amount) or amount < 0 or Decimal(str(amount)) > MAX_BOX_KG:
        frappe.throw(
            _("{0} harus angka kg non-negatif yang valid (maksimal {1} kg).").format(
                label, MAX_BOX_KG
            )
        )
    return amount


def _expected_unit_count(wo, lot, amount):
    """Whole count of the full `amount` in the item's warehouse display UOM
    (T39 universal: the stock UOM itself at EXACT factor 1 — not a fallback —
    or any alternate display UOM with a valid conversion row), computed from
    the RAW _enrich_units fields (never qty_in_pack — its `or 1` display
    fallback would silently accept unconverted items)."""
    unit = lot.display_uom or lot.stock_uom
    if unit == lot.stock_uom:
        factor = 1.0
    else:
        factor = flt(lot.display_conversion_factor)
        if not math.isfinite(factor) or factor <= 0:
            frappe.throw(
                _("Item {0} belum memiliki konversi {1} yang valid.").format(
                    wo.production_item, unit
                )
            )
    precision = wo.precision("produced_qty") or 3
    raw = amount / factor
    expected = round(raw)
    tolerance = 0.5 * (10 ** (-precision))
    if abs(raw - expected) >= tolerance:
        frappe.throw(_("Hasil Work Order {0} tidak membentuk {1} utuh.").format(wo.name, unit))
    return int(expected)


def _whole_count(value, label):
    """A whole, non-negative count (Int): no fractions, no NaN/inf."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        frappe.throw(_("{0} harus bilangan bulat.").format(label))
    if not math.isfinite(number) or number < 0 or number != int(number):
        frappe.throw(_("{0} harus bilangan bulat non-negatif.").format(label))
    return int(number)


def _validate_box_allocation(box_1, qty_1, box_2, qty_2, box_3, qty_3, expected, unit):
    """Box 1 always positive; Box 2/3 exactly 0 kg/0 count or positive/positive;
    the count sum must equal the server-computed count of the full amount."""
    if box_1 <= 0 or qty_1 <= 0:
        frappe.throw(_("Box 1 harus diisi: berat kg dan jumlah {0} harus positif.").format(unit))
    if box_2 <= 0 and qty_2 <= 0:
        box_2 = qty_2 = 0  # Box 2 kosong sah (0 kg / 0 jumlah)
    elif box_2 <= 0 or qty_2 <= 0:
        frappe.throw(_("Box 2 harus kosong (0 kg / 0 jumlah) atau terisi keduanya."))
    if box_3 <= 0 and qty_3 <= 0:
        box_3 = qty_3 = 0  # Box 3 kosong sah (0 kg / 0 jumlah)
    elif box_3 <= 0 or qty_3 <= 0:
        frappe.throw(_("Box 3 harus kosong (0 kg / 0 jumlah) atau terisi keduanya."))
    if qty_1 + qty_2 + qty_3 != expected:
        frappe.throw(
            _("Jumlah {0} Box 1 + Box 2 + Box 3 ({1}) harus tepat {2} {0}.").format(
                unit, qty_1 + qty_2 + qty_3, expected
            )
        )
    return box_1, qty_1, box_2, qty_2, box_3, qty_3


def _insert_submitted_handover_mr(wo, amount, stock_uom, source, target, box_values, box_plan=None):
    """Shared per-WO tail of create_request / create_group_request: insert +
    submit the native Material Transfer MR (full produced qty, pool source,
    target setting, row custom_work_order) and write the Work Order summary
    atomically in the SAME transaction (read_only fields: the controlled
    db_set gate), then sync the status. box_values is the WO summary box
    block; group requests pass all zeros (no per-WO-per-box attribution) and
    carry the shared plan on the MR header instead. Returns the submitted MR."""
    mr = frappe.get_doc(
        {
            "doctype": "Material Request",
            "material_request_type": "Material Transfer",
            "company": wo.company,
            "transaction_date": now(),
            "schedule_date": add_days(now(), 1),
            "set_from_warehouse": source,
            "set_warehouse": target,
            **({"custom_handover_box_plan": box_plan} if box_plan else {}),
            "items": [
                {
                    "item_code": wo.production_item,
                    "qty": amount,
                    "uom": stock_uom,
                    "stock_uom": stock_uom,
                    "from_warehouse": source,
                    "warehouse": target,
                    "schedule_date": add_days(now(), 1),
                    "custom_work_order": wo.name,
                }
            ],
        }
    )
    mr.insert()  # session user; submit below — one transaction, native perms
    mr.submit()
    frappe.db.set_value(
        "Work Order",
        wo.name,
        {"custom_handover_material_request": mr.name, **box_values},
        update_modified=False,
    )
    _sync_handover_summary([wo.name])  # fail-honest: status Diminta Gudang
    return mr


@frappe.whitelist()
@_retry_on_deadlock
def create_request(work_order, box_1=None, box_1_qty=None, box_2=0, box_2_qty=0, box_3=0, box_3_qty=0):
    """Gudang side (Stock User / Stock Manager / Gudang Barang Jadi): submit a Material Transfer
    request for the Work Order's FULL produced qty (R3 — no qty dialog, drag is
    the direct action) carrying the validated box allocation. The source
    warehouse is the handover source setting when set, else the SE-derived lot
    warehouse (R2). One ACTIVE (unshipped, unstopped) request per Work Order;
    a duplicate throws.

    T35: ALL box/count validation happens under the WO row lock BEFORE any
    write; the native MR is inserted + submitted WITHOUT any box/postpacking
    custom field, then the Work Order summary (Link + box values) is
    written atomically and the status synchronized — one transaction, zero
    writes on any failure."""
    _require_role(
        ROLES_GUDANG,
        _("Hanya peran gudang (Stock User / Stock Manager / Gudang Barang Jadi) yang dapat membuat permintaan serah terima."),
    )
    frappe.has_permission("Material Request", "create", throw=True)

    frappe.db.get_value("Work Order", work_order, "name", for_update=True)  # row lock
    wo = frappe.get_doc("Work Order", work_order)
    # FU58: gate company (setting hanya berlaku bila cocok dengan WO konsumen);
    # target masih dibaca SEBELUM tulisan pertama (mr.insert di bawah)
    target = _target_warehouse_or_throw(wo.company)
    amount = flt(wo.produced_qty)
    if amount <= 0:
        frappe.throw(
            _("Work Order {0} belum punya hasil barang jadi (produced qty 0).").format(work_order)
        )
    lot = _checked_lot(work_order)
    if lot.batchless:
        # the pool is ITEM-wide: requests from every WO of this item draw from
        # it, so the Item row lock serializes them (the WO lock above cannot)
        frappe.db.get_value("Item", wo.production_item, "name", for_update=True)
        lot = _checked_lot(work_order)  # re-derive under the pool lock

    # R3: one active request per WO — re-derived under the lock so racing
    # creates serialize on the row lock (stopped/draft/cancelled never block)
    for r in _requests([lot], wo_names=[work_order]):
        if r["lane"] == LANE_REQUEST and not r["flag"]:
            frappe.throw(
                _("Work Order {0} sudah punya permintaan aktif ({1}).").format(work_order, r["mr"])
            )

    # T36 race guard: a competitor that committed while this request waited on
    # the WO lock is invisible to the snapshot reads above. A locking re-read
    # of the summary Link sees CURRENT data — while it owns an active request,
    # the duplicate guard holds (the retry wrapper covers the ER-1020 mood).
    link_now = frappe.db.get_value(
        "Work Order", wo.name, "custom_handover_material_request", for_update=True
    )
    if link_now and _active_mr_now(link_now):
        frappe.throw(
            _("Work Order {0} sudah punya permintaan aktif ({1}).").format(work_order, link_now)
        )

    # T35 box form: compute + validate EVERYTHING before any write
    expected_units = _expected_unit_count(wo, lot, amount)
    unit = lot.display_uom or lot.stock_uom
    kg_1 = _finite_kg(box_1, "Box 1")
    qtys_1 = _whole_count(box_1_qty, f"Box 1 ({unit})")
    kg_2 = _finite_kg(box_2, "Box 2", allow_blank=True)
    qtys_2 = _whole_count(box_2_qty, f"Box 2 ({unit})")
    kg_3 = _finite_kg(box_3, "Box 3", allow_blank=True)
    qtys_3 = _whole_count(box_3_qty, f"Box 3 ({unit})")
    kg_1, qtys_1, kg_2, qtys_2, kg_3, qtys_3 = _validate_box_allocation(
        kg_1, qtys_1, kg_2, qtys_2, kg_3, qtys_3, expected_units, unit
    )

    stock_uom = lot.stock_uom or frappe.db.get_value("Item", wo.production_item, "stock_uom")
    _enforce_whole_uom(amount, stock_uom, "Qty")
    available = flt(lot.available_qty)
    if lot.batchless:
        # T36 race guard: sibling reservations committed while this request
        # waited on the pool lock are invisible to the snapshot — the CURRENT
        # total wins (physical stock is untouched by request-only races)
        available = min(available, flt(lot.physical_qty) - _pool_reserved_now(wo.production_item))
    if amount > available:
        frappe.throw(
            _("Qty melebihi lot tersedia: diminta {0}, tersedia {1}.").format(amount, available)
        )

    source = _pool_warehouse(lot)  # setting override, else SE-derived (R2)
    mr = _insert_submitted_handover_mr(
        wo,
        amount,
        stock_uom,
        source,
        target,
        {
            "custom_box_1": kg_1,
            "custom_box_1_qty": qtys_1,
            "custom_box_2": kg_2,
            "custom_box_2_qty": qtys_2,
            "custom_box_3": kg_3,
            "custom_box_3_qty": qtys_3,
        },
    )
    return {
        "ok": True,
        "material_request": mr.name,
        "qty": amount,
        "unit": unit,
        "expected_unit_count": expected_units,
        "box_1": kg_1,
        "box_1_qty": qtys_1,
        "box_2": kg_2,
        "box_2_qty": qtys_2,
        "box_3": kg_3,
        "box_3_qty": qtys_3,
        "board": _build_board(),
    }


def _cancel_unsent_request(material_request):
    """Shared cancel body (cancel_request + cancel_group_request): lock the
    bound Work Order FIRST (no MR->WO lock inversion), re-read the MR under
    the lock, refuse abnormal docstatus / submitted-SE evidence, native-cancel
    and clear the summary through document-derived synchronization in the same
    transaction. Returns the bound Work Order name (None for unbound MRs)."""
    wo_name = frappe.db.get_value(
        "Material Request Item",
        {"parent": material_request, "custom_work_order": ("is", "set")},
        "custom_work_order",
    )
    if wo_name:
        # the summary gate reads + writes this Work Order below, and neither
        # get_doc nor db reads check permissions — verify the WO read grant
        # explicitly (same policy as the board: no read, no handover access)
        frappe.has_permission("Work Order", "read", throw=True)
        frappe.db.get_value("Work Order", wo_name, "name", for_update=True)  # lock WO first
    mr = frappe.get_doc("Material Request", material_request)  # re-read under the lock
    frappe.has_permission("Material Request", "cancel", doc=mr, throw=True)
    if mr.docstatus != 1:
        frappe.throw(_("Permintaan {0} tidak bisa dibatalkan (docstatus {1}).").format(material_request, mr.docstatus))
    sent = _sent_se_by_mr([material_request])
    if sent:
        frappe.throw(
            _("Permintaan {0} sudah terkirim ({1}); tidak bisa dibatalkan.").format(
                material_request,
                get_link_to_form("Stock Entry", sent[material_request].name),
            )
        )
    mr.cancel()  # native; permission-checked as session user
    if wo_name:
        _sync_handover_summary([wo_name])  # fail-honest: clears Link/boxes/status
    return wo_name


@frappe.whitelist()
@_retry_on_deadlock
def cancel_request(material_request):
    """Gudang side (Stock User / Stock Manager / Gudang Barang Jadi): cancel an UNSENT request
    (native cancel; audit history stays, reservation is released). The Work
    Order lock is taken FIRST (no MR->WO lock inversion), then the MR is
    re-read and its submitted-SE evidence re-checked; the summary (Link +
    boxes) clears through document-derived synchronization in the same
    transaction. W19: a member of a shared-box group may only be cancelled
    through cancel_group_request while any sibling member is still active."""
    _require_role(
        ROLES_GUDANG,
        _("Hanya peran gudang (Stock User / Stock Manager / Gudang Barang Jadi) yang dapat membatalkan permintaan serah terima."),
    )
    plan = frappe.db.get_value("Material Request", material_request, "custom_handover_box_plan")
    if plan:
        siblings = frappe.db.count(
            "Material Request",
            {
                "custom_handover_box_plan": plan,
                "docstatus": 1,
                "name": ("!=", material_request),
            },
        )
        if siblings:
            frappe.throw(_("MR ini bagian grup box ({0}); batalkan seluruh grup.").format(plan))
    _cancel_unsent_request(material_request)
    return {"ok": True, "material_request": material_request, "board": _build_board()}


def _json_list(value, label):
    """Whitelisted endpoints receive JSON strings over HTTP; direct calls may
    pass parsed lists. Everything else is rejected before any read/write."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            frappe.throw(_("{0} harus JSON array yang valid.").format(label))
    if not isinstance(value, list):
        frappe.throw(_("{0} harus JSON array yang valid.").format(label))
    return value


@frappe.whitelist()
@_retry_on_deadlock
def create_group_request(work_orders, boxes):
    """Gudang side: request a GROUP of Work Orders of the SAME item whose
    output was packed into SHARED physical boxes (e.g. 3 boxes hold output
    mixed from 10 WOs — kg is weighed per physical box once, so per-WO-per-box
    attribution does not exist). The grain stays 1 MR per Work Order (full
    produced qty each, R3); the group is expressed by one Handover Box Plan
    that every member MR links (custom_handover_box_plan), while the WO
    summary box fields stay 0.

    Contract (mirrors create_request): role gate + every validation under the
    member-WO locks BEFORE any write; hard invariant Σ(box qty) == Σ(expected
    units of every member WO) — a mismatch is a zero-write rejection; then ONE
    transaction inserts the plan and one submitted MR per WO."""
    _require_role(
        ROLES_GUDANG,
        _("Hanya peran gudang (Stock User / Stock Manager / Gudang Barang Jadi) yang dapat membuat permintaan serah terima."),
    )
    frappe.has_permission("Material Request", "create", throw=True)

    wo_names = sorted({str(n).strip() for n in _json_list(work_orders, "Work Orders") if str(n or "").strip()})
    if len(wo_names) < 2:
        frappe.throw(_("Grup serah terima butuh minimal 2 Work Order."))
    box_rows = []
    for i, box in enumerate(_json_list(boxes, "Box"), 1):
        # every row is a WEIGHED physical box: no blank allowance here
        if not isinstance(box, dict):
            frappe.throw(_("Box {0} harus objek {{kg, qty}}.").format(i))
        kg = _finite_kg(box.get("kg"), f"Box {i}")
        if kg <= 0:
            frappe.throw(_("Box {0}: berat kg harus positif.").format(i))
        qty = _whole_count(box.get("qty"), f"Box {i}")
        if qty <= 0:
            frappe.throw(_("Box {0}: jumlah harus bilangan bulat positif.").format(i))
        box_rows.append({"kg": kg, "qty": qty})
    if not box_rows:
        frappe.throw(_("Minimal 1 box wajib diisi."))

    # lock every member WO first, sorted-name order (deadlock-safe)
    wos = []
    for name in wo_names:
        frappe.db.get_value("Work Order", name, "name", for_update=True)  # row lock
        wo = frappe.get_doc("Work Order", name)
        if wo.docstatus != 1:
            frappe.throw(_("Work Order {0} tidak aktif (docstatus {1}).").format(name, wo.docstatus))
        if flt(wo.produced_qty) <= 0:
            frappe.throw(
                _("Work Order {0} belum punya hasil barang jadi (produced qty 0).").format(name)
            )
        wos.append(wo)

    items = sorted({wo.production_item for wo in wos})
    if len(items) > 1:
        frappe.throw(
            _("Semua Work Order dalam grup harus satu item (ditemukan {0} item: {1}).").format(
                len(items), ", ".join(items)
            )
        )
    target = _target_warehouse_or_throw()
    item_code = items[0]
    # T31 pattern: a batchless pool is ITEM-wide — the Item row lock serializes
    # pool siblings before the first lot derivation
    if not frappe.db.get_value("Item", item_code, "has_batch_no"):
        frappe.db.get_value("Item", item_code, "name", for_update=True)

    members = []
    expected_total = 0
    unit = None
    claimed = 0
    for wo in wos:
        lot = _checked_lot(wo.name)
        # R3 duplicate guard, authoritative form re-derived under the lock
        link_now = frappe.db.get_value(
            "Work Order", wo.name, "custom_handover_material_request", for_update=True
        )
        if link_now and _active_mr_now(link_now):
            frappe.throw(
                _("Work Order {0} sudah punya permintaan aktif ({1}).").format(wo.name, link_now)
            )
        amount = flt(wo.produced_qty)
        expected = _expected_unit_count(wo, lot, amount)
        stock_uom = lot.stock_uom or frappe.db.get_value("Item", wo.production_item, "stock_uom")
        _enforce_whole_uom(amount, stock_uom, "Qty")
        available = flt(lot.available_qty)
        if lot.batchless:
            # grup menarik N kali dalam satu transaksi; _pool_reserved_now belum
            # melihat write mana pun, jadi kumulatif klaim anggota sebelumnya
            # harus dihitung manual (invarian T36 tetap berlaku utk grup).
            available = min(
                available,
                flt(lot.physical_qty) - _pool_reserved_now(wo.production_item) - claimed,
            )
        if amount > available:
            frappe.throw(
                _("Qty melebihi lot tersedia: diminta {0}, tersedia {1}.").format(amount, available)
            )
        claimed += amount
        unit = unit or lot.display_uom or lot.stock_uom
        members.append((wo, amount, stock_uom, lot))
        expected_total += expected

    box_total = sum(r["qty"] for r in box_rows)
    if box_total != expected_total:
        frappe.throw(
            _("Jumlah {0} semua box ({1}) harus tepat {2} {0} (hasil {3} Work Order).").format(
                unit, box_total, expected_total, len(wos)
            )
        )

    # ONE transaction: the plan first (the MR links target it), then the
    # identical per-WO MR shape of create_request via the shared helper
    plan = frappe.get_doc(
        {
            "doctype": "Handover Box Plan",
            "item_code": item_code,
            "boxes": box_rows,
        }
    ).insert()  # session user (in-doctype gudang create perm)
    zero_boxes = {
        "custom_box_1": 0,
        "custom_box_1_qty": 0,
        "custom_box_2": 0,
        "custom_box_2_qty": 0,
        "custom_box_3": 0,
        "custom_box_3_qty": 0,
    }
    mr_names = []
    for wo, amount, stock_uom, lot in members:
        mr = _insert_submitted_handover_mr(
            wo, amount, stock_uom, _pool_warehouse(lot), target, zero_boxes, box_plan=plan.name
        )
        mr_names.append(mr.name)
    return {
        "ok": True,
        "box_plan": plan.name,
        "material_requests": mr_names,
        "expected_unit_count": expected_total,
        "board": _build_board(),
    }


@frappe.whitelist()
@_retry_on_deadlock
def cancel_group_request(box_plan):
    """Gudang side: cancel EVERY still-active member Material Request of a
    Handover Box Plan in one transaction (the group guard of cancel_request
    never lets the members die one by one). Members already shipped keep their
    Stock Entry evidence and are left alone; cancelled/stopped/draft members
    are skipped; the plan document remains as the audit record of what was
    packed. Returns the per-member outcome."""
    _require_role(
        ROLES_GUDANG,
        _("Hanya peran gudang (Stock User / Stock Manager / Gudang Barang Jadi) yang dapat membatalkan permintaan serah terima."),
    )
    if not box_plan or not frappe.db.exists("Handover Box Plan", box_plan):
        frappe.throw(_("Handover Box Plan {0} tidak ditemukan.").format(box_plan))
    mr_names = sorted(
        frappe.get_all(
            "Material Request", filters={"custom_handover_box_plan": box_plan}, pluck="name"
        )
    )
    wo_names = sorted(
        {
            w
            for w in frappe.get_all(
                "Material Request Item",
                filters={"parent": ("in", mr_names), "custom_work_order": ("is", "set")},
                pluck="custom_work_order",
            )
            if w
        }
    )
    frappe.has_permission("Work Order", "read", throw=True)
    # lock every member WO first, sorted-name order (same order as creation)
    for wo_name in wo_names:
        frappe.db.get_value("Work Order", wo_name, "name", for_update=True)
    cancelled, skipped = [], []
    for mr_name in mr_names:
        if not _active_mr_now(mr_name):
            skipped.append(mr_name)  # shipped / cancelled / stopped: left alone
            continue
        _cancel_unsent_request(mr_name)
        cancelled.append(mr_name)
    return {
        "ok": True,
        "box_plan": box_plan,
        "cancelled": cancelled,
        "skipped": skipped,
        "board": _build_board(),
    }


def _handover_mr(material_request):
    """Load the handover MR: read-gated, docstatus 1, one Work Order-bound row."""
    mr = frappe.get_doc("Material Request", material_request)
    frappe.has_permission("Material Request", "read", doc=mr, throw=True)
    if mr.docstatus != 1:
        frappe.throw(_("Material Request {0} tidak aktif (docstatus {1}).").format(material_request, mr.docstatus))
    if not mr.items or not mr.items[0].get("custom_work_order"):
        frappe.throw(_("Material Request {0} bukan permintaan serah terima (tanpa Work Order).").format(material_request))
    return mr


@frappe.whitelist()
def save_post_packing(material_request, box_1=None, box_2=None):
    """Compatibility only (T35): verification moved into create_request — the
    Request Gudang form owns the box allocation now. Always rejects with the
    reload message BEFORE any lock/write so an old cached client can neither
    write the retired four-lane state nor half-mutate a request."""
    frappe.throw(_(
        "Verifikasi Siap Kirim sudah dipindahkan ke form Request Gudang. "
        "Muat ulang halaman sebelum melanjutkan."
    ))


@frappe.whitelist()
@_retry_on_deadlock
def send_handover(material_request):
    """Manufacturing User/Manager: create + submit the handover Stock Entry moving the
    MR's requested qty (= the WO's produced_qty at request time, R6) from the
    source warehouse to the handover target. ONE transaction: any failure rolls
    back with zero partial documents. Batch-tracked rows keep batch_no (the
    batch seam); the shortage pre-check reads the ROUTE from-warehouse (FU29),
    so a lot whose stock lives elsewhere is refused here with a clear message —
    native submit validation stays as the backstop in the same transaction.
    T35: no postpacking gate anymore — a submitted request is sendable; the
    Work Order summary (Link + boxes) is preserved and the status synchronized
    to Terkirim after the native submit."""
    _require_role(
        (ROLE_PRODUKSI, ROLE_MANAJER_PRODUKSI),
        _("Hanya Manufacturing User atau Manufacturing Manager yang dapat mengirim serah terima."),
    )
    mr = _handover_mr(material_request)
    frappe.has_permission("Stock Entry", "create", throw=True)
    frappe.has_permission("Stock Entry", "submit", throw=True)

    wo_name = mr.items[0].custom_work_order
    qty = flt(mr.items[0].stock_qty or mr.items[0].qty)
    if qty <= 0:
        frappe.throw(_("Qty permintaan {0} tidak valid.").format(material_request))

    frappe.db.get_value("Work Order", wo_name, "name", for_update=True)  # row lock
    mr = frappe.get_doc("Material Request", material_request)  # re-read under the lock
    lot = _checked_lot(wo_name)  # §4.9 unsupported error BEFORE any mutation
    batch = lot.batch
    if mr.status == "Stopped":
        frappe.throw(_("Permintaan {0} berstatus Stopped; aktifkan kembali lewat Desk.").format(material_request))
    sent = _sent_se_by_mr([material_request])
    if sent:
        frappe.throw(
            _("Pengiriman duplikat: Stock Entry {0} sudah ada untuk {1}.").format(
                get_link_to_form("Stock Entry", sent[material_request].name), material_request
            )
        )
    # T36 race guard: an SE committed while this send waited on the WO lock is
    # invisible to the snapshot reads above; the locking re-read sees it
    # CURRENT and delivers the duplicate-send validation (the retry wrapper
    # covers the ER-1020 mood instead).
    se_now = frappe.db.get_value(
        "Stock Entry Detail",
        {"material_request": material_request, "docstatus": 1, "parenttype": "Stock Entry"},
        "parent",
        for_update=True,
    )
    if se_now:
        frappe.throw(
            _("Pengiriman duplikat: Stock Entry {0} sudah ada untuk {1}.").format(
                get_link_to_form("Stock Entry", se_now), material_request
            )
        )
    # FU29: pre-check against the ROUTE origin (the same warehouse
    # make_mr_stock_entry will use for s_warehouse) — NOT the lot's SE-derived
    # warehouse. A legacy lot whose stock sits elsewhere is refused HERE with a
    # clear Indonesian message instead of dying at native submit.
    route_wh = mr.items[0].from_warehouse or mr.set_from_warehouse or lot.warehouse
    physical = (
        _item_stock(lot.item_code, route_wh)
        if lot.batchless
        else flt(get_batch_qty(batch, route_wh) or 0)
    )
    if physical < qty:  # fail atomically here; NegativeStockError is the backstop
        frappe.throw(
            _("Stok {0} di gudang asal {1} hanya {2}; tidak bisa mengirim {3}.").format(
                lot.item_code if lot.batchless else f"batch {batch}",
                route_wh,
                physical,
                qty,
            )
        )

    se = frappe.get_doc(make_mr_stock_entry(mr.name))
    if len(se.items) != 1:
        frappe.throw(_("Material Request {0} harus satu baris item.").format(material_request))
    row = se.items[0]
    row.qty = qty
    row.transfer_qty = qty * flt(row.conversion_factor or 1)
    if not lot.batchless:
        row.use_serial_batch_fields = 1  # v16: old batch field -> bundle at submit (T21)
        row.batch_no = batch
    se.insert()
    se.submit()  # native shortage/valuation failures roll the whole request back

    _sync_handover_summary([wo_name])  # fail-honest: Terkirim, Link/boxes kept

    return {
        "ok": True,
        "material_request": material_request,
        "stock_entry": se.name,
        "batch": batch,
        "qty": qty,
        "board": _build_board(),
    }
