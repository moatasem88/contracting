import frappe


def execute():
	"""Backfill the fix from the 2026-08-28 BRD ("Selling Amount Must Include
	Indirect Cost") onto every Complete Tender - these are locked by
	validate_not_complete_locked() so they can't pick up the corrected
	calculate_resource_quantities()/rollup_boq_costs()/calculate_row_sell_pricing()
	formula by being re-saved through the normal form.

	Recomputes, per Complete Tender:
	  - Material/Labor/Equipment row `amount` (now Direct + Safety + Indirect,
	    was Direct + Safety only)
	  - Tender BOQ Item `total_amount`/`effective_unit_price`/`sell_rate`/
	    `sell_amount` (derivatives of the corrected `amount`)
	  - Tender `subtotal`/`total_vat`/`total_additions`/`grand_total`
	  - Project BOQ Item `project_item_rate` for every row already carried
	    from one of these Tenders' BOQ rows (via source_tender_boq_item)

	Does NOT touch the 5 already-submitted Sales Orders keyed off these
	Tenders - their rates stay exactly as originally created, confirmed in
	the BRD as a deliberate non-goal.

	Writes via frappe.db.set_value(update_modified=False), never .save() -
	same idiom as backfill_indirect_cost_fields.py, so historical writes
	don't re-trigger Tender.on_update()'s cross-sync push or any
	won-automation side effect. Naturally idempotent: recomputing from the
	same stored inputs (rate, qty_per_unit, exchange_rate,
	safety_factor_percent, propagated_addition_percent, vat_percentage,
	other_additions_pct, fixed_additions, margin_percent) with the corrected
	formula yields the same output on every run.
	"""
	tender_names = frappe.get_all("Tender", filters={"status": "Complete"}, pluck="name")

	updated = 0
	boq_rows_updated = 0
	project_boq_rows_updated = 0

	for tender_name in tender_names:
		doc = frappe.get_doc("Tender", tender_name)
		doc.calculate_resource_quantities()
		doc.rollup_boq_costs()
		doc.calculate_row_sell_pricing()

		for fieldname in ("material_items", "labor_items", "equipment_items"):
			for row in doc.get(fieldname):
				frappe.db.set_value(
					row.doctype, row.name, "amount", row.amount, update_modified=False,
				)

		sell_rate_by_boq_name = {}
		for row in doc.boq_items:
			if row.is_group:
				continue
			frappe.db.set_value(
				row.doctype, row.name,
				{
					"total_amount": row.total_amount,
					"effective_unit_price": row.effective_unit_price,
					"sell_rate": row.sell_rate,
					"sell_amount": row.sell_amount,
				},
				update_modified=False,
			)
			sell_rate_by_boq_name[row.name] = row.sell_rate
			boq_rows_updated += 1

		frappe.db.set_value(
			"Tender", tender_name,
			{
				"subtotal": doc.subtotal,
				"total_vat": doc.total_vat,
				"total_additions": doc.total_additions,
				"grand_total": doc.grand_total,
			},
			update_modified=False,
		)

		if sell_rate_by_boq_name:
			project_boq_rows = frappe.get_all(
				"Project BOQ Item",
				filters={"source_tender_boq_item": ["in", list(sell_rate_by_boq_name.keys())]},
				fields=["name", "source_tender_boq_item"],
			)
			for pbi_row in project_boq_rows:
				frappe.db.set_value(
					"Project BOQ Item", pbi_row.name,
					"project_item_rate", sell_rate_by_boq_name[pbi_row.source_tender_boq_item],
					update_modified=False,
				)
				project_boq_rows_updated += 1

		updated += 1

	frappe.db.commit()
	print(
		"backfill_selling_amount_with_indirect_cost: recomputed {0} Complete Tenders, "
		"{1} Tender BOQ Item rows, {2} Project BOQ Item rows".format(
			updated, boq_rows_updated, project_boq_rows_updated
		)
	)
