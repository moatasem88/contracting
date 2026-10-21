import frappe

# New role for the price-ceiling/approval-chain rebuild (2026-09-27 BRD): can
# create and edit a Subcontractor Contract in Draft and trigger Submit for
# Approval, same as Contracts Manager can today. Named here rather than
# hand-created in the Role List so it is version-controlled - the same
# reasoning create_tender_roles.py's docstring gives for TENDER_ROLES.
CONTRACTS_ENGINEER_ROLE = "Contracts Engineer"


def execute():
	"""Create the Contracts Engineer role.

	Deliberately creates the role only - it grants no permission on
	Subcontractor Contract. That's set directly on the doctype JSON
	(subcontractor_contract.json's permissions block), same split
	create_tender_roles.py uses for Tender's own roles.

	Idempotent, so a partial run is safe to repeat.
	"""
	if frappe.db.exists("Role", CONTRACTS_ENGINEER_ROLE):
		return

	frappe.get_doc({
		"doctype": "Role", "role_name": CONTRACTS_ENGINEER_ROLE, "desk_access": 1,
	}).insert(ignore_permissions=True)
