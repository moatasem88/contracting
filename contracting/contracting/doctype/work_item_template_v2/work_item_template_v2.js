frappe.ui.form.on('Work Item Template V2', {
	setup(frm) {
		// is_stock_item doesn't reliably distinguish service items from
		// physical goods in this bench's data (plenty of real equipment -
		// Scaffolding, Furniture, Tools - is marked is_stock_item=0 for
		// unrelated stock-tracking reasons). Item Group is the actual
		// signal: dedicated "Labor" / "Subcontractors Items" / "Equipment"
		// groups keep those pools visually distinct in the picker from ordinary material.
		// Registered in setup (not just onload) to match this app's own
		// convention (see contract_document.js) - onload alone has been
		// observed not to stick for grid Link fields on every load.
		set_item_queries(frm);
	},
	onload(frm) { set_item_queries(frm); },
	refresh(frm) {
		set_item_queries(frm);
		calculate_totals(frm, {from_refresh: true});
	},
	material_items_add(frm) { calculate_totals(frm); },
	material_items_remove(frm) { calculate_totals(frm); },
	labor_items_add(frm) { calculate_totals(frm); },
	labor_items_remove(frm) { calculate_totals(frm); },
	subcontract_items_add(frm) { calculate_totals(frm); },
	subcontract_items_remove(frm) { calculate_totals(frm); },
	equipment_items_add(frm) { calculate_totals(frm); },
	equipment_items_remove(frm) { calculate_totals(frm); },
	additions_add(frm) { calculate_totals(frm); },
	additions_remove(frm) { calculate_totals(frm); }
});

['Template Material Item V2', 'Template Labor Item V2', 'Template Subcontract Item V2', 'Template Equipment Item V2']
	.forEach(dt => {
	frappe.ui.form.on(dt, {
		qty_per_unit(frm, cdt, cdn) { calculate_totals(frm); },
		rate(frm, cdt, cdn) { calculate_totals(frm); }
	});
});

frappe.ui.form.on('Work Item Template Addition', {
	value(frm, cdt, cdn) { calculate_totals(frm); },
	calculation_type(frm, cdt, cdn) { calculate_totals(frm); },
	per_unit_item(frm, cdt, cdn) { calculate_totals(frm); }
});

function set_item_queries(frm) {
	frm.set_query('item', 'material_items', () => ({filters: {is_stock_item: 1}}));
	frm.set_query('item', 'labor_items', () => ({filters: {item_group: 'Labor'}}));
	frm.set_query('item', 'subcontract_items', () => ({filters: {item_group: 'Subcontractors Items'}}));
	frm.set_query('item', 'equipment_items', () => ({filters: {item_group: 'Equipment'}}));
}

const TEMPLATE_RESOURCE_TABLES = ['material_items', 'labor_items', 'subcontract_items', 'equipment_items'];

function calculate_totals(frm, opts) {
	let base_cost = 0;
	TEMPLATE_RESOURCE_TABLES.forEach(fieldname => {
		(frm.doc[fieldname] || []).forEach(row => {
			row.amount = (row.qty_per_unit || 0) * (row.rate || 0);
			base_cost += row.amount;
		});
	});

	// Mirrors WorkItemTemplateV2.calculate_totals() - per-unit Fixed
	// Amounts are part of the per-unit cost, lump sums are reported
	// separately. per_unit_item is ignored on Percentage rows.
	let additions_pct = 0, fixed_per_unit = 0, fixed_lump_sum = 0;
	(frm.doc.additions || []).forEach(a => {
		if (a.calculation_type === 'Percentage') additions_pct += (a.value || 0);
		else if (a.calculation_type === 'Fixed Amount') {
			if (a.per_unit_item) fixed_per_unit += (a.value || 0);
			else fixed_lump_sum += (a.value || 0);
		}
	});

	// On refresh() only write back when the form is new or already
	// dirty - a rounding difference against the saved server values must
	// never mark a clean template dirty on open (same idiom as tender.js).
	if (!(opts && opts.from_refresh) || frm.doc.__islocal || frm.is_dirty()) {
		let total_additions = base_cost * additions_pct / 100 + fixed_per_unit;
		frm.set_value('total_work_item_cost', base_cost);
		frm.set_value('total_additions', total_additions);
		frm.set_value('total_cost_per_unit', base_cost + total_additions);
		frm.set_value('total_fixed_lump_sum', fixed_lump_sum);
	}
	TEMPLATE_RESOURCE_TABLES.forEach(fieldname => frm.refresh_field(fieldname));
}
