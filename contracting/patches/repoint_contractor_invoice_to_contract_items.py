import frappe


def execute():
	"""Carry existing Contractor Invoice rows over to Contracted Items.

	Contractor Invoice Item.tender_item pointed at Subcontractor Contract's
	items_subcontracted (Tender Item), which
	migrate_subcontractor_contract_items retired in favour of
	contracted_items (Contractor Contract Item). Contracts created after
	that migration have no Tender Item rows at all, so the field could not
	be filled for them - the picker had nothing to offer.

	The field is now contract_item -> Contractor Contract Item. Rows are
	resolved through the BOQ line both sides share: the old Tender Item's
	tender_boq_item is the new Contractor Contract Item's work_item, within
	the same contract.

	tender_item is deliberately left in place. It is the only record of what
	each row originally claimed against, and nothing reads it any more.
	"""
	if not frappe.db.has_column("Contractor Invoice Item", "tender_item"):
		return

	rows = frappe.db.sql(
		"""
		select item.name, item.tender_item, invoice.contractor_contract
		from `tabContractor Invoice Item` item
		inner join `tabContractor Invoice` invoice on invoice.name = item.parent
		where ifnull(item.tender_item, '') != '' and ifnull(item.contract_item, '') = ''
		""",
		as_dict=True,
	)

	unresolved = []
	for row in rows:
		work_item = frappe.db.get_value("Tender Item", row.tender_item, "tender_boq_item")
		contract_item = (
			frappe.db.get_value(
				"Contractor Contract Item",
				{
					"parent": row.contractor_contract,
					"parenttype": "Subcontractor Contract",
					"work_item": work_item,
				},
				"name",
			)
			if work_item
			else None
		)

		if not contract_item:
			# Better a row the user has to repoint by hand than a silent
			# guess at which contract line an invoice was billed against.
			unresolved.append(row)
			continue

		frappe.db.set_value(
			"Contractor Invoice Item", row.name, "contract_item", contract_item, update_modified=False
		)

	print(
		"repoint_contractor_invoice_to_contract_items: moved {0} of {1} row(s)".format(
			len(rows) - len(unresolved), len(rows)
		)
	)
	for row in unresolved:
		print(
			"  unresolved: {0} (Tender Item {1} on {2}) - set Contract Item by hand".format(
				row.name, row.tender_item, row.contractor_contract
			)
		)
