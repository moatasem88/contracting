// Copyright (c) 2026, kazem and contributors
// For license information, please see license.txt

frappe.ui.form.on('Work Item Template', {
	// Work Item Template Addition is shared with V2, whose per_unit_item
	// flag means nothing on legacy V1. depends_on can't hide a grid
	// list-view column in this Frappe version, so hide it here.
	refresh(frm) { hide_per_unit_item(frm); }
});

function hide_per_unit_item(frm) {
	let grid = frm.fields_dict.additions && frm.fields_dict.additions.grid;
	let df = grid && grid.docfields.find(d => d.fieldname === 'per_unit_item');
	if (!df || df.hidden) return;
	grid.toggle_display('per_unit_item', false);
	// The grid caches its visible columns on first render and
	// toggle_display doesn't invalidate that - rebuild once.
	grid.reset_grid();
}
