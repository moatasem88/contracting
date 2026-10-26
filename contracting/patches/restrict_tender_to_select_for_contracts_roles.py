import frappe

# The contracts department (2026-10-13, Follows Tender BRD follow-up) must
# not see Tender costs or rates. Their Tender work-item pickers on
# Subcontractor Contract / Contract Addendum only need 'select' on Tender
# (contracting.contracting.api.link._permitted_parenttypes accepts select or
# read), so read/report/export/print are removed and select is kept.
ROLES = ("Contracts Manager", "Contracts Engineer")
REVOKE = ("read", "report", "export", "print", "email", "share")


def execute():
	"""Tender's permissions live in Custom DocPerm on this site (they shadow
	tender.json, which carries the same select-only shape for a fresh site),
	so this edits the Custom DocPerm rows directly. A no-op when Tender has
	no custom perms, and idempotent: rows already select-only are untouched.
	"""
	if not frappe.db.exists("Custom DocPerm", {"parent": "Tender"}):
		return

	changed = False
	for name in frappe.get_all(
		"Custom DocPerm", filters={"parent": "Tender", "role": ["in", ROLES]}, pluck="name"
	):
		perm = frappe.get_doc("Custom DocPerm", name)
		if not perm.select or any(perm.get(right) for right in REVOKE):
			perm.select = 1
			for right in REVOKE:
				perm.set(right, 0)
			perm.save(ignore_permissions=True)
			changed = True

	if changed:
		frappe.clear_cache(doctype="Tender")
		frappe.db.commit()
