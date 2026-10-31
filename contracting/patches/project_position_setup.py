import frappe
from frappe.permissions import add_permission, update_permission_property

# Project & Cost Center Position BRD §7/§13. The report's ref_doctype is
# Cost Center (not GL Entry as the BRD first said): Frappe only opens a
# report for users with `report` on its ref_doctype, and GL Entry has no
# Custom DocPerm rows on this site - add_permission() there would copy
# erpnext's GL Entry perms into Custom DocPerm and shadow gl_entry.json for
# good. Cost Center already has Custom DocPerm rows, so nothing new is frozen.
REPORT_ROLES = ("Company CEO", "Treasury and bank", "Supervisor Treasury", "Financial Manger Mr Ahmed")
RIGHTS = ("read", "report")
SETTINGS = "Project Position Settings"


def execute():
	# Cheques Settings on this site holds one record but is issingle = 0;
	# the mandatory-Project switch must not end up in the same state.
	if not frappe.get_meta(SETTINGS).issingle:
		frappe.throw(f"{SETTINGS} must be a Single doctype")

	if not frappe.db.sql("SELECT 1 FROM `tabSingles` WHERE doctype = %s LIMIT 1", SETTINGS):
		settings = frappe.get_doc(SETTINGS)
		settings.company = settings.company or "Friends"
		settings.enforce_mandatory_project = 1
		settings.save(ignore_permissions=True)

	grant_report_roles()


def grant_report_roles():
	"""No-op when Cost Center has no Custom DocPerm (a clean site): the
	doctype JSON's perms apply there, and add_permission() would start
	shadowing them - see grant_tender_read_to_contract_roles.py."""
	if not frappe.db.exists("Custom DocPerm", {"parent": "Cost Center"}):
		return

	for role in REPORT_ROLES:
		if not frappe.db.exists("Role", role):
			continue
		exists = frappe.db.exists(
			"Custom DocPerm", {"parent": "Cost Center", "role": role, "permlevel": 0, "if_owner": 0}
		)
		if not exists:
			add_permission("Cost Center", role, 0, "read")
		for right in RIGHTS:
			update_permission_property("Cost Center", role, 0, right, 1, validate=False)

	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype

	validate_permissions_for_doctype("Cost Center")
	frappe.clear_cache(doctype="Cost Center")
