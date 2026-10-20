"""Production App is a host adapter only; Product QR owns label implementation."""

import ast
from pathlib import Path
import tomllib
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[2]


class TestLabelRetirement(TestCase):
	def test_label_api_routes_and_dependencies_removed(self):
		for name in ("production_app/api/qz_signing.py", "production_app/api/work_order_labels.py",
			"production_app/www/work_order_label.py", "production_app/www/work_order_label.html",
			"production_app/qz_signing/private-key.pem", "production_app/qz_signing/digital-certificate.txt"):
			self.assertFalse((ROOT / name).exists(), name)
		hooks = (ROOT / "production_app/hooks.py").read_text()
		self.assertNotIn("qz_signing", hooks)
		self.assertIn('"product_qr"', hooks)
		deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
		self.assertFalse(any(name.startswith(("pyqrcode", "cryptography")) for name in deps))

	def test_shared_production_actions_and_uom_helpers_remain(self):
		module = ast.parse((ROOT / "production_app/api/work_order.py").read_text())
		names = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
		self.assertNotIn("get_label_data", names)
		self.assertTrue({"prepare", "transfer_materials", "confirm_prepacking", "confirm_postpacking", "finish", "_enrich_units", "skips_prepacking"} <= names)

	def test_mapping_schema_is_no_longer_exported_by_production_app(self):
		base = ROOT / "production_app/production_app/doctype/work_order_qr_label"
		self.assertFalse((base / "work_order_qr_label.json").exists())
		self.assertFalse((base / "work_order_qr_label.py").exists())
