import frappe

from contracting.contracting.utils.progress_invoicing import contract_item_title

# 2026-10-18 BRD FR-10: invoice rows saved before descriptions were filled
# server-side (and rows of one addendum-inserted contract line that had no
# description) show their raw row id instead of a name. Submitted invoices
# can't be re-saved, hence db.set_value. Only fills blanks.


def execute():
	filled = 0
	for doctype in ("Contractor Invoice Item", "Contractor Invoice Condition Progress"):
		for row in frappe.get_all(
			doctype, filters={"description": ["in", ["", None]]}, fields=["name", "contract_item"]
		):
			title = contract_item_title(row.contract_item)
			if title:
				frappe.db.set_value(doctype, row.name, "description", title, update_modified=False)
				filled += 1
	# The contract line itself, when it is the source of the blank.
	for line in frappe.get_all(
		"Contractor Contract Item", filters={"description": ["in", ["", None]]}, fields=["name"]
	):
		title = contract_item_title(line.name)
		if title:
			frappe.db.set_value("Contractor Contract Item", line.name, "description", title, update_modified=False)
			filled += 1
	frappe.db.commit()
	print("filled {0} blank description(s)".format(filled))
