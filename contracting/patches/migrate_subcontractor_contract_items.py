import frappe


def execute():
	"""Move existing contracts' rows from items_subcontracted to contracted_items.

	The old child table (Tender Item) allocated a raw quantity against a BOQ
	line with no labor/equipment awareness. The new one (Contractor Contract
	Item) is what the reconciliation engine reads, so pre-existing contracts
	have to be carried over or they'd appear to have no scope at all.

	Written against the database directly rather than through the document
	API: the new validation would reject some of this legacy data (no
	Retention row, no attachment, suppliers not yet flagged as
	subcontractors), and the point here is to preserve what exists, not to
	retro-fit it.
	"""
	if not frappe.db.table_exists("Contractor Contract Item"):
		return

	contracts = frappe.db.sql(
		"""
		select name, type_subcontractor, workflow_state
		from `tabSubcontractor Contract`
		""",
		as_dict=True,
	)

	migrated = 0
	for contract in contracts:
		already = frappe.db.count("Contractor Contract Item", {"parent": contract.name})
		if already:
			continue

		old_rows = frappe.db.sql(
			"""
			select name, idx, tender_boq_item, qty, rate, description, uom
			from `tabTender Item`
			where parent = %s and parenttype = 'Subcontractor Contract'
			order by idx
			""",
			contract.name,
			as_dict=True,
		)

		for row in old_rows:
			if not row.tender_boq_item:
				# Nothing to reconcile against - leave it in the deprecated
				# table rather than inventing a work item for it.
				continue

			frappe.get_doc({
				"doctype": "Contractor Contract Item",
				"parent": contract.name,
				"parenttype": "Subcontractor Contract",
				"parentfield": "contracted_items",
				"idx": row.idx,
				"work_item": row.tender_boq_item,
				"description": row.description,
				"uom": row.uom,
				"qty": row.qty,
				"unit_price": row.rate,
				"amount": (row.qty or 0) * (row.rate or 0),
			}).db_insert()
			migrated += 1

		# Existing records predate the workflow, so give them its start state.
		if not contract.workflow_state:
			frappe.db.set_value(
				"Subcontractor Contract", contract.name, "workflow_state", "Draft",
				update_modified=False,
			)

	frappe.db.commit()
	print("migrate_subcontractor_contract_items: carried over {0} row(s)".format(migrated))
