import frappe
from frappe.utils import flt


def execute():
	"""Backfill every field this BRD adds, so existing Tenders/Project
	Tenders don't show blank/stale values for them before anyone touches
	the record again:

	  - Project Tender.total_addition_percent
	  - Tender.propagated_addition_percent (pushed from the linked Project
	    Tender, same as the live cross-sync would do - Tenders with no
	    project_tender linkage are left at 0, per the BRD's own edge case)
	  - Material/Labor/Equipment row direct_cost_amount/safety_factor_amount/
	    indirect_cost_amount/amount_currency/rate_egp
	  - Tender BOQ Item rollups (material_cost..boq_indirect_cost_amount,
	    indirect_cost_percent, display_amount)

	Must run *after* migrate_percentage_of_running_total_to_total_price -
	that patch fixes grand_total_price/total_sell_price on Project Tenders
	using the old "Percentage of Running Total" option, which this patch's
	total_addition_percent computation reads.

	Writes via frappe.db.set_value, not .save() - same idiom as
	backfill_header_totals.py, to avoid firing Tender/Project Tender
	on_update() side effects (won-automation, cross-sync pushes) against
	historical data during a one-time backfill. Naturally idempotent:
	recomputing from current stored inputs with the same formula yields
	the same output, so re-running this patch is a no-op in effect.
	"""
	project_tender_names = frappe.get_all("Project Tender", pluck="name")
	total_addition_percent_by_pt = {}

	for name in project_tender_names:
		total_direct_cost, total_sell_price = frappe.db.get_value(
			"Project Tender", name, ["total_direct_cost", "total_sell_price"]
		)
		total_addition_percent = (
			(flt(total_sell_price) - flt(total_direct_cost)) / flt(total_direct_cost) * 100
			if total_direct_cost else 0
		)
		total_addition_percent_by_pt[name] = total_addition_percent
		frappe.db.set_value(
			"Project Tender", name, "total_addition_percent", total_addition_percent, update_modified=False
		)

	tender_rows = frappe.get_all(
		"Tender", fields=["name", "project_tender", "safety_factor_percent"]
	)

	updated = 0
	for trow in tender_rows:
		propagated_addition_percent = total_addition_percent_by_pt.get(trow.project_tender, 0) or 0
		frappe.db.set_value(
			"Tender", trow.name, "propagated_addition_percent", propagated_addition_percent, update_modified=False
		)

		doc = frappe.get_doc("Tender", trow.name)
		doc.propagated_addition_percent = propagated_addition_percent
		doc.calculate_resource_quantities()
		doc.rollup_boq_costs()

		for fieldname in ("material_items", "labor_items", "equipment_items"):
			for row in doc.get(fieldname):
				frappe.db.set_value(
					row.doctype, row.name,
					{
						"direct_cost_amount": row.direct_cost_amount,
						"safety_factor_amount": row.safety_factor_amount,
						"indirect_cost_amount": row.indirect_cost_amount,
						"amount_currency": row.amount_currency,
						"rate_egp": row.rate_egp,
					},
					update_modified=False,
				)

		for row in doc.boq_items:
			if row.is_group:
				continue
			frappe.db.set_value(
				row.doctype, row.name,
				{
					"material_cost": row.material_cost,
					"labor_cost": row.labor_cost,
					"equipment_cost": row.equipment_cost,
					"boq_direct_cost": row.boq_direct_cost,
					"boq_safety_factor_amount": row.boq_safety_factor_amount,
					"boq_indirect_cost_amount": row.boq_indirect_cost_amount,
					"indirect_cost_percent": row.indirect_cost_percent,
					"display_amount": row.display_amount,
				},
				update_modified=False,
			)

		updated += 1

	frappe.db.commit()
	print(
		"backfill_indirect_cost_fields: recomputed {0} Project Tenders and {1} Tenders".format(
			len(project_tender_names), updated
		)
	)
