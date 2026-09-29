# FU64 — model role native: kontrak DocPerm pasca-perombakan.
#
# Latar (diagnosis 29 Sep 2026): Custom DocPerm Frappe MENGGANTIKAN DocPerm
# standar (frappe/model/meta.py set_custom_permissions — satu baris custom
# saja, seluruh perm standar doctype itu diabaikan). DOCPERM_MATRIX lama
# didesain seolah delta sehingga di situs yang apply() dari kode bersih hak
# native 4 role lenyap (Work Order efektif tinggal "Gudang Barang Jadi:
# read") — user Stock User + Stock Manager + Manufacturing User +
# Manufacturing Manager dilayani menu tapi ditolak server.
#
# Kontrak baru yang dijaga suite ini:
# - Custom DocPerm hanya di Material Request + Batch; set-nya LENGKAP
#   (cermin native disalin dari tabDocPerm + baris app) — replacement-safe;
# - Work Order / Stock Entry / Item / Warehouse / Material Request Item
#   TANPA Custom DocPerm — set native ERPNext yang berlaku (ikut track
#   upgrade ERPNext);
# - pemegang role legacy "Gudang Barang Jadi" otomatis mendapat Stock User;
# - effective perms (union antar-role) per persona native: Stock User,
#   Stock Manager, Manufacturing User, Manufacturing Manager murni, dan
#   gabungan 4 role (skenario org nyata).
# Konvensi native yang didokumentasikan (bukan bug): Manufacturing Manager
# murni TIDAK punya baca Work Order/Item/Warehouse — ERPNext mengasumsikan
# user manager juga memegang role User-nya; Stock Manager murni tidak
# membaca Work Order/Warehouse. Persona app = pasangan role (SU+SM gudang,
# MU+MM produksi) atau gabungan.

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import random_string

from production_app import upgrade

LEGACY = "Gudang Barang Jadi"
FOUR_ROLES = ("Stock User", "Stock Manager", "Manufacturing User", "Manufacturing Manager")


def _flags(doctype, role, fields):
	name = frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": role}, "name")
	if not name:
		return None
	row = frappe.db.get_value("Custom DocPerm", name, list(fields), as_dict=True)
	return {k: int(row.get(k) or 0) for k in fields}


