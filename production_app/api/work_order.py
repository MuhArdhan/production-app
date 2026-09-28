# Work Order workspace server API — read side (T06) and shared stage rules.
#
# Contract: IMPLEMENTATION_PLAN.md + PROJECT_STATE.md "Action contract".
# All endpoints run as the session user; permissions come from frappe.get_list
# / has_permission. The stage is ALWAYS derived from current documents here —
# the UI never saves or sends a stage.

import math

import frappe
from frappe import _
from frappe.defaults import get_user_default, set_user_default
from frappe.utils import cint, flt

DOCTYPE = "Work Order"
SUGGESTION_DEFAULT_KEY = "production_app_metadata_suggestions"
LIST_PREFERENCES_KEY = "production_app_work_order_list_preferences"
SUGGESTION_FIELDS = {
	"penimbang": "custom_nama_penimbang",
	"leader": "custom_leader_produksi",
	"jumlah_kru": "custom_jumlah_kru",
	"qc_produksi": "custom_qc_produksi",
	"qc_packing": "custom_qc_packing",
}

LIST_FIELDS = [
	"name",
	"status",
	"docstatus",
	"production_item",
	"qty",
	"produced_qty",
	"process_loss_qty",
	"material_transferred_for_manufacturing",
	"fg_warehouse",
	"wip_warehouse",
	"source_warehouse",
	"stock_uom",
	"planned_start_date",
	"planned_end_date",
	"actual_start_date",
	"actual_end_date",
	"custom_uom",
	"custom_conversion_factor",
	"custom_qty_in_uom",
	"custom_prepacking_confirmed",
	"custom_postpacking_confirmed",
	"custom_adonan_ke",
	"skip_transfer",
]

STAGE_PERSIAPAN = "persiapan"
STAGE_MATERIAL = "material"
STAGE_OPERASI = "operasi"
STAGE_PREPACKING = "pre_packing"
STAGE_POST_PACKING = "post_packing"
STAGE_FINISH = "finish"
STAGE_SELESAI = "selesai"
STAGE_CANCELLED = "cancelled"
STAGE_REVIEW = "review"

# FU46: satu-satunya role yang boleh mengoreksi Data Adonan pada WO yang sudah
# selesai (stage selesai); keputusan enforcement ada di server (prepare gate +
# flag can_edit_persiapan di wo_detail), frontend hanya mengikuti flag.
PREP_FINISHED_ROLE = "Manufacturing Manager"

# stage-filter scan cap: exact stage needs child-table truth, so stage-filtered
# lists scan at most this many permission-visible rows before slicing a page
STAGE_SCAN_LIMIT = 2500


def _overproduction_allowance():
	return flt(
		frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_work_order")
	)


def _operations_complete(operations):
	"""True when every operation is natively Completed. Native
	`update_operation_status` sets Completed only once completed_qty +
	process_loss_qty reaches the WO qty (or the overproduction allowance),
	so an operation short of plan must record its shortfall as process
	loss on its Job Card before the stage can advance."""
	return all(op.get("status") == "Completed" for op in operations)


def _has_operations(name):
	return bool(
		frappe.get_all("Work Order Operation", filters={"parent": name}, limit=1, pluck="name")
	)


def derive_stage(wo, operations=None):
	"""Derive the workspace stage from server truth. `wo` is a dict/doc of the
	Work Order; `operations` (optional) avoids refetching child rows."""
	if wo.get("docstatus") == 2:
		return STAGE_CANCELLED
	if wo.get("status") in ("Stopped", "Closed"):
		return STAGE_REVIEW
	if wo.get("docstatus") == 0:
		return STAGE_PERSIAPAN

	qty = flt(wo.get("qty"))
	if qty > 0 and flt(wo.get("produced_qty")) + flt(wo.get("process_loss_qty")) >= qty:
		return STAGE_SELESAI

	if operations is None and wo.get("name"):
		operations = frappe.get_all(
			"Work Order Operation",
			filters={"parent": wo.get("name")},
			fields=["completed_qty", "process_loss_qty", "status"],
		)

	if not wo.get("skip_transfer") and flt(wo.get("material_transferred_for_manufacturing")) < qty:
		return STAGE_MATERIAL

	if operations and not _operations_complete(operations):
		return STAGE_OPERASI

	if not wo.get("custom_prepacking_confirmed"):
		return STAGE_PREPACKING
	if not wo.get("custom_postpacking_confirmed"):
		return STAGE_POST_PACKING
	return STAGE_FINISH


def _base_filters(search=None, production_item=None, status=None, start_date=None, end_date=None):
	filters, or_filters = [], []
	if search:
		or_filters.extend(
			[
				[DOCTYPE, "name", "like", f"%{search}%"],
				[DOCTYPE, "production_item", "like", f"%{search}%"],
			]
		)
		# FU44: nama item tinggal di Item, bukan di Work Order — ikutkan agar
		# operator bisa mencari dengan nama produk, bukan hanya kode/dokumen
		item_codes = frappe.get_all(
			"Item", filters={"item_name": ("like", f"%{search}%")}, pluck="name", limit=0
		)
		if item_codes:
			or_filters.append([DOCTYPE, "production_item", "in", item_codes])
	if production_item:
		filters.append([DOCTYPE, "production_item", "=", production_item])
	if status:
		filters.append([DOCTYPE, "status", "=", status])
	if start_date:
		filters.append([DOCTYPE, "planned_start_date", ">=", start_date])
	if end_date:
		filters.append([DOCTYPE, "planned_start_date", "<=", f"{end_date} 23:59:59"])
	return filters, or_filters


def _stage_filters(stage):
	"""SQL-side narrowing per stage; exact matching happens in derive_stage."""
	filters = []
	if stage == STAGE_PERSIAPAN:
		filters.append([DOCTYPE, "docstatus", "=", 0])
	elif stage == STAGE_MATERIAL:
		filters.append([DOCTYPE, "docstatus", "=", 1])
		filters.append([DOCTYPE, "status", "not in", ["Completed", "Stopped", "Closed"]])
	elif stage in (STAGE_OPERASI, STAGE_PREPACKING, STAGE_POST_PACKING, STAGE_FINISH):
		filters.append([DOCTYPE, "docstatus", "=", 1])
		filters.append([DOCTYPE, "status", "not in", ["Completed", "Stopped", "Closed"]])
		if stage == STAGE_FINISH:
			filters.append([DOCTYPE, "custom_prepacking_confirmed", "=", 1])
			filters.append([DOCTYPE, "custom_postpacking_confirmed", "=", 1])
		elif stage == STAGE_PREPACKING:
			filters.append([DOCTYPE, "custom_prepacking_confirmed", "=", 0])
		elif stage == STAGE_POST_PACKING:
			filters.append([DOCTYPE, "custom_prepacking_confirmed", "=", 1])
			filters.append([DOCTYPE, "custom_postpacking_confirmed", "=", 0])
	elif stage == STAGE_SELESAI:
		filters.append([DOCTYPE, "docstatus", "=", 1])
		filters.append([DOCTYPE, "status", "=", "Completed"])
	elif stage == STAGE_CANCELLED:
		filters.append([DOCTYPE, "docstatus", "=", 2])
	return filters


