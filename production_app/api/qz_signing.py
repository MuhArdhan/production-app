"""QZ Tray signing for confirmed Work Order labels.

The private key lives in the site's private/qz_signing directory and is never
returned to the browser. Both endpoints use the current Frappe session.
"""

import base64
import re
from pathlib import Path

import frappe
from frappe import _
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def _files():
	base = Path(frappe.get_site_path("private", "qz_signing"))
	return base / "digital-certificate.txt", base / "private-key.pem"


def _check_work_order(name):
	if frappe.session.user == "Guest":
		frappe.throw(_("Silakan masuk untuk mencetak label."), frappe.PermissionError)
	if not name:
		frappe.throw(_("Work Order wajib dipilih untuk mencetak label."))
	wo = frappe.get_doc("Work Order", name)
	wo.check_permission("read")
	if not wo.get("custom_prepacking_confirmed"):
		frappe.throw(_("Simpan Pre-Packing sebelum mencetak label Work Order {0}.").format(name))
	return wo


@frappe.whitelist()
def certificate(work_order):
	"""Return the public X.509 certificate to QZ Tray for this label session."""
	_check_work_order(work_order)
	return _read_certificate()


def _read_certificate():
	cert_path, _key_path = _files()
	try:
		return cert_path.read_text(encoding="ascii")
	except (OSError, UnicodeError):
		frappe.throw(_("Sertifikat QZ Tray belum tersedia di server."))


@frappe.whitelist()
def download_certificate():
	"""Download the public certificate for installing QZ Tray on a workstation."""
	frappe.local.response.filename = "override.crt"
	frappe.local.response.filecontent = _read_certificate().encode("ascii")
	frappe.local.response.content_type = "application/x-x509-ca-cert"
	frappe.local.response.type = "download"


@frappe.whitelist(methods=["POST"])
def sign(work_order, request):
	"""Sign QZ Tray's hashed request with RSA/SHA512; never expose the key."""
	_check_work_order(work_order)
	# qz-tray 2.3 passes the SHA-256 digest of its call JSON to this callback.
	if not isinstance(request, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", request):
		frappe.throw(_("Permintaan tanda tangan QZ Tray tidak valid."))
	_cert_path, key_path = _files()
	try:
		private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
	except (OSError, ValueError, TypeError):
		frappe.throw(_("Private key QZ Tray belum tersedia atau tidak valid di server."))
	if not isinstance(private_key, rsa.RSAPrivateKey):
		frappe.throw(_("Private key QZ Tray harus menggunakan RSA."))
	signature = private_key.sign(request.encode("utf-8"), padding.PKCS1v15(), hashes.SHA512())
	return base64.b64encode(signature).decode("ascii")
