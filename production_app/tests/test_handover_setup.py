# T22 handover setup — metadata + permission matrix (upgrade.py extensions)
#
# Proves by execution on the installed runtime:
# - production_app.upgrade.apply() is idempotent (run twice, second run changes
#   nothing) and reconciles the pre-existing Manufacturing User Material Request
#   Custom DocPerm down to read/write (task-21-report §3.8 drift);
# - the full T22 permission matrix behaves per HANDOVER_PLAN.md §3:
#   "Gudang Barang Jadi" runs the MR lifecycle (create/submit/cancel) but has
#   read-only reach into Work Order / Batch / Stock Entry and no Stock Entry
#   create; "Manufacturing User" reads/writes MR (incl. allow_on_submit fields
#   on submitted docs) but cannot create/cancel/submit;
# - the 5th warehouse default (`custom_default_handover_warehouse`) persists via
#   the Pengaturan save API with write permission (Manufacturing Manager), the
#   read API stays open, and saving without permission throws.
#
# Every record created here is test-only (users t22.* prefixed, warehouses/items
# "T22 ..." prefixed); the Frappe test framework rolls each run back.

from unittest.mock import MagicMock, patch

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now, random_string

from production_app import hooks, upgrade
from production_app.api.work_order import (
	LIST_FIELDS,
	POSTPACKING_FIELD_MAP,
	PREPACKING_FIELD_MAP,
	PREP_FIELD_MAP,
	warehouse_defaults,
	warehouse_defaults_save,
)

PREFIX = "T22"
HANDOVER_ROLE = "Gudang Barang Jadi"


def _company_and_group():
	name = frappe.db.get_value("Work Order", {"docstatus": 1}, "name", order_by="creation desc")
	company = frappe.db.get_value("Work Order", name, "company")
	parent = frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")
	return company, parent


def _mr_flags(doctype, role):
	name = frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": role}, "name")
	if not name:
		return None
	row = frappe.db.get_value(
		"Custom DocPerm", name,
		["read", "write", "create", "submit", "cancel", "amend"], as_dict=True,
	)
	return {k: int(row.get(k) or 0) for k in ("read", "write", "create", "submit", "cancel", "amend")}


def _make_user(local, role=None):
	suffix = random_string(6).upper()
	user = (
		frappe.get_doc(
			{
				"doctype": "User",
				"email": f"t22.{local}.{suffix.lower()}@prodapp.example.com",
				"first_name": f"T22 {local}",
				"send_welcome_email": 0,
			}
		)
		.insert()
	)
	if role:
		user.append("roles", {"role": role})
	user.save()
	return user.name


