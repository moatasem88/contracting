import frappe

RESOURCE_TABLES = ("Tender Labor Item", "Tender Equipment Item", "Tender Material Item")


def execute():
	"""Backfill Tender.subtotal/total_vat/total_additions/grand_total.

	These were only ever computed client-side (tender.js
	recalculate_group_totals), so stored values on existing Tenders reflect
	whatever the browser last computed - which drifts from a fresh
	server-side recompute whenever resource-row `work_item` pointers don't
	resolve to a current BOQ row. Recomputing here brings stored values back
	in line with what Tender.rollup_boq_costs() now computes server-side, so
	the form's dirty-on-load check (JS recompute vs stored value) agrees on
	load instead of tripping immediately.

	Writes through frappe.db rather than saving the Tender, for the same
	reason backfill_boq_row_id does: Tender.on_update() runs
	handle_won_automation(), and re-saving live tenders here could fire
	project/sales-order side effects.
	"""
	boq_rows = frappe.db.sql(
		"""
		select parent, idx, is_group, vat_percentage, other_additions_pct, fixed_additions
		from `tabTender BOQ Item`
		""",
		as_dict=True,
	)

	totals_by_work_item = {}  # {tender: {idx: amount}}
	for doctype in RESOURCE_TABLES:
		rows = frappe.db.sql(
			"select parent, work_item, amount from `tab{0}`".format(doctype),
			as_dict=True,
		)
		for row in rows:
			by_idx = totals_by_work_item.setdefault(row.parent, {})
			by_idx[row.work_item] = by_idx.get(row.work_item, 0.0) + (row.amount or 0)

	by_tender = {}
	for row in boq_rows:
		by_tender.setdefault(row.parent, []).append(row)

	updated = 0
	for tender, rows in by_tender.items():
		subtotal = total_vat = total_additions = 0.0
		for row in rows:
			if row.is_group:
				continue
			base_cost = totals_by_work_item.get(tender, {}).get(row.idx, 0.0)
			vat = base_cost * (row.vat_percentage or 0) / 100.0
			other = base_cost * (row.other_additions_pct or 0) / 100.0
			fixed = row.fixed_additions or 0
			subtotal += base_cost
			total_vat += vat
			total_additions += other + fixed

		frappe.db.set_value(
			"Tender", tender,
			{
				"subtotal": subtotal,
				"total_vat": total_vat,
				"total_additions": total_additions,
				"grand_total": subtotal + total_vat + total_additions,
			},
			update_modified=False,
		)
		updated += 1

	frappe.db.commit()
	print("backfill_header_totals: recomputed totals on {0} tenders".format(updated))
