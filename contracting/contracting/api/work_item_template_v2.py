import frappe


@frappe.whitelist()
def get_v2_template_rows(template_name):
	"""Expand a flat Work Item Template V2 for pulling into a Tender:
	parent-row fields to merge into the selected Tender BOQ Item row
	(including its VAT/other-additions, carried over from the template),
	plus flat resource rows tagged by type for insertion into the
	Tender's own Material/Labor/Equipment tabs - those tabs are the real
	editable input now, not nested children of the BOQ row. Rate on each
	resource row comes solely from the template's own configured rate
	(0 if unset) - an item's last_purchase_rate is never fetched here,
	since purchase history can be stale, off-project, or tied to a
	different quantity/supplier than the one being estimated now."""

	template = frappe.get_doc("Work Item Template V2", template_name)

	children = []
	for fieldname, resource_type in (
		("material_items", "material"),
		("labor_items", "labor"),
		("equipment_items", "equipment"),
	):
		for row in template.get(fieldname):
			rate = row.rate or 0
			children.append({
				"resource_type": resource_type,
				"item": row.item,
				"qty_per_unit": row.qty_per_unit or 0,
				"rate": rate,
			})

	# Per-unit fixed amounts go to fixed_additions_per_unit, which the
	# Tender multiplies by original_quantity; lump sums go to
	# fixed_additions, added once per BOQ row.
	additions_pct = 0.0
	fixed_per_unit = 0.0
	fixed_lump_sum = 0.0
	for a in template.additions:
		if a.calculation_type == "Percentage":
			additions_pct += (a.value or 0)
		elif a.calculation_type == "Fixed Amount":
			if a.per_unit_item:
				fixed_per_unit += (a.value or 0)
			else:
				fixed_lump_sum += (a.value or 0)

	parent = {
		"item_name": template.template_name,
		"item_type": "Work Item",
		"uom": template.uom,
		"is_group": 0,
		"vat_percentage": 0,
		"other_additions_pct": additions_pct,
		"fixed_additions": fixed_lump_sum,
		"fixed_additions_per_unit": fixed_per_unit,
	}

	return {"parent": parent, "children": children}
