import frappe


def execute():
	"""Backfill Project Tenders (FR-01, extended 2026-08-30) for contracts
	created before tender_id was deprecated in favour of it.

	tender_id was a single Link, always equal to one of the contract's
	Project's Tenders - so for every pre-existing contract that still only
	has tender_id set, a single Subcontractor Contract Tender row carrying
	that same value is a faithful, lossless backfill. Newer/future saves
	populate the full multi-tender list themselves via
	SubcontractorContract.set_project_tenders().

	Written against the database directly, in the same style as
	migrate_subcontractor_contract_items.py - the point is to carry
	existing data over unchanged, not to re-run today's validation
	against it.
	"""
	if not frappe.db.table_exists("Subcontractor Contract Tender"):
		return

	contracts = frappe.db.sql(
		"""
		select name, tender_id
		from `tabSubcontractor Contract`
		where ifnull(tender_id, '') != ''
		""",
		as_dict=True,
	)

	migrated = 0
	for contract in contracts:
		already = frappe.db.exists(
			"Subcontractor Contract Tender", {"parent": contract.name, "tender": contract.tender_id}
		)
		if already:
			continue

		frappe.get_doc({
			"doctype": "Subcontractor Contract Tender",
			"parent": contract.name,
			"parenttype": "Subcontractor Contract",
			"parentfield": "project_tenders",
			"idx": frappe.db.count("Subcontractor Contract Tender", {"parent": contract.name}) + 1,
			"tender": contract.tender_id,
		}).db_insert()
		migrated += 1

	frappe.db.commit()
	print("migrate_subcontractor_contract_project_tenders: carried over {0} row(s)".format(migrated))
