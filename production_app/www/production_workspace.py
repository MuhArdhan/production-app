import frappe


def get_context(context):
	# internal workspace: require a logged-in session
	if frappe.session.user == "Guest":
		# FU67: fragment URL (#/wo/<id>) tidak pernah terkirim ke server, jadi
		# redirect server-side selalu membuang deep-link — setelah login user
		# mendarat di daftar, bukan di Work Order yang ditautkan. Template
		# me-render interstitial yang me-redirect dari sisi klien agar
		# redirect-to memuat hash utuh.
		context.guest_login_redirect = True
		context.no_cache = 1
		return context
	# FU48: the SPA is produksi-only — gudang-only users work in native Desk
	# (/app). Dual-role (gudang+produksi) users still get in; Administrator and
	# System Manager always pass.
	if frappe.session.user != "Administrator":
		roles = set(frappe.get_roles(frappe.session.user))
		if not roles & {"Manufacturing User", "Manufacturing Manager", "System Manager"}:
			frappe.local.flags.redirect_location = "/app"
			raise frappe.Redirect
	from frappe.sessions import get_csrf_token
	context.csrf_token = get_csrf_token()
	context.workspace_user = frappe.get_cached_value("User", frappe.session.user, "full_name") or frappe.session.user
	context.no_cache = 1
	return context