@frappe.whitelist()
def wo_list(search=None, production_item=None, status=None, start_date=None, end_date=None, stage=None, start=0, page_len=20, meta=0):
	"""Permission-filtered, paginated Work Order list for the workspace."""
	start, page_len = max(int(start), 0), max(min(int(page_len), 2500), 1)
	filters, or_filters = _base_filters(search, production_item, status, start_date, end_date)
	if stage:
		filters.extend(_stage_filters(stage))

	def fetch(start_at, limit):
		try:
			return frappe.get_list(
				DOCTYPE,
				filters=filters,
				or_filters=or_filters or None,
				fields=LIST_FIELDS,
				order_by="planned_start_date desc, creation desc",
				start=start_at,
				page_length=limit,
			)
		except frappe.PermissionError:
			return []

	if not stage:
		rows = fetch(start, page_len)
		try:
			total = len(frappe.get_list(
				DOCTYPE,
				filters=filters,
				or_filters=or_filters or None,
				fields=["name"],
				limit_page_length=0,
			))
		except frappe.PermissionError:
			total = 0
	else:
		rows = fetch(0, STAGE_SCAN_LIMIT)
		rows = [r for r in rows if derive_stage(r) == stage]
		total = len(rows)
		rows = rows[start : start + page_len]

	_batch_enrich(rows)
	_enrich_units(rows)
	_handover_enrich(rows)
	for row in rows:
		row["stage"] = derive_stage(row)
	if int(meta):
		return {"rows": rows, "total": total, "start": start, "page_len": page_len}
	return rows


@frappe.whitelist()
def list_preferences():
	return frappe.parse_json(get_user_default(LIST_PREFERENCES_KEY) or "{}") or {}


@frappe.whitelist()
def list_preferences_save(values):
	values = frappe.parse_json(values) or {}
	set_user_default(LIST_PREFERENCES_KEY, frappe.as_json(values))
	return values


def _batch_enrich(rows):
	"""Attach display-only fields for the list: item names and has-operations."""
	if not rows:
		return
	items = {r.production_item for r in rows if r.get("production_item")}
	names = frappe._dict()
	if items:
		for d in frappe.get_all(
			"Item", filters={"name": ("in", list(items))}, fields=["name", "item_name"], limit=0
		):
			names[d.name] = d.item_name
	parents = [r.name for r in rows]
	with_ops = set(
		frappe.get_all(
			"Work Order Operation",
			filters={"parent": ("in", parents)},
			pluck="parent",
			distinct=True,
		)
	)
	for r in rows:
		r.production_item_name = names.get(r.production_item) or r.production_item
		r.has_operations = r.name in with_ops


def _handover_enrich(rows):
	"""FU20: penanda serah terima per Work Order untuk daftar/kanban/detail —
	derivasi LIVE dari dokumen (handover._handover_lanes; sumber yang sama
	dengan field native custom_handover_status). Tanpa izin baca MR/SE flag
	diam-diam kosong (halaman tetap jalan)."""
	if not rows:
		return
	# lazy import: handover sudah mengimpor modul ini di level modul
	from production_app.api.handover import _handover_lanes

	best = _handover_lanes([r["name"] for r in rows])
	for r in rows:
		r["handover"] = best.get(r["name"])



def _enrich_units(rows):
	"""Resolve the warehouse display UOM separately from stored stock quantities."""
	items = {}
	for row in rows:
		code = row.get("production_item")
		if code not in items:
			items[code] = frappe.get_cached_doc("Item", code)
		item = items[code]
		stock = item.stock_uom
		alternate = item.get("custom_default_uom_warehouse") or row.get("custom_uom") or stock
		factor = 1.0 if alternate == stock else None
		if alternate != stock:
			conversions = item.uoms
			if item.variant_of:
				conversions = list(conversions) + list(frappe.get_cached_doc("Item", item.variant_of).uoms)
			for conversion in conversions:
				if conversion.uom == alternate:
					value = flt(conversion.conversion_factor)
					factor = value if math.isfinite(value) and value > 0 else None
					break
		row["stock_uom"] = stock
		row["display_uom"] = alternate
		row["display_conversion_factor"] = factor
		row["stock_uom_whole_number"] = bool(frappe.get_cached_value("UOM", stock, "must_be_whole_number"))
		row["uom_warning"] = (
			_("Konversi {0} ke {1} belum valid di Item {2}; gunakan {1}.").format(alternate, stock, code)
			if factor is None else None
		)

