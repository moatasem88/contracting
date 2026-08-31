import frappe


def execute():
	"""Rename "Percentage of Running Total" -> "Percentage of Total Price"
	on every existing Project Tender Cost Line row and the Tender Cost
	Component master (both share this Select option), then recompute the
	stored amounts of every affected row under the new K/(1-R) formula -
	confirmed with the user this should recompute, not freeze, historical
	figures, since the old running-total formula no longer exists in code.

	Writes header totals + row amounts via frappe.db.set_value rather than
	.save(), same idiom as backfill_header_totals.py: a raw .save() would
	fire Project Tender.on_update()'s handle_won_automation/cross_sync
	side effects against historical (possibly already-Won) documents.

	Idempotent: the UPDATE ... WHERE calculation_type = 'Percentage of
	Running Total' clauses are no-ops on re-run once nothing matches.
	"""
	affected_parents = frappe.db.sql_list(
		"""select distinct parent from `tabProject Tender Cost Line`
		where calculation_type = 'Percentage of Running Total'"""
	)

	frappe.db.sql(
		"""update `tabProject Tender Cost Line`
		set calculation_type = 'Percentage of Total Price'
		where calculation_type = 'Percentage of Running Total'"""
	)
	frappe.db.sql(
		"""update `tabTender Cost Component`
		set default_calculation_type = 'Percentage of Total Price'
		where default_calculation_type = 'Percentage of Running Total'"""
	)

	updated = 0
	for name in affected_parents:
		doc = frappe.get_doc("Project Tender", name)
		doc.flags.ignore_permissions = True

		try:
			doc.validate_direct_cost_details()
		except frappe.ValidationError:
			# Historical data integrity issue unrelated to this rename -
			# still recompute everything downstream of tender_total as-is.
			pass

		doc.calculate_direct_cost()
		doc.calculate_indirect_cost()
		doc.calculate_overhead_and_deductions()
		doc.solve_percentage_of_total_price()
		doc.calculate_percent_of_total_price()
		doc.calculate_vat_and_sell_price()
		doc.calculate_total_addition_percent()

		frappe.db.set_value(
			"Project Tender", doc.name,
			{
				"total_direct_cost": doc.total_direct_cost,
				"total_indirect_cost": doc.total_indirect_cost,
				"total_direct_and_indirect_cost": doc.total_direct_and_indirect_cost,
				"sub_total_overhead_and_profit": doc.sub_total_overhead_and_profit,
				"grand_total_price": doc.grand_total_price,
				"vat_amount": doc.vat_amount,
				"total_sell_price": doc.total_sell_price,
				"total_addition_percent": doc.total_addition_percent,
			},
			update_modified=False,
		)

		for row in doc.direct_cost_details:
			frappe.db.set_value(
				row.doctype, row.name,
				{"percent_of_total_price": row.percent_of_total_price},
				update_modified=False,
			)

		for row in list(doc.indirect_cost_details) + list(doc.overhead_deduction_details):
			frappe.db.set_value(
				row.doctype, row.name,
				{
					"amount": row.amount,
					"rate": row.rate,
					"percent_of_direct_cost": row.percent_of_direct_cost,
					"percent_of_total_price": row.percent_of_total_price,
				},
				update_modified=False,
			)

		updated += 1

	frappe.db.commit()
	print(
		"migrate_percentage_of_running_total_to_total_price: renamed option and recomputed "
		"{0} Project Tenders".format(updated)
	)
