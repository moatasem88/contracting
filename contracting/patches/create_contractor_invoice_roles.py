import frappe


# The 7-stage Contractor Invoice approval chain (create_contractor_invoice_
# workflow.py) needs these 5 roles; 3 (cost control manager, Accounts
# Manager, Contracts Manager) already exist. Named here rather than
# hand-created in the Role List so they are version-controlled - the same
# reasoning create_tender_roles.py's docstring gives for TENDER_ROLES.
CONTRACTOR_INVOICE_ROLES = (
	"Site Engineer",
	"Project Manager",
	"Cost Control Engineer",
	"Planning and Cost Control Director",
	"Accountant",
)


def execute():
	"""Create the 5 new Contractor Invoice workflow roles.

	Deliberately creates the roles only - it grants no permission on
	Contractor Invoice. That's set directly on the doctype JSON
	(contractor_invoice.json's permissions block), same split
	create_tender_roles.py uses for Tender's own roles.

	Idempotent per role, so a partial run is safe to repeat.
	"""
	for role_name in CONTRACTOR_INVOICE_ROLES:
		if frappe.db.exists("Role", role_name):
			continue

		frappe.get_doc({
			"doctype": "Role",
			"role_name": role_name,
			"desk_access": 1,
		}).insert(ignore_permissions=True)
