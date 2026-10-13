import frappe
from frappe.permissions import add_permission, update_permission_property

# Tender export (BRD 2026-10-13): these roles are on TENDER_EXPORT_ROLES but
# have no Tender Custom DocPerm row, so they could not open a Tender at all.
# BD / Cost Control roles only need to see Tenders; Tender Engineers create
# and edit them (confirmed 2026-10-13), so they get read+write+create.
# export/print are deliberately not granted - the export goes through
# export_tender_xlsx, which does its own role check.
GRANTS = {
	"Business Development Manager": ("read",),
	"Business Development Director": ("read",),
	"Cost Control Engineer": ("read",),
	"Planning and Cost Control Director": ("read",),
	"Tender Engineer Civil": ("read", "write", "create"),
	"Tender Engineer Electrical": ("read", "write", "create"),
	"Tender Engineer Mechanical": ("read", "write", "create"),
}


def execute():
	"""Same idiom as grant_tender_manager_write_access.py: Tender has Custom
	DocPerm rows on this site, which shadow tender.json entirely, so the
	grant has to go through them.

	No-op when no custom perms exist: add_permission() would call
	setup_custom_perms(), copying the JSON perms into Custom DocPerm and
	permanently shadowing tender.json on a site that never needed it.
	Idempotent: existing rows are reused, rights are only ever switched on.
	"""
	if not frappe.db.exists("Custom DocPerm", {"parent": "Tender"}):
		return

	for role, rights in GRANTS.items():
		if not frappe.db.exists("Role", role):
			continue

		exists = frappe.db.exists(
			"Custom DocPerm", {"parent": "Tender", "role": role, "permlevel": 0, "if_owner": 0}
		)
		if not exists:
			add_permission("Tender", role, 0, "read")
			# add_permission() leaves export on for a fresh row; only clear it on
			# rows created here, never on a pre-existing one.
			update_permission_property("Tender", role, 0, "export", 0, validate=False)

		for right in rights:
			update_permission_property("Tender", role, 0, right, 1, validate=False)

	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype

	validate_permissions_for_doctype("Tender")
	frappe.clear_cache(doctype="Tender")