class TestDocPermMatrix(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		upgrade.apply()  # kontrak metadata; commit-by-design (pola T22)

		def make(local, roles):
			user = frappe.get_doc({
				"doctype": "User",
				"email": f"t64.{local}.{random_string(6).lower()}@prodapp.example.com",
				"first_name": f"T64 {local}",
				"send_welcome_email": 0,
			}).insert()
			for role in roles:
				user.append("roles", {"role": role})
			user.save()
			return user.name

		cls.su = make("su", ["Stock User"])
		cls.sm = make("sm", ["Stock Manager"])
		cls.mu = make("mu", ["Manufacturing User"])
		cls.mm = make("mm", ["Manufacturing Manager"])
		cls.four = make("four", FOUR_ROLES)
		cls.legacy = make("legacy", [LEGACY])
		cls.bare = make("bare", [])

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()  # buang user yang belum ke-commit
		frappe.set_user("Administrator")
		for user in (cls.su, cls.sm, cls.mu, cls.mm, cls.four, cls.legacy, cls.bare):
			if frappe.db.exists("User", user):
				frappe.delete_doc("User", user, force=True)
		frappe.db.commit()
		super().tearDownClass()

	# ------------------------------------------------------- kontrak metadata

	def test_t64_custom_rows_exist_only_on_mr_and_batch(self):
		"""Custom DocPerm hanya di MR + Batch dengan SET role persis matrix;
		kelima doctype retired bersih; apply ×2 konvergen."""
		r1 = upgrade.apply()
		r2 = upgrade.apply()
		self.assertEqual(r2["retired_docperms"], [])
		self.assertTrue(
			all(entry.endswith(": unchanged") for entry in r2["docperm_matrix"]),
			f"docperm_matrix not idempotent: {r2['docperm_matrix']}",
		)

		for doctype in upgrade.RETIRED_DOCPERM_DOCTYPES:
			self.assertEqual(
				frappe.db.count("Custom DocPerm", {"parent": doctype}),
				0,
				f"{doctype} must carry no Custom DocPerm",
			)

		mr_expected = {"Purchase Manager", "Purchase User", "Stock Manager", "Stock User",
			"Manufacturing User", "Manufacturing Manager", LEGACY}
		self.assertEqual(
			set(frappe.get_all("Custom DocPerm", filters={"parent": "Material Request"}, pluck="role")),
			mr_expected,
		)
		batch_expected = {"Item Manager", "Manufacturing User", "Manufacturing Manager",
			"Stock User", "Stock Manager", LEGACY}
		self.assertEqual(
			set(frappe.get_all("Custom DocPerm", filters={"parent": "Batch"}, pluck="role")),
			batch_expected,
		)

	def test_t64_mirror_rows_match_standard_docperm(self):
		"""Baris cermin identik dengan DocPerm standar (drift vs upgrade
		ERPNext gagal keras di sini — apply() menyalin ulang setiap kali)."""
		for doctype, roles in upgrade.MIRROR_DOCPERM_ROLES.items():
			for role in roles:
				self.assertEqual(
					_flags(doctype, role, upgrade.MIRROR_PERM_FIELDS),
					_flags_std(doctype, role, upgrade.MIRROR_PERM_FIELDS),
					f"{doctype}/{role} mirror drift",
				)

	def test_t64_batch_matrix_rows(self):
		"""Baris app di Batch: MU read+create (WO submit membuat batch FG),
		read untuk MM/SU/SM/legacy; cermin Item Manager utuh."""
		self.assertEqual(
			_flags("Batch", "Manufacturing User", ("read", "write", "create")),
			{"read": 1, "write": 0, "create": 1},
		)
		for role in ("Manufacturing Manager", "Stock User", "Stock Manager", LEGACY):
			self.assertEqual(_flags("Batch", role, ("read", "create")), {"read": 1, "create": 0})

	# ---------------------------------------------------- effective perms

	def test_t64_effective_perms_stock_personas(self):
		"""Stock User & Stock Manager murni — hak native union matrix.
		SM tanpa Warehouse/WO read = gap native yang didokumentasikan
		(user gudang wajib membawa Stock User)."""
		self.assertTrue(frappe.has_permission("Material Request", "create", user=self.su))
		self.assertTrue(frappe.has_permission("Material Request", "cancel", user=self.su))
		self.assertTrue(frappe.has_permission("Stock Entry", "create", user=self.su))
		self.assertTrue(frappe.has_permission("Work Order", "read", user=self.su))
		self.assertFalse(frappe.has_permission("Work Order", "write", user=self.su))
		self.assertTrue(frappe.has_permission("Item", "read", user=self.su))
		self.assertTrue(frappe.has_permission("Warehouse", "read", user=self.su))
		self.assertTrue(frappe.has_permission("Batch", "read", user=self.su))
		self.assertFalse(frappe.has_permission("Batch", "create", user=self.su))

		self.assertTrue(frappe.has_permission("Material Request", "create", user=self.sm))
		self.assertTrue(frappe.has_permission("Stock Entry", "create", user=self.sm))
		self.assertTrue(frappe.has_permission("Item", "read", user=self.sm))
		self.assertTrue(frappe.has_permission("Batch", "read", user=self.sm))
		# gap native yang disengaja (didokumentasikan di docstring modul):
		self.assertFalse(frappe.has_permission("Work Order", "read", user=self.sm))
		self.assertFalse(frappe.has_permission("Warehouse", "read", user=self.sm))

	def test_t64_effective_perms_manufacturing_personas(self):
		"""Manufacturing User & Manufacturing Manager murni — MM tanpa baca
		WO/Item/Warehouse = konvensi native (manager memegang role User)."""
		for ptype in ("read", "create", "submit", "cancel"):
			self.assertTrue(frappe.has_permission("Material Request", ptype, user=self.mu))
		self.assertTrue(frappe.has_permission("Stock Entry", "create", user=self.mu))
		self.assertTrue(frappe.has_permission("Work Order", "write", user=self.mu))
		self.assertTrue(frappe.has_permission("Item", "read", user=self.mu))
		self.assertTrue(frappe.has_permission("Batch", "read", user=self.mu))
		self.assertTrue(frappe.has_permission("Batch", "create", user=self.mu))
		self.assertFalse(frappe.has_permission("Manufacturing Settings", "write", user=self.mu))

		self.assertTrue(frappe.has_permission("Material Request", "create", user=self.mm))
		self.assertTrue(frappe.has_permission("Stock Entry", "create", user=self.mm))
		self.assertTrue(frappe.has_permission("Manufacturing Settings", "write", user=self.mm))
		# konvensi native (manager = User + Manager dalam satu user):
		self.assertFalse(frappe.has_permission("Work Order", "read", user=self.mm))
		self.assertFalse(frappe.has_permission("Item", "read", user=self.mm))
		self.assertFalse(frappe.has_permission("Warehouse", "read", user=self.mm))

	def test_t64_four_role_union_is_full_access(self):
		"""4 role sekaligus = union terbesar (inilah keluhan awal: 'harusnya
		full access') — dua sisi app + pengaturan dalam satu akun."""
		self.assertTrue(frappe.has_permission("Material Request", "create", user=self.four))
		self.assertTrue(frappe.has_permission("Material Request", "cancel", user=self.four))
		self.assertTrue(frappe.has_permission("Stock Entry", "create", user=self.four))
		self.assertTrue(frappe.has_permission("Stock Entry", "submit", user=self.four))
		self.assertTrue(frappe.has_permission("Work Order", "read", user=self.four))
		self.assertTrue(frappe.has_permission("Work Order", "write", user=self.four))
		self.assertTrue(frappe.has_permission("Item", "read", user=self.four))
		self.assertTrue(frappe.has_permission("Warehouse", "read", user=self.four))
		self.assertTrue(frappe.has_permission("Batch", "read", user=self.four))
		self.assertTrue(frappe.has_permission("Batch", "create", user=self.four))
		self.assertTrue(frappe.has_permission("Manufacturing Settings", "write", user=self.four))

	def test_t64_legacy_user_gets_stock_user_and_bare_stays_out(self):
		"""Migrasi legacy: pemegang Gudang Barang Jadi otomatis dapat Stock
		User (akses data lewat native); user tanpa role tetap terkunci.
		User legacy dibuat fresh di dalam test — apply() lain boleh saja
		sudah memigrasi holder yang dibuat di setUpClass."""
		fresh = frappe.get_doc({
			"doctype": "User",
			"email": f"t64.legacy2.{random_string(6).lower()}@prodapp.example.com",
			"first_name": "T64 Legacy2",
			"send_welcome_email": 0,
		}).insert()
		fresh.append("roles", {"role": LEGACY})
		fresh.save()
		self.assertNotIn("Stock User", frappe.get_roles(fresh.name))
		granted = upgrade.ensure_legacy_gudang_stock_user()
		self.assertIn(fresh.name, [entry.split(":")[0] for entry in granted])
		self.assertIn("Stock User", frappe.get_roles(fresh.name))
		# idempoten: panggilan berikutnya tanpa efek
		self.assertEqual(upgrade.ensure_legacy_gudang_stock_user(), [])
		frappe.delete_doc("User", fresh.name, force=True)

		self.assertFalse(frappe.has_permission("Material Request", "read", user=self.bare))
		self.assertFalse(frappe.has_permission("Stock Entry", "create", user=self.bare))


def _flags_std(doctype, role, fields):
	name = frappe.db.get_value("DocPerm", {"parent": doctype, "role": role, "permlevel": 0}, "name")
	if not name:
		return None
	row = frappe.db.get_value("DocPerm", name, list(fields), as_dict=True)
	return {k: int(row.get(k) or 0) for k in fields}
