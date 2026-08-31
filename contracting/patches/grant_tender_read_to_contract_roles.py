import frappe
from frappe.permissions import add_permission, update_permission_property

# Everyone who can open a Subcontractor Contract needs to be able to pick a
# work item on it. Since Tender BOQ Item is a child table, its permission
# derives from Tender (see contracting/contracting/api/link.py), so read on
# Tender is what actually unblocks the Work Item / Labor Item / Equipment
# Item pickers - all three are children of Tender.
ROLES = ("Contracts Manager", "cost control manager", "Company CEO")
RIGHTS = ("read", "select", "report", "export", "print")


def execute():
	"""Grant read on Tender to the roles that work Subcontractor Contracts.

	tender.json already ships these DocPerms, but this site has Custom
	DocPerm rows for Tender (added through the Role Permissions Manager),
	and Frappe uses custom perms *instead of* the doctype's own once any
	exist - so the JSON alone is inert here.

	Deliberately a no-op when no custom perms exist: add_permission() would
	call setup_custom_perms(), copying the JSON perms into Custom DocPerm
	and permanently shadowing tender.json on a site that never needed it.
	"""
	if not frappe.db.exists("Custom DocPerm", {"parent": "Tender"}):
		return

	for role in ROLES:
		if not frappe.db.exists("Role", role):
			continue

		exists = frappe.db.exists(
			"Custom DocPerm", {"parent": "Tender", "role": role, "permlevel": 0, "if_owner": 0}
		)
		if not exists:
			add_permission("Tender", role, 0, "read")

		for right in RIGHTS:
			update_permission_property("Tender", role, 0, right, 1, validate=False)

	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype

	validate_permissions_for_doctype("Tender")
	frappe.clear_cache(doctype="Tender")
