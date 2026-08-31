import frappe


def execute():
	"""Carry the legacy inline labor_item value on Contractor Contract Item
	rows over into the new, dedicated Contractor Contract Labor Item table
	(FR-19).

	allocation.py's get_labor_committed now reads only the new table, so
	any pre-existing Install Only contract with a labor_item set directly
	on its Contracted Items row would otherwise appear to have committed
	nothing at all. Only 3 rows are affected on this bench (confirmed via
	inventory before this patch was written).

	Written against the database directly, in the same style as
	migrate_subcontractor_contract_items.py: the point here is to carry
	existing data over unchanged, not to re-run today's validation against
	it.
	"""
	if not frappe.db.table_exists("Contractor Contract Labor Item"):
		return

	legacy_rows = frappe.db.sql(
		"""
		select name, parent, work_item, labor_item, resource_item, qty
		from `tabContractor Contract Item`
		where ifnull(labor_item, '') != ''
		""",
		as_dict=True,
	)

	migrated = 0
	for row in legacy_rows:
		already = frappe.db.exists(
			"Contractor Contract Labor Item",
			{"parent": row.parent, "work_item": row.work_item, "labor_item": row.labor_item},
		)
		if already:
			continue

		next_idx = frappe.db.count("Contractor Contract Labor Item", {"parent": row.parent}) + 1
		frappe.get_doc({
			"doctype": "Contractor Contract Labor Item",
			"parent": row.parent,
			"parenttype": "Subcontractor Contract",
			"parentfield": "contract_labor_items",
			"idx": next_idx,
			"work_item": row.work_item,
			"labor_item": row.labor_item,
			"resource_item": row.resource_item,
			"qty": row.qty,
		}).db_insert()
		migrated += 1

	frappe.db.commit()
	print("migrate_contractor_contract_labor_items: carried over {0} row(s)".format(migrated))
