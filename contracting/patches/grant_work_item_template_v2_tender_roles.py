import frappe
from frappe.permissions import add_permission, update_permission_property

# External app BRD (2026-10-20) FR-16: the Tender creators had no permission
# at all on Work Item Template V2, so "Add from template" offered them an
# empty picker (in desk too). work_item_template_v2.json now grants these
# roles read/write/create/select (confirmed 2026-10-20) - this patch carries
# the same grant onto a site where the doctype's permissions have been edited
# through the Role Permissions Manager, whose Custom DocPerm rows shadow the
# JSON entirely.
DOCTYPE = "Work Item Template V2"
ROLES = (
	"Tender Engineer Civil",
	"Tender Engineer Electrical",
	"Tender Engineer Mechanical",
	"Tender Manager",
	"Planning Manager",
)
RIGHTS = ("read", "write", "create", "select", "report", "print", "export")


def execute():
	"""Same idiom as grant_tender_export_roles.py. No-op when no custom perms
	exist (site1.local): migrate syncs the JSON rows there, and calling
	add_permission() would copy the JSON perms into Custom DocPerm and
	permanently shadow the JSON on a site that never needed it.
	Idempotent: existing rows are reused, rights are only ever switched on.
	"""
	if not frappe.db.exists("Custom DocPerm", {"parent": DOCTYPE}):
		return

	for role in ROLES:
		if not frappe.db.exists("Role", role):
			continue
		if not frappe.db.exists("Custom DocPerm", {"parent": DOCTYPE, "role": role, "permlevel": 0, "if_owner": 0}):
			add_permission(DOCTYPE, role, 0, "read")
		for right in RIGHTS:
			update_permission_property(DOCTYPE, role, 0, right, 1, validate=False)

	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype

	validate_permissions_for_doctype(DOCTYPE)
	frappe.clear_cache(doctype=DOCTYPE)
