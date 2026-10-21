import frappe
from frappe.permissions import add_permission, update_permission_property

# 2026-10-18: Tender staff create their own Labor / Equipment / Subcontract
# Items. Role permissions can't scope that by Item Group, so the roles get
# plain Item read/write/create here and controllers/item.py narrows it to
# the three resource groups. Item Group read is needed for the Item form's
# own Item Group picker. No delete, export or import.
GRANTS = {
	"Item": ("read", "write", "create"),
	"Item Group": ("read",),
}
ROLES = (
	"Tender Manager",
	"Tender Engineer Civil",
	"Tender Engineer Electrical",
	"Tender Engineer Mechanical",
)


def execute():
	"""Same idiom as grant_tender_export_roles.py: Item and Item Group have
	Custom DocPerm rows on this site, which shadow their JSON permissions,
	so the grant goes through them. A doctype without custom perms is
	skipped - add_permission() would otherwise copy the JSON perms into
	Custom DocPerm and permanently shadow them on a site that never needed
	it. Idempotent: existing rows are reused, rights are only switched on.
	"""
	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype

	for doctype, rights in GRANTS.items():
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype}):
			continue

		for role in ROLES:
			if not frappe.db.exists("Role", role):
				continue

			exists = frappe.db.exists(
				"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0}
			)
			if not exists:
				add_permission(doctype, role, 0, "read")
				# add_permission() leaves export on for a fresh row; only clear
				# it on rows created here, never on a pre-existing one.
				update_permission_property(doctype, role, 0, "export", 0, validate=False)

			for right in rights:
				update_permission_property(doctype, role, 0, right, 1, validate=False)

		validate_permissions_for_doctype(doctype)
		frappe.clear_cache(doctype=doctype)
