import frappe


def execute():
	"""Create the Contracts Manager role used by the Subcontractor Contract
	approval chain.

	Runs pre_model_sync: Subcontractor Contract's DocPerms reference this
	role, and doctype sync would fail if it didn't exist yet.

	`cost control manager` and `Company CEO` already exist on this instance
	and are reused as-is - the BRD's "Cost Control" / "CEO" names are not
	created, to avoid near-duplicate roles.
	"""
	if frappe.db.exists("Role", "Contracts Manager"):
		return

	frappe.get_doc({
		"doctype": "Role",
		"role_name": "Contracts Manager",
		"desk_access": 1,
	}).insert(ignore_permissions=True)
