import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import frappe
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from frappe.tests.utils import FrappeTestCase

from production_app.api import qz_signing


class TestQzSigning(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_signs_qz_hash_with_site_private_key(self):
		key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
		with TemporaryDirectory() as directory:
			base = Path(directory)
			(base / "private-key.pem").write_bytes(
				key.private_bytes(
					serialization.Encoding.PEM,
					serialization.PrivateFormat.PKCS8,
					serialization.NoEncryption(),
				)
			)
			(base / "digital-certificate.txt").write_text("public certificate", encoding="ascii")
			wo = Mock()
			wo.get.return_value = True
			with patch.object(qz_signing, "_files", return_value=(base / "digital-certificate.txt", base / "private-key.pem")), patch.object(frappe, "get_doc", return_value=wo):
				self.assertEqual(qz_signing.certificate("WO-TEST"), "public certificate")
				request = "a" * 64
				signature = base64.b64decode(qz_signing.sign("WO-TEST", request))
				key.public_key().verify(signature, request.encode(), padding.PKCS1v15(), hashes.SHA512())
				wo.check_permission.assert_called_with("read")

	def test_rejects_unconfirmed_and_invalid_hash(self):
		wo = Mock()
		wo.get.return_value = False
		with patch.object(frappe, "get_doc", return_value=wo):
			with self.assertRaises(frappe.ValidationError):
				qz_signing.certificate("WO-TEST")
		wo.get.return_value = True
		with patch.object(frappe, "get_doc", return_value=wo):
			with self.assertRaises(frappe.ValidationError):
				qz_signing.sign("WO-TEST", "not-a-qz-hash")
		wo.check_permission.side_effect = frappe.PermissionError
		with patch.object(frappe, "get_doc", return_value=wo):
			with self.assertRaises(frappe.PermissionError):
				qz_signing.certificate("WO-TEST")
