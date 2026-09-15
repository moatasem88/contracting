import frappe

# migrate_subcontractor_contract_items bulk-inserted Contractor Contract Item
# rows via .db_insert(), bypassing SubcontractorContract.validate_contracted_
# items() - so pre-migration rows can carry a blank description, showing
# their raw work_item docname in the grid/Link title cache instead of
# readable text until the parent document is next saved for real. Backfills
# the same way validate_contracted_items() itself derives it. Idempotent:
# re-running finds nothing left with a blank description.


def execute():
	rows = frappe.get_all(
		"Contractor Contract Item",
		filters={"description": ["in", ["", None]]},
		fields=["name", "work_item"],
	)
	for row in rows:
		if not row.work_item:
			continue
		work_item = frappe.db.get_value(
			"Tender BOQ Item", row.work_item, ["description", "item_name"], as_dict=True
		)
		if not work_item:
			continue
		description = work_item.description or work_item.item_name
		if description:
			frappe.db.set_value("Contractor Contract Item", row.name, "description", description)
