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
		stage_patch = patch.object(qz_signing, "skips_prepacking", return_value=False)
		stage_patch.start()
		self.addCleanup(stage_patch.stop)

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

	def test_file_resolution_prioritizes_site_pair(self):
		with TemporaryDirectory() as directory:
			base = Path(directory)
			site = base / "site"
			site.mkdir()
			(site / "digital-certificate.txt").touch()
			(site / "private-key.pem").touch()
			with patch.object(frappe, "get_site_path", return_value=str(site)), patch.object(frappe, "get_app_path") as app_path:
				self.assertEqual(qz_signing._files(), (site / "digital-certificate.txt", site / "private-key.pem"))
				app_path.assert_not_called()

	def test_file_resolution_falls_back_to_bundled_pair(self):
		with TemporaryDirectory() as directory:
			base = Path(directory)
			with patch.object(frappe, "get_site_path", return_value=str(base / "missing-site")), patch.object(frappe, "get_app_path", return_value=str(base / "app")) as app_path:
				self.assertEqual(qz_signing._files(), (base / "app/digital-certificate.txt", base / "app/private-key.pem"))
				app_path.assert_called_once_with("production_app", "qz_signing")

	def test_incomplete_site_pair_never_mixes_sources(self):
		for filename in ("digital-certificate.txt", "private-key.pem"):
			with self.subTest(filename=filename), TemporaryDirectory() as directory:
				base = Path(directory)
				(base / filename).touch()
				with patch.object(frappe, "get_site_path", return_value=str(base)), patch.object(frappe, "get_app_path") as app_path:
					self.assertEqual(qz_signing._files(), (base / "digital-certificate.txt", base / "private-key.pem"))
					app_path.assert_not_called()

	def test_bundled_pair_matches_and_can_sign_without_site_files(self):
		from cryptography import x509

		with TemporaryDirectory() as directory, patch.object(frappe, "get_site_path", return_value=str(Path(directory) / "missing")):
			cert_path, key_path = qz_signing._files()
			cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
			key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
			self.assertEqual(cert.public_key().public_numbers(), key.public_key().public_numbers())
			wo = Mock()
			wo.get.return_value = True
			with patch.object(frappe, "get_doc", return_value=wo):
				request = "b" * 64
				signature = base64.b64decode(qz_signing.sign("WO-TEST", request))
				cert.public_key().verify(signature, request.encode(), padding.PKCS1v15(), hashes.SHA512())
				self.assertEqual(qz_signing.certificate("WO-TEST"), cert_path.read_text(encoding="ascii"))