class TestHandoverSetup(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company, cls.parent_wh = _company_and_group()
		suffix = random_string(6).upper()
		cls.src_wh = (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": f"{PREFIX} Source {suffix}",
					"company": cls.company,
					"parent_warehouse": cls.parent_wh,
					"is_group": 0,
				}
			)
			.insert()
			.name
		)
		cls.dst_wh = (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": f"{PREFIX} Handover {suffix}",
					"company": cls.company,
					"parent_warehouse": cls.parent_wh,
					"is_group": 0,
				}
			)
			.insert()
			.name
		)
		cls.item = (
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": f"{PREFIX}-ITEM-{suffix}",
					"item_name": f"{PREFIX} Item {suffix}",
					"item_group": frappe.db.get_value("Item Group", {}, "name"),
					"stock_uom": frappe.db.get_value(
						"Work Order", {"docstatus": 1}, "stock_uom", order_by="creation desc"
					),
					"is_stock_item": 1,
					"is_purchase_item": 0,
					"is_sales_item": 0,
					"is_fixed_asset": 0,
				}
			)
			.insert()
			.name
		)

	@classmethod
	def tearDownClass(cls):
		"""The apply() idempotency test commits mid-class (by design — metadata),
		which persists the class fixtures past the framework rollback. Purge the
		T22 leftovers explicitly (they carry no ledger entries)."""
		frappe.db.rollback()  # discard uncommitted per-test data (users, MRs)
		frappe.set_user("Administrator")
		for doctype, field, pattern in (
			("Warehouse", "warehouse_name", f"{PREFIX} %"),
			("Item", "item_code", f"{PREFIX}-%"),
		):
			for name in frappe.get_all(doctype, filters={field: ("like", pattern)}, pluck="name"):
				frappe.delete_doc(doctype, name, force=True)
		frappe.db.commit()
		super().tearDownClass()

	# ------------------------------------------------------------- fixtures

	def _make_mr(self, qty=10):
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Material Transfer",
				"company": self.company,
				"transaction_date": now(),
				"schedule_date": add_days(now(), 1),
				"set_from_warehouse": self.src_wh,
				"set_warehouse": self.dst_wh,
				"items": [
					{
						"item_code": self.item,
						"qty": qty,
						"uom": frappe.db.get_value("Item", self.item, "stock_uom"),
						"from_warehouse": self.src_wh,
						"warehouse": self.dst_wh,
						"schedule_date": add_days(now(), 1),
					}
				],
			}
		)
		mr.insert()
		mr.submit()
		return mr

	# ------------------------------------------------------- idempotency

	def test_cloud_fresh_install_contract_is_self_contained(self):
		required_wo = {
			fieldname
			for fieldname in (
				*LIST_FIELDS,
				*PREP_FIELD_MAP.values(),
				*PREPACKING_FIELD_MAP.values(),
				*POSTPACKING_FIELD_MAP.values(),
			)
			if fieldname.startswith("custom_")
		}
		migrated_wo = {spec["fieldname"] for spec in upgrade.WORKSPACE_FIELDS}
		self.assertEqual(required_wo - migrated_wo, set())
		required_layout = {
			"custom_item_name_information",
			"custom_column_break_fdsxk",
			"custom_detail_produksi",
			"custom_sebelum",
			"custom_column_break_khnwb",
			"custom_section_break_b0phj",
			"custom_column_break_8rt2c",
			"custom_detail_produksi_lain",
		}
		self.assertEqual(required_layout - migrated_wo, set())

		# 29 Sep: custom_default_uom_warehouse dipensiunkan dari spec (digantikan
		# custom_default_inventory_unit_of_measure milik warehouse_app, W21) —
		# yang dijamin spec ini tinggal rantai gudang.
		required_item = {
			"custom_default_source_warehouse",
			"custom_default_wip_warehouse",
			"custom_default_fg_warehouse",
		}
		migrated_item = {spec["fieldname"] for spec in upgrade.ITEM_FIELDS}
		self.assertEqual(required_item - migrated_item, set())
		self.assertNotIn("custom_default_uom_warehouse", migrated_item)
		self.assertEqual(hooks.required_apps, ["erpnext"])
		self.assertEqual(hooks.after_install, "production_app.upgrade.apply")

		upgrade.apply()
		wo_meta = frappe.get_meta("Work Order", cached=False)
		item_meta = frappe.get_meta("Item", cached=False)
		self.assertTrue(all(wo_meta.has_field(fieldname) for fieldname in required_wo))
		self.assertTrue(all(item_meta.has_field(fieldname) for fieldname in required_item))

	def test_cloud_workspace_creation_has_no_missing_parent_link(self):
		doc = MagicMock()
		doc.parent_page = ""
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe, "get_doc", return_value=doc),
		):
			upgrade.ensure_workspace()
		self.assertFalse(doc.parent_page)

	def test_t22_apply_idempotent_and_drift_reconciled(self):
		"""apply() twice: no error; the second run reports every handover step
		'unchanged' (incl. box_kg_fields); the Manufacturing User MR row is
		read/write only."""
		r1 = upgrade.apply()
		r2 = upgrade.apply()
		for key in ("box_kg_fields", "handover_mr_fields", "handover_permissions", "warehouse_default_fields"):
			entries = r2[key].values() if isinstance(r2[key], dict) else r2[key]
			self.assertTrue(
				all(entry.endswith(": unchanged") for entry in entries),
				f"{key} not idempotent: {r2[key]}",
			)
		# FO 2026-09-18 (Form Order): Manufacturing User kini menjalankan siklus
		# MR penuh (create/submit/cancel) — ruling T22 lama (read/write saja)
		# digantikan; before-state tersimpan di form-order-pre.json.
		mfg = _mr_flags("Material Request", "Manufacturing User")
		self.assertEqual(
			mfg, {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "amend": 0}
		)
		self.assertTrue(frappe.db.exists("Role", HANDOVER_ROLE))
		# 5th + 6th warehouse default fields exist on Manufacturing Settings
		self.assertTrue(
			frappe.get_meta("Manufacturing Settings").has_field("custom_default_handover_warehouse")
		)
		self.assertTrue(
			frappe.get_meta("Manufacturing Settings").has_field(
				"custom_default_handover_source_warehouse"
			)
		)
		# and the full matrix landed at the DocPerm level
		self.assertEqual(
			_mr_flags("Material Request", HANDOVER_ROLE),
			{"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "amend": 1},
		)
		self.assertEqual(_mr_flags("Material Request Item", HANDOVER_ROLE)["create"], 1)
		# FO: Manufacturing User juga create MR Item (Form Order)
		self.assertEqual(_mr_flags("Material Request Item", "Manufacturing User")["create"], 1)
		# T31 (R8): WO AND MR box fields are Float kg (allow_on_submit — written
		# at Verifikasi Siap Kirim on submitted docs)
		wo_meta = frappe.get_meta("Work Order")
		mr_meta = frappe.get_meta("Material Request")
		for meta, doctype in ((wo_meta, "Work Order"), (mr_meta, "Material Request")):
			for fieldname in ("custom_box_1", "custom_box_2"):
				df = meta.get_field(fieldname)
				self.assertEqual(df.fieldtype, "Float", f"{doctype}.{fieldname}")
				self.assertTrue(df.allow_on_submit, f"{doctype}.{fieldname}")
		# W18: Box 3 exists ONLY on the Work Order summary — the MR hidden
		# box fields stay 1/2 (pre-cutover fallback, deliberately not extended)
		wo_box3 = wo_meta.get_field("custom_box_3")
		self.assertIsNotNone(wo_box3)
		self.assertEqual(wo_box3.fieldtype, "Float")
		self.assertTrue(wo_box3.allow_on_submit)
		self.assertTrue(wo_box3.non_negative)
		self.assertIsNone(mr_meta.get_field("custom_box_3"))
		self.assertEqual(
			frappe.get_meta("Material Request").get_field("custom_box_1").allow_on_submit, 1
		)
		# T35/T39: three-lane handover metadata — Link + unit-free count fields
		wo_meta = frappe.get_meta("Work Order", cached=False)
		expected = {
			"custom_handover_material_request": ("Link", "Material Request"),
			"custom_box_1_qty": ("Int", None),
			"custom_box_2_qty": ("Int", None),
			"custom_box_3_qty": ("Int", None),
		}
		for fieldname, (fieldtype, options) in expected.items():
			df = wo_meta.get_field(fieldname)
			self.assertIsNotNone(df)
			self.assertEqual(df.fieldtype, fieldtype)
			self.assertEqual(df.options or None, options)
			self.assertTrue(df.allow_on_submit)
			self.assertTrue(df.read_only)
		# T39 rename: the unit-carrying _pack fields are retired for good
		for fieldname in upgrade.BOX_QTY_OLD_FIELDS:
			self.assertIsNone(wo_meta.get_field(fieldname))
		self.assertEqual(wo_meta.get_field("custom_box_1_qty").label, "Box 1 (Jumlah)")
		self.assertEqual(wo_meta.get_field("custom_box_2_qty").label, "Box 2 (Jumlah)")
		self.assertEqual(wo_meta.get_field("custom_box_3_qty").label, "Box 3 (Jumlah)")
		# and the rename migration itself converges on the second apply
		entries = (
			r2["box_qty_rename"].values()
			if isinstance(r2["box_qty_rename"], dict)
			else [r2["box_qty_rename"]]
		)
		self.assertTrue(
			all(str(entry).endswith(": unchanged") for entry in entries),
			f"box_qty_rename not idempotent: {r2['box_qty_rename']}",
		)

		for fieldname in ("custom_box_1", "custom_box_2", "custom_box_3"):
			self.assertTrue(wo_meta.get_field(fieldname).read_only)

		self.assertEqual(
			wo_meta.get_field("custom_handover_status").options,
			"\nDiminta Gudang\nTerkirim",
		)
		# the migration converges: the second apply reports every three-lane
		# step unchanged and never rewrites correct Links/values
		entries = (
			r2["three_lane_handover"].values()
			if isinstance(r2["three_lane_handover"], dict)
			else [r2["three_lane_handover"]]
		)
		self.assertTrue(
			all(str(entry).endswith(": unchanged") for entry in entries),
			f"three_lane_handover not idempotent: {r2['three_lane_handover']}",
		)
		# snapshot-first: the evidence must agree with its own data — the OLD
		# "Siap Kirim" option is required ONLY while the stored status values
		# prove pre-cutover data; a fresh-install capture holds the current
		# narrowed options/contract instead (it never saw the old option).
		with open(upgrade.THREE_LANE_SNAPSHOT) as f:
			snap = json.load(f)
		# T39: a pre-rename capture still keys its counts as the OLD _pack
		# fieldnames (historical evidence); a post-T39/fresh capture keys the
		# current _qty fieldnames. Both are valid point-in-time contracts.
		allowed_key_sets = (
			{
				"custom_handover_status",
				upgrade.THREE_LANE_LINK_FIELD,
				"custom_box_1", "custom_box_1_pack", "custom_box_2", "custom_box_2_pack",
			},
			{
				"custom_handover_status",
				upgrade.THREE_LANE_LINK_FIELD,
				*upgrade.THREE_LANE_BOX_FIELDS,
			},
		)
		self.assertIn(set(snap["stored_values"]), allowed_key_sets)
		status_fields = [
			cf for cf in snap["custom_fields"] if cf["fieldname"] == "custom_handover_status"
		]
		stored_status = snap["stored_values"]["custom_handover_status"]
		if upgrade.RETIRED_STATUS_VALUE in stored_status:
			# live-site pre-cutover evidence: old options present, and ONLY
			# values the old options allowed
			self.assertTrue(status_fields, "snapshot missed custom_handover_status")
			self.assertIn(upgrade.RETIRED_STATUS_VALUE, status_fields[0].get("options") or "")
			self.assertLessEqual(
				set(stored_status),
				{"Diminta Gudang", upgrade.RETIRED_STATUS_VALUE, "Terkirim"},
			)
		else:
			# fresh-install evidence: no retired value anywhere; a captured
			# definition already carries the narrowed options
			self.assertNotIn(upgrade.RETIRED_STATUS_VALUE, stored_status)
			if status_fields:
				self.assertEqual(
					status_fields[0].get("options"), "\nDiminta Gudang\nTerkirim"
				)
		for fieldname in ("custom_handover_status", "custom_box_1", "custom_box_2"):
			self.assertIsInstance(snap["stored_values"][fieldname], dict)

	# ------------------------------------------------ permission: gudang

	def test_t22_gudang_runs_mr_lifecycle_and_can_fulfill_se(self):
		"""Gudang Barang Jadi: MR create/submit/cancel allowed; Stock Entry
		create allowed (FO fulfill, 2026-09-18 — dulu ditolak); Work Order
		write denied; masters read-only visible."""
		gudang = _make_user("gudang", HANDOVER_ROLE)

		self.assertTrue(frappe.has_permission("Material Request", "create", user=gudang))
		self.assertTrue(frappe.has_permission("Material Request", "submit", user=gudang))
		self.assertTrue(frappe.has_permission("Material Request", "cancel", user=gudang))
		# child-table checks resolve via parent_doctype (frappe.permissions
		# has_child_permission); the MR Item DocPerm rows themselves are
		# asserted at DB level in the idempotency test
		self.assertTrue(
			frappe.has_permission(
				"Material Request Item", "read", parent_doctype="Material Request", user=gudang
			)
		)
		self.assertTrue(
			frappe.has_permission(
				"Material Request Item", "create", parent_doctype="Material Request", user=gudang
			)
		)
		for doctype in ("Work Order", "Batch", "Stock Entry", "Item", "Warehouse"):
			self.assertTrue(
				frappe.has_permission(doctype, "read", user=gudang), f"{doctype} read"
			)
		# FO 2026-09-18: gudang memproses Form Order (SE create/submit/cancel)
		self.assertTrue(frappe.has_permission("Stock Entry", "create", user=gudang))
		self.assertTrue(frappe.has_permission("Stock Entry", "submit", user=gudang))
		self.assertFalse(frappe.has_permission("Work Order", "write", user=gudang))

		frappe.set_user(gudang)
		try:
			mr = self._make_mr(10)  # create + submit
			self.assertEqual(mr.docstatus, 1)
			frappe.get_doc("Material Request", mr.name).cancel()  # no sends yet
			self.assertEqual(frappe.db.get_value("Material Request", mr.name, "docstatus"), 2)
		finally:
			frappe.set_user("Administrator")

	# ------------------------------------------------- permission: prod user

	def test_t22_prod_user_runs_mr_lifecycle_fo(self):
		"""Manufacturing User: MR read/write + create/submit/cancel (FO
		2026-09-18 — Form Order; dulu read/write saja); MR Item read+create."""
		prod = _make_user("prod", "Manufacturing User")
		self.assertTrue(frappe.has_permission("Material Request", "create", user=prod))
		self.assertTrue(frappe.has_permission("Material Request", "cancel", user=prod))
		self.assertTrue(frappe.has_permission("Material Request", "submit", user=prod))
		self.assertTrue(frappe.has_permission("Material Request", "read", user=prod))
		self.assertTrue(frappe.has_permission("Material Request", "write", user=prod))
		self.assertTrue(
			frappe.has_permission(
				"Material Request Item", "read", parent_doctype="Material Request", user=prod
			)
		)
		self.assertTrue(
			frappe.has_permission(
				"Material Request Item", "create", parent_doctype="Material Request", user=prod
			)
		)

		frappe.set_user(prod)
		try:
			mr = self._make_mr(5)  # FO: create + submit kini diizinkan
			self.assertEqual(mr.docstatus, 1)
			# allow_on_submit write path tetap: db_set pada field yang diizinkan
			doc = frappe.get_doc("Material Request", mr.name)
			doc.db_set("custom_good_qty_postpacking", 3.5)
			doc.db_set("custom_postpacking_confirmed", 1)
			doc.reload()
			self.assertEqual(doc.custom_good_qty_postpacking, 3.5)
			self.assertEqual(doc.custom_postpacking_confirmed, 1)
			doc.cancel()  # cancel perm kini ada (FO); owner-scope diterapkan API
			self.assertEqual(frappe.db.get_value("Material Request", mr.name, "docstatus"), 2)
		finally:
			frappe.set_user("Administrator")

	# ------------------------------------------------- permission: bare user

	def test_t22_bare_user_cannot_read_mr(self):
		"""A user with no roles has no Material Request access at all."""
		bare = _make_user("bare")
		self.assertFalse(frappe.has_permission("Material Request", "read", user=bare))
		frappe.set_user(bare)
		try:
			with self.assertRaises(frappe.PermissionError):
				self._make_mr(1)
		finally:
			frappe.set_user("Administrator")

	# ------------------------------------------- 5th warehouse default (API)

	def test_t22_handover_warehouse_setting_via_api(self):
		"""warehouse_defaults_save persists custom_default_handover_warehouse for
		a Manufacturing Settings writer; plain user save throws; read is open."""
		if not frappe.db.exists("Role", "Manufacturing Manager"):
			self.skipTest("no Manufacturing Manager role on site")
		manager = _make_user("manager", "Manufacturing Manager")

		self.assertIn("handover_warehouse", warehouse_defaults())  # read open

		frappe.set_user(manager)
		try:
			saved = warehouse_defaults_save(handover_warehouse=self.dst_wh)
			self.assertEqual(saved["handover_warehouse"], self.dst_wh)
			self.assertEqual(
				frappe.db.get_single_value("Manufacturing Settings", "custom_default_handover_warehouse"),
				self.dst_wh,
			)
		finally:
			frappe.set_user("Administrator")

		bare = _make_user("bare2")
		frappe.set_user(bare)
		try:
			with self.assertRaises(frappe.PermissionError):
				warehouse_defaults_save(handover_warehouse=self.dst_wh)
		finally:
			frappe.set_user("Administrator")
