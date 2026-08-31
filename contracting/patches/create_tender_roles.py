import frappe


# The tender desk as of 2026-08: three discipline engineers building the BOQ,
# a Tender Manager over them, and the two Business Development roles that own
# the commercial side. Named here rather than created through the Role List so
# they are version-controlled - `Planning Manager`'s Tender write permission
# was hand-made in the UI and exists only in site1.local's DB, which is the
# mistake this patch exists not to repeat.
TENDER_ROLES = (
	"Tender Engineer Electrical",
	"Tender Engineer Mechanical",
	"Tender Engineer Civil",
	"Tender Manager",
	"Business Development Manager",
	"Business Development Director",
)


def execute():
	"""Create the six tender-desk roles.

	Runs pre_model_sync for the same reason as create_contracting_roles: if
	Tender's DocPerms ever reference these roles, doctype sync would fail if
	they didn't exist yet.

	Deliberately creates the roles only - it grants no permission on Tender.
	The read/write split (engineers + Tender Manager write, Business
	Development read-only) is a separate change, so that adding a role and
	granting it access to a doctype stay reviewable independently.

	Idempotent per role, so a partial run is safe to repeat.
	"""
	for role_name in TENDER_ROLES:
		if frappe.db.exists("Role", role_name):
			continue

		frappe.get_doc({
			"doctype": "Role",
			"role_name": role_name,
			"desk_access": 1,
		}).insert(ignore_permissions=True)
