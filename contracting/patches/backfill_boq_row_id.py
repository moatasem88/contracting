import frappe

RESOURCE_TABLES = ("Tender Labor Item", "Tender Equipment Item", "Tender Material Item")


def execute():
	"""Backfill Tender resource rows' boq_row_id from their work_item idx.

	work_item is an Int pointing at the BOQ row's idx, which shifts if BOQ
	rows are reordered or deleted. Contractor Contract reconciliation
	resolves through boq_row_id instead, so existing tenders need it
	populated before any contract references them.

	Deliberately writes through frappe.db rather than saving the Tender:
	Tender.on_update runs handle_won_automation(), and re-saving 26 live
	tenders here could fire project/sales-order side effects.
	"""
	boq_rows = frappe.db.sql(
		"""
		select parent, idx, name
		from `tabTender BOQ Item`
		""",
		as_dict=True,
	)

	# {tender: {idx: boq_row_name}}
	by_tender = {}
	for row in boq_rows:
		by_tender.setdefault(row.parent, {})[row.idx] = row.name

	updated = 0
	for doctype in RESOURCE_TABLES:
		rows = frappe.db.sql(
			"""
			select name, parent, work_item
			from `tab{0}`
			where ifnull(boq_row_id, '') = ''
			""".format(doctype),
			as_dict=True,
		)

		for row in rows:
			boq_row_id = by_tender.get(row.parent, {}).get(row.work_item)
			if not boq_row_id:
				# Resource row points at an idx with no matching BOQ row -
				# already-broken data. Leave it null so the availability
				# engine falls back to the idx lookup and surfaces it,
				# rather than inventing a link here.
				continue

			frappe.db.set_value(doctype, row.name, "boq_row_id", boq_row_id, update_modified=False)
			updated += 1

	frappe.db.commit()
	print("backfill_boq_row_id: populated {0} resource rows".format(updated))
