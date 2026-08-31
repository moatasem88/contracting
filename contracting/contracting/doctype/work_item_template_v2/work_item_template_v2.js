frappe.ui.form.on('Work Item Template V2', {
	setup(frm) {
		// is_stock_item doesn't reliably distinguish service items from
		// physical goods in this bench's data (plenty of real equipment -
		// Scaffolding, Furniture, Tools - is marked is_stock_item=0 for
		// unrelated stock-tracking reasons). Item Group is the actual
		// signal: dedicated "Labor" / "Equipment" groups keep those two
		// pools visually distinct in the picker from ordinary material.
		// Registered in setup (not just onload) to match this app's own
		// convention (see contract_document.js) - onload alone has been
		// observed not to stick for grid Link fields on every load.
		set_item_queries(frm);
	},
	onload(frm) { set_item_queries(frm); },
	refresh(frm) {
		set_item_queries(frm);
		calculate_totals(frm);
	},
	material_items_add(frm) { calculate_totals(frm); },
	material_items_remove(frm) { calculate_totals(frm); },
	labor_items_add(frm) { calculate_totals(frm); },
	labor_items_remove(frm) { calculate_totals(frm); },
	equipment_items_add(frm) { calculate_totals(frm); },
	equipment_items_remove(frm) { calculate_totals(frm); },
	additions_add(frm) { calculate_totals(frm); },
	additions_remove(frm) { calculate_totals(frm); }
});

['Template Material Item V2', 'Template Labor Item V2', 'Template Equipment Item V2'].forEach(dt => {
	frappe.ui.form.on(dt, {
		qty_per_unit(frm, cdt, cdn) { calculate_totals(frm); },
		rate(frm, cdt, cdn) { calculate_totals(frm); }
	});
});

frappe.ui.form.on('Work Item Template Addition', {
	value(frm, cdt, cdn) { calculate_totals(frm); },
	calculation_type(frm, cdt, cdn) { calculate_totals(frm); }
});

function set_item_queries(frm) {
	frm.set_query('item', 'material_items', () => ({filters: {is_stock_item: 1}}));
	frm.set_query('item', 'labor_items', () => ({filters: {item_group: 'Labor'}}));
	frm.set_query('item', 'equipment_items', () => ({filters: {item_group: 'Equipment'}}));
}

function calculate_totals(frm) {
	let base_cost = 0;
	['material_items', 'labor_items', 'equipment_items'].forEach(fieldname => {
		(frm.doc[fieldname] || []).forEach(row => {
			row.amount = (row.qty_per_unit || 0) * (row.rate || 0);
			base_cost += row.amount;
		});
	});

	let additions_pct = 0, fixed = 0;
	(frm.doc.additions || []).forEach(a => {
		if (a.calculation_type === 'Percentage') additions_pct += (a.value || 0);
		else if (a.calculation_type === 'Fixed Amount') fixed += (a.value || 0);
	});

	frm.set_value('total_cost_per_unit', base_cost * (1 + additions_pct / 100) + fixed);
	frm.refresh_field('material_items');
	frm.refresh_field('labor_items');
	frm.refresh_field('equipment_items');
}
