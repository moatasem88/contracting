import frappe
from frappe.permissions import add_permission, update_permission_property

# Tender Manager gates the new In Progress -> Complete transition
# (mark_tender_complete), but it has zero Custom DocPerm rows on Tender
# today - it can't even open one. Grant read+write (deliberately narrower
# than Planning Manager's full create/delete, since this role only needs
# to gate that one transition, not own the whole Tender lifecycle).
ROLE = "Tender Manager"


def execute():
	"""Same idiom as grant_tender_read_to_contract_roles.py: this site has
	Custom DocPerm rows for Tender (added through the Role Permissions
	Manager), which shadow tender.json's own permissions block entirely -
	so granting only through Custom DocPerm actually takes effect here.

	Deliberately a no-op when no custom perms exist: add_permission() would
	call setup_custom_perms(), copying the JSON perms into Custom DocPerm
	and permanently shadowing tender.json on a site that never needed it.
	"""
	if not frappe.db.exists("Custom DocPerm", {"parent": "Tender"}):
		return

	if not frappe.db.exists("Role", ROLE):
		return

	exists = frappe.db.exists(
		"Custom DocPerm", {"parent": "Tender", "role": ROLE, "permlevel": 0, "if_owner": 0}
	)
	if not exists:
		add_permission("Tender", ROLE, 0, "read")

	for right in ("read", "write"):
		update_permission_property("Tender", ROLE, 0, right, 1, validate=False)

	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype

	validate_permissions_for_doctype("Tender")
	frappe.clear_cache(doctype="Tender")
