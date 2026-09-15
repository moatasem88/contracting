import erpnext
import frappe

# FR-05/NFR-03: company is new on Subcontractor Contract and defaults itself
# on every future save via set_default_company() - this only catches
# existing rows saved before that field existed, so they don't sit blank.
# Idempotent: re-running finds nothing left to backfill.


def execute():
	default_company = erpnext.get_default_company()
	if not default_company:
		return

	names = frappe.get_all(
		"Subcontractor Contract", filters={"company": ["in", ["", None]]}, pluck="name"
	)
	for name in names:
		frappe.db.set_value("Subcontractor Contract", name, "company", default_company)