@frappe.whitelist()
def wo_detail(name):
	"""Full workspace detail for one Work Order; independent permission check."""
	if not frappe.has_permission(DOCTYPE, "read", doc=name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	wo = frappe.get_doc(DOCTYPE, name)
	data = wo.as_dict()
	# the WO table has no production_item_name column on this site — always
	# resolve the operator-facing item name from the Item master
	data["production_item_name"] = (
		frappe.db.get_value("Item", wo.production_item, "item_name") or wo.production_item
	)
	_enrich_units([data])
	_handover_enrich([data])
	allowance = _overproduction_allowance()

	operations = frappe.get_all(
		"Work Order Operation",
		filters={"parent": name},
		fields=["name", "operation", "status", "completed_qty", "process_loss_qty", "pending_qty", "workstation"],
		order_by="idx",
	)

	job_cards = frappe.get_all(
		"Job Card",
		filters={"work_order": name, "docstatus": ("<", 2)},
		fields=[
			"name",
			"operation",
			"operation_id",
			"status",
			"docstatus",
			"for_quantity",
			"total_completed_qty",
			"pending_qty",
			"process_loss_qty",
			"workstation",
		],
		order_by="creation",
	)
	for card in job_cards:
		card["time_logs"] = frappe.get_all(
			"Job Card Time Log",
			filters={"parent": card.name},
			fields=["from_time", "to_time", "completed_qty"],
			order_by="idx",
		)
		employee_names = frappe.get_all(
			"Job Card Employee",
			filters={"parent": card.name},
			fields=["employee", "employee_name"],
		)
		card["employees"] = employee_names

	stock_entries = frappe.get_all(
		"Stock Entry",
		filters={"work_order": name, "docstatus": ("<", 2)},
		fields=["name", "purpose", "docstatus", "fg_completed_qty", "creation"],
		order_by="creation",
	)

	required_items = frappe.get_all(
		"Work Order Item",
		filters={"parent": name, "parenttype": DOCTYPE},
		fields=[
			"item_code", "item_name", "stock_uom",
			"required_qty", "transferred_qty", "consumed_qty", "source_warehouse",
		],
		order_by="idx",
	)

	# FU10/FU13: these fields are free text names; legacy User ids remain honest
	# text and are not rewritten or guessed into names.
	users = [d for d in (data.get("custom_nama_penimbang"), data.get("custom_qc_produksi"), data.get("custom_qc_packing")) if d]
	full = frappe._dict(
		[(u.name, u.full_name) for u in frappe.get_all(
			"User", filters={"name": ("in", users)}, fields=["name", "full_name"], limit=0
		)]
	) if users else frappe._dict()
	data["custom_nama_penimbang_full"] = full.get(data.get("custom_nama_penimbang"))
	data["custom_qc_produksi_full"] = full.get(data.get("custom_qc_produksi"))
	data["custom_qc_packing_full"] = full.get(data.get("custom_qc_packing"))

	data["required_items"] = required_items
	data["operations"] = operations
	data["job_cards"] = job_cards
	data["stock_entries"] = stock_entries
	data["stage"] = derive_stage(data, operations)
	# FU46: frontend membuka kunci panel Persiapan hanya bila server mengizinkan
	# (WO selesai hanya untuk Manufacturing Manager; stage lain sudah terkunci UI)
	data["can_edit_persiapan"] = data["stage"] != STAGE_SELESAI or (
		PREP_FINISHED_ROLE in frappe.get_roles()
	)

	data["suggestions_enabled"] = _suggestions_enabled()
	data["suggestion_sources"] = {}
	for key, fieldname in SUGGESTION_FIELDS.items():
		data[f"suggested_{key}"] = None
	if data["suggestions_enabled"]:
		previous = _previous_same_item(name, wo.production_item)
		if previous:
			for key, fieldname in SUGGESTION_FIELDS.items():
				if not data.get(fieldname) and previous["values"].get(fieldname):
					data[f"suggested_{key}"] = previous["values"][fieldname]
					data["suggestion_sources"][key] = previous["sources"][fieldname]

	data["remaining_transfer"] = max(
		flt(data["qty"]) - flt(data["material_transferred_for_manufacturing"]), 0
	)
	data["remaining_produce"] = max(
		flt(data["qty"]) - flt(data["produced_qty"]) - flt(data["process_loss_qty"]), 0
	)
	data["max_allowed_qty"] = flt(data["qty"]) * (1 + allowance / 100)
	data["allowance_percentage"] = allowance

	return data


def _suggestions_enabled():
	return cint(get_user_default(SUGGESTION_DEFAULT_KEY) or 1) == 1


def _previous_same_item(name, production_item):
	rows = frappe.get_all(
		DOCTYPE,
		filters={
			"production_item": production_item,
			"name": ("!=", name),
			"docstatus": ("<", 2),
		},
		fields=["name", "creation", *SUGGESTION_FIELDS.values()],
		order_by="creation desc",
		limit_page_length=20,
	)
	values = {}
	sources = {}
	for fieldname in SUGGESTION_FIELDS.values():
		for row in rows:
			if row.get(fieldname) not in (None, ""):
				values[fieldname] = row.get(fieldname)
				sources[fieldname] = row.name
				break
	return {"values": values, "sources": sources} if values else None


@frappe.whitelist()
def suggestion_preferences():
	return {"enabled": _suggestions_enabled()}


@frappe.whitelist()
def suggestion_preferences_save(enabled=1):
	value = 1 if cint(enabled) else 0
	set_user_default(SUGGESTION_DEFAULT_KEY, value)
	return {"enabled": bool(value)}


# --------------------------------------------------------------- T07 prepare

PREP_FIELD_MAP = {
	"adonan_ke": "custom_adonan_ke",  # Data
	"adonan": "custom_adonan",  # Int
	"jam_adonan": "custom_jam_adonan",  # Time
	"suhu_adonan": "custom_suhu_adonan",  # Float
	"penimbang": "custom_nama_penimbang",  # Data (person's name, FU10 — was Link User)
	"jumlah_kru": "custom_jumlah_kru",  # Int
	"leader": "custom_leader_produksi",  # Data (person's NAME, not a User/Int)
}


def _validate_prep_values(values):
	"""Return {fieldname: value} of validated prep inputs; raises on invalid."""
	cleaned = {}
	for key, value in values.items():
		if key not in PREP_FIELD_MAP:
			frappe.throw(_("Unknown preparation field: {0}").format(key))
		if value in (None, ""):
			continue
		fieldname = PREP_FIELD_MAP[key]
		if key in ("penimbang", "leader"):
			name = str(value).strip()
			if not name:
				continue
			cleaned[fieldname] = name  # person's name as text; never cast to number
		elif key == "adonan_ke":
			cleaned[fieldname] = str(value)
		elif key in ("adonan", "jumlah_kru"):
			cleaned[fieldname] = int(value)
			if cleaned[fieldname] < 0:
				frappe.throw(_("{0} tidak boleh negatif").format(key))
		elif key == "suhu_adonan":
			cleaned[fieldname] = float(value)
		elif key == "jam_adonan":
			frappe.utils.get_time(value)  # raises on garbage
			cleaned[fieldname] = value
	# FU11: jam kosong -> jam saat penyimpanan
	if PREP_FIELD_MAP["jam_adonan"] not in cleaned:
		cleaned[PREP_FIELD_MAP["jam_adonan"]] = frappe.utils.nowtime()
	return cleaned


# Production App warehouse defaults (Custom Fields on Manufacturing Settings).
# Applied by prepare() to Work Orders whose warehouse fields are still empty;
# they never override values already on the Work Order.
WAREHOUSE_DEFAULT_FIELDS = {
	"source_warehouse": "custom_default_source_warehouse",
	"wip_warehouse": "custom_default_wip_warehouse",
	"fg_warehouse": "custom_default_fg_warehouse",
	"scrap_warehouse": "custom_default_scrap_warehouse",
}

# T22: 5th default — MR Material Transfer serah terima target. Settings-only
# (prepare() never fills it on the Work Order), so it lives outside
# WAREHOUSE_DEFAULT_FIELDS which doubles as Work Order fieldnames.
HANDOVER_WAREHOUSE_FIELD = "custom_default_handover_warehouse"

# T31 (ruling R2): gudang asal serah terima (Cold Storage) — overrides the
# batchless pool source and the from_warehouse written by create_request.
HANDOVER_SOURCE_FIELD = "custom_default_handover_source_warehouse"

# FO 2026-09-18 (Form Order): rute default produksi minta barang dari gudang.
# Settings-only seperti dua default serah terima — tidak pernah diisi ke WO.
FORM_ORDER_SOURCE_FIELD = "custom_default_form_order_source_warehouse"
FORM_ORDER_TARGET_FIELD = "custom_default_form_order_target_warehouse"

SETTING_WAREHOUSE_FIELDS = {
	**WAREHOUSE_DEFAULT_FIELDS,
	"handover_warehouse": HANDOVER_WAREHOUSE_FIELD,
	"handover_source_warehouse": HANDOVER_SOURCE_FIELD,
	"form_order_source_warehouse": FORM_ORDER_SOURCE_FIELD,
	"form_order_target_warehouse": FORM_ORDER_TARGET_FIELD,
}


# Status yang TIDAK diperlakukan "berjalan" untuk propagasi/remediasi FU58 —
# konsisten dengan semantik native get_status dan filter stage aplikasi
# (docstatus < 2 sudah meng-exclude Cancelled; "Cancelled" dipertahankan di
# sini sebagai pertahanan ganda terhadap data legacy).
WO_TERMINAL_STATUSES = ("Completed", "Stopped", "Closed", "Cancelled")

def _ensure_settings_fields():
	"""Ensure warehouse defaults exist before reading Manufacturing Settings.
	The retired company field is cleaned up only during install or migrate.
	Sentinel = last field created by ensure_warehouse_default_fields()."""
	meta = frappe.get_meta("Manufacturing Settings")
	if meta.get_field("custom_default_form_order_target_warehouse"):
		return
	from production_app.upgrade import ensure_warehouse_default_fields
	ensure_warehouse_default_fields()


def _warehouse_defaults():
	_ensure_settings_fields()
	return {
		key: (frappe.db.get_single_value("Manufacturing Settings", fieldname) or None)
		for key, fieldname in SETTING_WAREHOUSE_FIELDS.items()
	}


@frappe.whitelist()
def warehouse_defaults():
	"""Read the Production App warehouse defaults (incl. handover). Open to any
	logged-in workspace user; writing is the permission gate."""
	return _warehouse_defaults()


@frappe.whitelist()
def warehouse_defaults_save(
	source_warehouse=None, wip_warehouse=None, fg_warehouse=None, scrap_warehouse=None,
	handover_warehouse=None, handover_source_warehouse=None,
	form_order_source_warehouse=None, form_order_target_warehouse=None,
):
	"""Save the Production App warehouse defaults (empty string clears).

	FU58 LIVE: perubahan lama->baru (keduanya non-kosong) pada 4 field Work
	Order dipropagasikan ke WO berjalan yang kolomnya masih bernilai lama,
	satu transaksi dengan simpanan pengaturan. Return flat 8 kunci plus
	kunci propagated/skipped/failed."""
	frappe.has_permission("Manufacturing Settings", "write", throw=True)
	payload = {
		"source_warehouse": source_warehouse,
		"wip_warehouse": wip_warehouse,
		"fg_warehouse": fg_warehouse,
		"scrap_warehouse": scrap_warehouse,
		"handover_warehouse": handover_warehouse,
		"handover_source_warehouse": handover_source_warehouse,
		"form_order_source_warehouse": form_order_source_warehouse,
		"form_order_target_warehouse": form_order_target_warehouse,
	}
	for key, value in payload.items():
		if value in (None, ""):
			payload[key] = None
		elif not frappe.db.exists("Warehouse", value):
			frappe.throw(_("Gudang tidak ditemukan: {0}").format(value))
	# nilai lama DIBACA sebelum singleton disimpan (dasar diff propagasi)
	old = _warehouse_defaults()
	settings = frappe.get_doc("Manufacturing Settings")
	for key, value in payload.items():
		settings.set(SETTING_WAREHOUSE_FIELDS[key], value)
	settings.save()
	# FU58 §4.1: hanya 4 field yang hidup di Work Order; field settings-only
	# (handover/form order) tidak dipropagasikan — konsumennya membaca nilai
	# saat aksi terjadi.
	changed = {
		key: (old[key], payload[key])
		for key in WAREHOUSE_DEFAULT_FIELDS
		if old[key] and payload[key] and old[key] != payload[key]
	}
	values = _warehouse_defaults()
	values.update(_propagate_warehouse_changes(changed))
	return values


def _propagate_warehouse_changes(changed):
	"""FU58 §4.2: scan kandidat db-level (frappe.db.get_all — bukan frappe.get_all
	yang terfilter permission sesi) per field yang berubah, lalu tulis per WO
	melewat _apply_warehouse_changes. Return kontrak §8."""
	propagated, skipped, failed = [], [], []
	per_wo = {}
	for field, (old_value, new_value) in changed.items():
		filters = [
			["docstatus", "<", 2],
			["status", "not in", list(WO_TERMINAL_STATUSES)],
			[field, "=", old_value],
		]
		for name in frappe.db.get_all(DOCTYPE, filters=filters, pluck="name"):
			per_wo.setdefault(name, {})[field] = (old_value, new_value)
	for name in sorted(per_wo):
		_apply_warehouse_changes_collect(
			name, per_wo[name], propagated, skipped, failed
		)
	return {"propagated": propagated, "skipped": skipped, "failed": failed}


def _wo_error_message(error):
	"""Pesan Indonesia untuk daftar failed FU58 (§4.5)."""
	if isinstance(error, frappe.PermissionError):
		return "Tidak ada izin mengubah Work Order"
	return str(error)


def _apply_warehouse_changes_collect(name, changes, written, skipped, failed):
	"""Jalankan _apply_warehouse_changes untuk satu WO dan kumpulkan hasilnya;
	kegagalan per-WO tidak membatalkan pemanggilnya (settings tetap tersimpan)."""
	try:
		outcome = _apply_warehouse_changes(name, changes)
	except Exception as e:
		frappe.log_error(f"FU58 warehouse propagation failed for {name}: {e!r}")
		failed.append({"name": name, "error": _wo_error_message(e)})
		return
	if outcome["written"]:
		written.append(outcome["written"])
	skipped.extend({"name": name, "reason": reason} for reason in outcome["skipped"])
	failed.extend({"name": name, "error": error} for error in outcome["failed"])


def _apply_warehouse_changes(wo_name, changes):
	"""FU58 §4.3: terapkan perubahan gudang ke SATU Work Order berjalan.
	Urutan sama untuk kedua jalur (draft dan submitted): lock baris ->
	get_doc fresh -> re-verifikasi kandidat -> validasi warehouse-company ->
	tulis. Dipakai propagasi (warehouse_defaults_save) dan remediasi
	(sync_warehouse_defaults_to_running_work_orders).

	`changes = {field: (old, new)}`. Return
	{"written": entry|None, "skipped": [alasan], "failed": [alasan]} — tidak
	pernah raise untuk kasus bisnis agar WO lain tetap diproses."""
	# 1. lock baris dulu (pola prepare/transfer/finish) — menutup lost-update
	#    terhadap tulisan operator yang bersamaan
	frappe.db.get_value(DOCTYPE, wo_name, "name", for_update=True)
	# 2. selalu fresh dari DB, tidak pernah salinan basi
	wo = frappe.get_doc(DOCTYPE, wo_name)
	# 3. re-verifikasi kandidat (menutup TOCTOU antara scan dan tulisan):
	#    gagal salah satu -> skipped, TANPA tulisan apa pun
	if wo.docstatus >= 2 or wo.status in WO_TERMINAL_STATUSES:
		return {
			"written": None,
			"skipped": [f"sudah tidak berjalan (docstatus {wo.docstatus}, status {wo.status})"],
			"failed": [],
		}
	for field, (old_value, _new_value) in changes.items():
		current = getattr(wo, field, None) or None
		if current != (old_value or None):
			return {
				"written": None,
				"skipped": [
					f"kolom {field} sudah bernilai {current or 'kosong'}, "
					f"bukan lagi {old_value}"
				],
				"failed": [],
			}
	applicable = {}
	field_skips, field_failures = [], []
	for field, (old_value, new_value) in changes.items():
		# 5. guard skip_transfer: validate native selalu me-reset wip_warehouse
		#    ke None pada WO skip_transfer — nilai yang ditulis hanya jadi data
		#    basi yang menyesatkan. Field ini dilewati; field lain tetap jalan.
		if field == "wip_warehouse" and wo.skip_transfer:
			field_skips.append("wip_warehouse dilewati (Work Order skip_transfer)")
			continue
		# 4. validasi prapath (pengganti validate_warehouse_belongs_to_company
		#    yang dilompati db_set): gudang milik company lain -> field itu
		#    dilewati untuk WO ini dan dicatat di failed
		wh_company = frappe.db.get_value("Warehouse", new_value, "company") if new_value else None
		if wh_company and wh_company != wo.company:
			field_failures.append(
				f"gudang {new_value} milik company {wh_company}, bukan {wo.company}"
			)
			continue
		applicable[field] = (old_value, new_value)
	if not applicable:
		return {"written": None, "skipped": field_skips, "failed": field_failures}
	rows_updated = 0
	if wo.docstatus == 0:
		# 6. draft via save() — validate native tetap jalan (set_warehouses
		#    mengisi baris kosong dari header yang baru)
		for field, (_old_value, new_value) in applicable.items():
			wo.set(field, new_value)
		if "source_warehouse" in applicable:
			old_value = applicable["source_warehouse"][0]
			for row in wo.required_items:
				if (row.source_warehouse or None) == (old_value or None):
					row.source_warehouse = applicable["source_warehouse"][1]
					rows_updated += 1
		wo.save()
	else:
		# 7. submitted via db_set (pola native transferred_qty per baris);
		#    db_set meng-update modified dan membersihkan cache dokumen
		for field, (_old_value, new_value) in applicable.items():
			if field == "source_warehouse":
				continue
			wo.db_set(field, new_value)
		if "source_warehouse" in applicable:
			old_value, new_value = applicable["source_warehouse"]
			wo.db_set("source_warehouse", new_value)
			for row in wo.required_items:
				if (row.source_warehouse or None) == (old_value or None):
					row.db_set("source_warehouse", new_value)
					rows_updated += 1
	entry = {
		"name": wo.name,
		"docstatus": wo.docstatus,
		"status": wo.status,
		"company": wo.company,
		"changes": {
			field: [old_value, new_value]
			for field, (old_value, new_value) in applicable.items()
		},
		"rows_updated": rows_updated,
	}
	return {"written": entry, "skipped": field_skips, "failed": field_failures}


@frappe.whitelist()
def sync_warehouse_defaults_to_running_work_orders(dry_run=0):
	"""FU58 §5: remediasi sekali-pakai — terapkan default SEKARANG (4 field WO,
	company-gated) ke semua WO berjalan yang kolomnya berbeda dengan default
	(termasuk kolom kosong -> diisi default). Default yang kosong TIDAK pernah
	menghapus nilai WO (§4.4: baris tanpa gudang merusak transfer berikutnya).
	dry_run=1 hanya mengembalikan rencana tanpa menulis apa pun; eksekusi kedua
	mengembalikan daftar kosong (idempoten). Gate = write Manufacturing
	Settings — satu sumber gate dengan simpan pengaturan."""
	if not frappe.has_permission("Manufacturing Settings", "write"):
		frappe.throw(
			_(
				"Hanya pemegang izin tulis Manufacturing Settings "
				"(Manufacturing Manager) yang dapat menjalankan sinkronisasi gudang Work Order."
			),
			frappe.PermissionError,
		)
	dry = bool(cint(dry_run))
	defaults = {
		key: (frappe.db.get_single_value("Manufacturing Settings", fieldname) or None)
		for key, fieldname in WAREHOUSE_DEFAULT_FIELDS.items()
	}
	filters = [
		["docstatus", "<", 2],
		["status", "not in", list(WO_TERMINAL_STATUSES)],
	]
	wos = frappe.db.get_all(
		DOCTYPE,
		filters=filters,
		fields=["name", "docstatus", "status", "company", *WAREHOUSE_DEFAULT_FIELDS],
		order_by="name",
	)
	updated, skipped, failed = [], [], []
	for row in wos:
		changes = {
			field: (getattr(row, field) or None, defaults[field])
			for field in WAREHOUSE_DEFAULT_FIELDS
			if defaults[field] and (getattr(row, field) or None) != defaults[field]
		}
		if not changes:
			continue
		if dry:
			# Preview jujur terhadap eksekusi: baris HANYA ditulis lewat jalur
			# source_warehouse (§4.3 langkah 6-7) — bila field itu tidak
			# termasuk changes, eksekusi tidak menulis baris apa pun, jadi
			# preview juga melaporkan 0 (review FU58: jangan menghitung baris
			# bersource kosong yang memang tidak akan disentuh).
			rows_updated = 0
			if "source_warehouse" in changes:
				old_source = changes["source_warehouse"][0]
				row_filters = {"parent": row.name}
				row_filters["source_warehouse"] = old_source if old_source else ("is", "not set")
				rows_updated = frappe.db.count("Work Order Item", row_filters)
			updated.append(
				{
					"name": row.name,
					"docstatus": row.docstatus,
					"status": row.status,
					"company": row.company,
					"changes": {
						field: [old_value, new_value]
						for field, (old_value, new_value) in changes.items()
					},
					"rows_updated": rows_updated,
				}
			)
			continue
		_apply_warehouse_changes_collect(
			row.name, changes, updated, skipped, failed
		)
	return {"updated": updated, "skipped": skipped, "failed": failed, "dry_run": dry}


def _fill_warehouse_defaults(wo):
	"""Fill EMPTY Work Order warehouses: Production App defaults
	(Manufacturing Settings) first, then the production item's defaults.
	Never overrides a value already on the document.
	FU61: gate company dipensiunkan — default berlaku untuk semua WO;
	default Item tetap (scoped per item)."""
	settings = _warehouse_defaults()
	item = frappe.db.get_value(
		"Item",
		wo.production_item,
		["custom_default_source_warehouse", "custom_default_wip_warehouse", "custom_default_fg_warehouse"],
		as_dict=True,
	)
	for wo_field in WAREHOUSE_DEFAULT_FIELDS:
		if getattr(wo, wo_field, None):
			continue
		value = settings.get(wo_field)
		if not value and item:
			value = item.get(f"custom_default_{wo_field}")
		if value:
			setattr(wo, wo_field, value)


@frappe.whitelist()
def prepare(name, values, submit=0):
	"""Persist preparation data; optionally submit a draft WO natively.

	Repeated calls are safe: a submitted WO is only updated via its
	allow-on-submit fields and is never resubmitted."""
	# lock the row first, then re-read the document under the lock
	frappe.has_permission(DOCTYPE, "write", doc=name, throw=True)
	frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
	wo = frappe.get_doc(DOCTYPE, name)
	if wo.docstatus == 2 or wo.status in ("Stopped", "Closed"):
		frappe.throw(_("Work Order {0} tidak bisa disiapkan").format(name))
	# FU46: WO yang sudah selesai hanya boleh dikoreksi Data Adonannya oleh
	# Manufacturing Manager — ditolak sebelum ada tulisan apa pun
	if derive_stage(wo) == STAGE_SELESAI and PREP_FINISHED_ROLE not in frappe.get_roles():
		frappe.throw(
			_("Hanya {0} yang bisa mengubah Data Adonan Work Order yang sudah selesai").format(
				PREP_FINISHED_ROLE
			),
			frappe.PermissionError,
		)

	cleaned = _validate_prep_values(frappe.parse_json(values) or {})
	if wo.docstatus == 0:
		_fill_warehouse_defaults(wo)
	for fieldname, value in cleaned.items():
		wo.set(fieldname, value)

	submitted_now = False
	if wo.docstatus == 0 and frappe.utils.cint(submit):
		wo.submit()
		submitted_now = True
	else:
		wo.save()  # submitted WOs: validate_update_after_submit enforces the field list

	return {
		"name": wo.name,
		"docstatus": wo.docstatus,
		"status": wo.status,
		"stage": derive_stage(wo),
		"submitted_now": submitted_now,
		"job_cards": frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name"),
	}


# -------------------------------------------------- T08 material transfer

@frappe.whitelist()
def transfer_materials(name):
	"""Create and submit the native Material Transfer for Manufacture for the
	REMAINING planned requirement. Existing submitted transfers are respected;
	existing DRAFT transfers block the action (never silently submitted)."""
	from erpnext.manufacturing.doctype.work_order.work_order import (
		make_stock_entry as make_wo_stock_entry,
	)

	frappe.has_permission(DOCTYPE, "write", doc=name, throw=True)
	frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
	wo = frappe.get_doc(DOCTYPE, name)
	if wo.docstatus != 1 or wo.status in ("Stopped", "Closed"):
		frappe.throw(_("Material transfer tidak tersedia untuk Work Order {0}").format(name))

	transferred_before = flt(wo.material_transferred_for_manufacturing)
	remaining = max(flt(wo.qty) - transferred_before, 0)

	draft = frappe.db.get_value(
		"Stock Entry",
		{"work_order": name, "purpose": "Material Transfer for Manufacture", "docstatus": 0},
		"name",
	)
	if draft:
		frappe.throw(
			_("Selesaikan atau batalkan draft transfer {0} terlebih dahulu").format(
				frappe.utils.get_link_to_form("Stock Entry", draft)
			)
		)

	moved = 0
	entry_name = None
	if remaining > 0:
		se = frappe.get_doc(make_wo_stock_entry(name, "Material Transfer for Manufacture", qty=remaining))
		se.insert()
		se.submit()  # native shortage/validation errors roll the whole request back
		moved = remaining
		entry_name = se.name

	wo.reload()
	return {
		"name": wo.name,
		"stock_entry": entry_name,
		"transferred_now": moved,
		"material_transferred_for_manufacturing": flt(wo.material_transferred_for_manufacturing),
		"stage": derive_stage(wo),
	}


# ------------------------------------------------- T09 Job Card actions

def _locked_job_card(name, job_card):
	"""Fetch the exact Job Card and prove it belongs to this Work Order."""
	frappe.has_permission(DOCTYPE, "write", doc=name, throw=True)
	wo = frappe.get_doc(DOCTYPE, name)
	if wo.docstatus != 1 or wo.status in ("Stopped", "Closed"):
		frappe.throw(_("Job Card actions tidak tersedia untuk Work Order {0}").format(name))

	card = frappe.get_doc("Job Card", job_card)  # raises DoesNotExistError on wrong id
	if card.work_order != name:
		frappe.throw(
			_("Job Card {0} bukan milik Work Order {1}").format(job_card, name),
			frappe.PermissionError,
		)
	return card


def _resolve_employees(employees):
	"""Normalize the employees payload; fall back to the session user's Employee."""
	parsed = frappe.parse_json(employees) if isinstance(employees, str) else employees
	if parsed:
		return [{"employee": e.get("employee") if isinstance(e, dict) else e} for e in parsed]
	if frappe.session.user not in ("Guest", "Administrator"):
		self_employee = frappe.db.get_value(
			"Employee", {"user": frappe.session.user, "status": "Active"}, "name"
		)
		if self_employee:
			return [{"employee": self_employee}]
	frappe.throw(_("Data karyawan (Employee) diperlukan untuk mulai Job Card"))


@frappe.whitelist()
def jobcard_start(name, job_card, start_time=None, employees=None):
	"""Start the native timer on exactly this Job Card."""
	card = _locked_job_card(name, job_card)
	if any(not t.to_time for t in (card.time_logs or [])):
		frappe.throw(_("Job Card {0} sudah berjalan (time log masih terbuka)").format(job_card))
	card.start_timer(start_time=start_time or frappe.utils.now(), employees=_resolve_employees(employees))
	card.reload()
	return {
		"name": card.name,
		"status": card.status,
		"time_logs": [
			{"from_time": str(t.from_time), "to_time": str(t.to_time or ""), "completed_qty": flt(t.completed_qty)}
			for t in (card.time_logs or [])
		],
	}


@frappe.whitelist()
def jobcard_complete(name, job_card, qty=None, end_time=None, auto_submit=1, process_loss_qty=None):
	"""Close this Job Card in ONE action (decision 2026-09-13): `qty` is the
	actual completed output and `process_loss_qty` (susut) records the shortfall
	against the card target, so the operation reaches native Completed status
	even below plan. qty + susut must cover the remaining card quantity."""
	card = _locked_job_card(name, job_card)
	if card.docstatus == 0 and flt(card.total_completed_qty) > 0:
		frappe.throw(
			_("Job Card {0} sudah memiliki penyelesaian sebelumnya; selesaikan lewat dokumennya").format(job_card)
		)
	qty = flt(qty) if qty is not None else 0
	loss = flt(process_loss_qty) if process_loss_qty is not None else 0
	remaining = flt(card.for_quantity) - flt(card.total_completed_qty)
	if qty > remaining:
		frappe.throw(
			_("Qty selesai ({0}) melebihi sisa qty Job Card ({1})").format(qty, remaining)
		)
	if qty + loss < remaining:
		frappe.throw(
			_("Qty selesai ({0}) + susut ({1}) belum menutup sisa qty Job Card ({2}); isi sisanya sebagai susut agar operasi selesai").format(
				qty, loss, remaining
			)
		)
	kwargs = frappe._dict(
		qty=qty,
		for_quantity=flt(card.for_quantity),  # forces the native qty-split validation
		end_time=end_time or frappe.utils.now(),
		pending_qty=0,  # one-shot closure: the remainder is produced or recorded as susut
		process_loss_qty=loss,
		auto_submit=frappe.utils.cint(auto_submit),
	)
	card.complete_job_card(**kwargs)
	card.reload()
	wo = frappe.get_doc(DOCTYPE, name)
	return {
		"name": card.name,
		"docstatus": card.docstatus,
		"status": card.status,
		"total_completed_qty": flt(card.total_completed_qty),
		"pending_qty": flt(card.pending_qty),
		"work_order_status": wo.status,
		"work_order_stage": derive_stage(wo),
	}


# ------------------------------------------- T10 prepacking confirmation

PREPACKING_FIELD_MAP = {
	"good": "custom_good_qty_prepacking",  # stock UOM (Pcs), must be > 0
	"reject": "custom_reject_qty_prepacking",
	"trial": "custom_trial_qty_prepacking",
	"sisa": "custom_sisa_qty_prepacking",
	"jam_pembekuan": "custom_jam_pembekuan",  # Time
	"qc_produksi": "custom_qc_produksi",  # Data (person's name, FU10 — was Link User)
}


def _validate_prepacking(values):
	"""Validate the ENTIRE payload before any mutation. Returns cleaned dict."""
	cleaned = {}
	for key, value in values.items():
		if key not in PREPACKING_FIELD_MAP:
			frappe.throw(_("Field pre-packing tidak dikenal: {0}").format(key))
		if value in (None, ""):
			continue
		fieldname = PREPACKING_FIELD_MAP[key]
		if key == "good":
			good = float(value)
			if good != good or good in (float("inf"), float("-inf")) or good <= 0:
				frappe.throw(_("Good Qty pre-packing harus angka lebih besar dari 0"))
			cleaned[fieldname] = good
		elif key in ("reject", "trial", "sisa"):
			amount = float(value)
			if amount != amount or amount in (float("inf"), float("-inf")) or amount < 0:
				frappe.throw(_("{0} harus angka desimal >= 0").format(key))
			cleaned[fieldname] = amount
		elif key in ("qc_produksi", "jam_pembekuan"):
			if key == "qc_produksi":
				name = str(value).strip()
				if not name:
					continue
				cleaned[fieldname] = name  # person's name as text (FU10)
			else:
				frappe.utils.get_time(value)  # raises on garbage
				cleaned[fieldname] = value
	# FU11: jam kosong -> jam saat penyimpanan
	if PREPACKING_FIELD_MAP["jam_pembekuan"] not in cleaned:
		cleaned[PREPACKING_FIELD_MAP["jam_pembekuan"]] = frappe.utils.nowtime()
	return cleaned


@frappe.whitelist()
def confirm_prepacking(name, values):
	"""Save/confirm the prepacking block. good must be finite and > 0 — the
	validation completes BEFORE anything is written. Zero reject/trial/sisa is
	valid. Postpacking fields are never touched here. FU12: re-edit is allowed
	at post_packing/finish — salah input masih bisa diperbaiki sampai Manufacture
	dijalankan (setelah itu WO Completed dan aksi ditolak); pre good baru tidak
	boleh lebih kecil dari total hasil Post-Packing yang sudah dikonfirmasi."""
	frappe.has_permission(DOCTYPE, "write", doc=name, throw=True)
	frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
	wo = frappe.get_doc(DOCTYPE, name)
	if wo.docstatus != 1 or wo.status in ("Stopped", "Closed"):
		frappe.throw(_("Pre-packing tidak tersedia untuk Work Order {0}").format(name))

	stage = derive_stage(wo)
	if stage not in (STAGE_PREPACKING, STAGE_POST_PACKING, STAGE_FINISH):
		frappe.throw(
			_("Pre-packing belum tersedia: tahap sekarang {0} (selesaikan material/operasi dulu)").format(stage)
		)

	cleaned = _validate_prepacking(frappe.parse_json(values) or {})
	if PREPACKING_FIELD_MAP["good"] not in cleaned:
		frappe.throw(_("Good Qty pre-packing wajib diisi (> 0)"))

	if wo.custom_postpacking_confirmed:
		post_total = (
			flt(wo.custom_good_qty_postpacking)
			+ flt(wo.custom_reject_qty_postpacking)
			+ flt(wo.custom_trial_qty_postpacking)
		)
		new_good = cleaned[PREPACKING_FIELD_MAP["good"]]
		if post_total > new_good:
			frappe.throw(
				_("Total hasil Post-Packing terkonfirmasi ({0}) melebihi Good Pre-Packing baru ({1}); perbaiki lewat Post-Packing").format(post_total, new_good)
			)

	for fieldname, value in cleaned.items():
		wo.set(fieldname, value)
	wo.set("custom_prepacking_confirmed", 1)
	wo.save()  # update-after-submit: native allow_on_submit enforcement

	return {
		"name": wo.name,
		"stage": derive_stage(wo),
		"good": flt(wo.custom_good_qty_prepacking),
	}


# ------------------------------------------- T27 postpacking confirmation

POSTPACKING_FIELD_MAP = {
	"good": "custom_good_qty_postpacking",
	"reject": "custom_reject_qty_postpacking",
	"trial": "custom_trial_qty_postpacking",
	"sisa": "custom_sisa_qty_postpacking",  # FU11: manual input (bukan dihitung server)
	"jam_packing": "custom_jam_packing",  # Time
	"qc_packing": "custom_qc_packing",  # Data (person's name, FU13)
}


def _validate_postpacking(values, pre_good):
	"""Validate the ENTIRE payload before any mutation. Returns cleaned dict.
	`pre_good` is the confirmed prepacking good quantity, read from the WO row
	the caller locked for update. FU11: sisa is a MANUAL input (finite >= 0),
	not computed; jam kosong default ke jam saat penyimpanan.
	FU41: total Good+Reject+Trial+Sisa (dan Good sendiri) TIDAK lagi dibatasi
	oleh Good Qty pre-packing — batas akhirnya tetap berupa overproduksi
	native ERPNext yang divalidasi `finish()`."""
	if pre_good <= 0:
		frappe.throw(_("Pre-packing harus dikonfirmasi dulu"))
	cleaned = {}
	for key, value in values.items():
		if key not in POSTPACKING_FIELD_MAP:
			frappe.throw(_("Field post-packing tidak dikenal: {0}").format(key))
		if value in (None, ""):
			continue
		fieldname = POSTPACKING_FIELD_MAP[key]
		if key == "good":
			good = float(value)
			if good != good or good in (float("inf"), float("-inf")) or good <= 0:
				frappe.throw(_("Good Qty post-packing harus angka lebih besar dari 0"))
			cleaned[fieldname] = good
		elif key in ("reject", "trial", "sisa"):
			amount = float(value)
			if amount != amount or amount in (float("inf"), float("-inf")) or amount < 0:
				frappe.throw(_("{0} harus angka desimal >= 0").format(key))
			cleaned[fieldname] = amount
		elif key == "qc_packing":
			name = str(value).strip()
			if name:
				cleaned[fieldname] = name
		elif key == "jam_packing":
			frappe.utils.get_time(value)  # raises on garbage
			cleaned[fieldname] = value

	if POSTPACKING_FIELD_MAP["good"] not in cleaned:
		frappe.throw(_("Good Qty post-packing wajib diisi (> 0)"))
	# FU11: jam kosong -> jam saat penyimpanan
	if POSTPACKING_FIELD_MAP["jam_packing"] not in cleaned:
		cleaned[POSTPACKING_FIELD_MAP["jam_packing"]] = frappe.utils.nowtime()
	return cleaned


@frappe.whitelist()
def confirm_postpacking(name, values):
	"""Save/confirm the postpacking block. good must be finite and > 0 —
	FU41: kuantitas tidak lagi dibatasi Good Qty pre-packing (batas akhir =
	overproduksi native saat finish). The validation completes BEFORE anything
	is written. FU11: sisa is a manual input (finite >= 0), jam kosong default
	ke jam saat penyimpanan. Re-edit at the finish stage is allowed; prepacking
	fields are never touched here."""
	frappe.has_permission(DOCTYPE, "write", doc=name, throw=True)
	frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
	wo = frappe.get_doc(DOCTYPE, name)
	if wo.docstatus != 1 or wo.status in ("Stopped", "Closed"):
		frappe.throw(_("Post-Packing tidak tersedia untuk Work Order {0}").format(name))

	stage = derive_stage(wo)
	if stage not in (STAGE_POST_PACKING, STAGE_FINISH):
		frappe.throw(
			_("Post-Packing belum tersedia: tahap sekarang {0}").format(stage)
		)
	if not wo.custom_prepacking_confirmed:
		frappe.throw(_("Pre-packing harus dikonfirmasi dulu"))

	cleaned = _validate_postpacking(frappe.parse_json(values) or {}, flt(wo.custom_good_qty_prepacking))

	for fieldname, value in cleaned.items():
		wo.set(fieldname, value)
	wo.set("custom_postpacking_confirmed", 1)
	wo.save()  # update-after-submit: native allow_on_submit enforcement

	return {
		"name": wo.name,
		"stage": derive_stage(wo),
		"good": flt(wo.custom_good_qty_postpacking),
		"sisa": flt(wo.custom_sisa_qty_postpacking),
	}


# ------------------------------------------------------- T11 finish action

@frappe.whitelist()
def finish(name):
	"""Submit the Manufacture for the confirmed postpacking quantity.

	Re-validates every prerequisite server-side, locks the WO row, reuses the
	proven T03/T04 transaction shape: native entry builder with qty = confirmed
	postpacking good, raw-material rows restored to the transferred (planned)
	quantities, finished-goods rows only on the real FG item, batch handling
	left to the native flow + bakery_manufacturing override. Atomic:
	insert+submit in one request, so any failure rolls back everything."""
	from erpnext.manufacturing.doctype.work_order.work_order import (
		make_stock_entry as make_wo_stock_entry,
	)
	frappe.has_permission(DOCTYPE, "write", doc=name, throw=True)
	frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
	wo = frappe.get_doc(DOCTYPE, name)
	if wo.docstatus != 1 or wo.status in ("Stopped", "Closed"):
		frappe.throw(_("Finish tidak tersedia untuk Work Order {0}").format(name))

	good = flt(wo.custom_good_qty_postpacking)
	if not wo.custom_postpacking_confirmed or good <= 0:
		frappe.throw(_("Post-Packing harus dikonfirmasi dengan Good Qty > 0 sebelum finish"))

	# native guards re-checked here for clear errors before building the entry
	allowance = flt(
		frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_work_order")
	)
	if flt(wo.produced_qty) + good > flt(wo.qty) * (1 + allowance / 100):
		frappe.throw(
			_("Good Qty {0} melebihi batas overproduksi ({1})").format(
				good, flt(wo.qty) * (1 + allowance / 100)
			)
		)

	if derive_stage(wo) != STAGE_FINISH:
		frappe.throw(_("Finish belum tersedia; selesaikan tahap sebelumnya."))

	# native guards (operations complete, duplicate entry, overproduction) throw
	# from the builder itself — surfaced to the operator as-is.
	# One-shot finish (decision 2026-09-13): the entry covers the whole remaining
	# target — `good` is produced, the shortfall becomes process loss natively
	# (header qty minus finished-item rows), which also completes a legacy
	# partially-produced WO in a single submission.
	remaining_target = max(flt(wo.qty) - flt(wo.produced_qty) - flt(wo.process_loss_qty), 0)
	se = frappe.get_doc(make_wo_stock_entry(name, "Manufacture", qty=max(good, remaining_target)))

	# workspace consumption rule (plan non-negotiable #2): raw materials follow
	# the plan, not the yield — restore RM rows to the still-unconsumed planned
	# quantity (transferred − consumed). Native backflush prorates to yield.
	# For the normal one-shot flow consumed is 0, so this equals the full
	# transferred quantity; a legacy partially-produced WO keeps its balance.
	transferred = frappe._dict()
	for r in frappe.get_all(
		"Stock Entry Detail",
		filters={
			"parent": ("in", frappe.get_all(
				"Stock Entry",
				filters={
					"work_order": name,
					"purpose": "Material Transfer for Manufacture",
					"docstatus": 1,
				},
				pluck="name",
			)),
			"docstatus": 1,
		},
		fields=["item_code", "transfer_qty"],
	):
		transferred[r.item_code] = transferred.get(r.item_code, 0.0) + flt(r.transfer_qty)
	consumed = frappe._dict()
	for r in wo.required_items:
		consumed[r.item_code] = consumed.get(r.item_code, 0.0) + flt(r.consumed_qty)

	fg_item = wo.production_item
	for row in se.items:
		if row.is_finished_item:
			if row.item_code != fg_item:
				frappe.throw(_("Baris barang jadi tidak valid: {0}").format(row.item_code))
			row.qty = good / flt(row.conversion_factor or 1)
			row.transfer_qty = good
		elif row.s_warehouse and not row.t_warehouse and row.item_code in transferred:
			target = flt(transferred[row.item_code]) - flt(consumed.get(row.item_code, 0.0))
			if target > 0:
				row.transfer_qty = target
				row.qty = row.transfer_qty / flt(row.conversion_factor or 1)

	se.insert()
	se.submit()  # StockOverProductionError / valuation failures roll the request back

	wo.reload()
	batch = None
	for row in frappe.get_doc("Stock Entry", se.name).items:
		if row.is_finished_item and row.serial_and_batch_bundle:
			entries = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": row.serial_and_batch_bundle},
				fields=["batch_no", "qty"],
			)
			if len(entries) == 1:
				batch = entries[0].batch_no

	return {
		"name": wo.name,
		"stock_entry": se.name,
		"batch": batch,
		"produced_qty": flt(wo.produced_qty),
		"process_loss_qty": flt(wo.process_loss_qty),
		"status": wo.status,
		"stage": derive_stage(wo),
	}
