// Tender staff may only create resource items (controllers/item.py) - offer
// them just those Item Groups, and keep the item non-stock. The server
// enforces both; this only keeps the form from offering a dead end.
frappe.ui.form.on('Item', {
	setup(frm) {
		frappe.call({
			method: 'contracting.contracting.controllers.item.get_item_group_scope',
			callback(r) {
				frm.__tender_item_groups = r.message || null;
				apply_tender_item_scope(frm);
			},
		});
	},
	// ERPNext's own item.js assigns fields_dict.item_group.get_query in its
	// setup, so ours is re-applied on every refresh to stay on top.
	refresh(frm) { apply_tender_item_scope(frm); },
});

function apply_tender_item_scope(frm) {
	const groups = frm.__tender_item_groups;
	if (!groups) return;
	frm.fields_dict.item_group.get_query = () => ({filters: [['Item Group', 'name', 'in', groups]]});
	if (frm.is_new() && frm.doc.is_stock_item) frm.set_value('is_stock_item', 0);
	frm.set_df_property('is_stock_item', 'read_only', 1);
	// Any other existing Item is read-only for them (the server refuses the
	// save via the has_permission hook, which Desk doesn't consult up front).
	if (!frm.is_new() && !groups.includes(frm.doc.item_group) && !frm.__tender_read_only) {
		frm.__tender_read_only = true;
		frm.set_read_only();
		frm.disable_save();
		frm.refresh_fields();
	}
}
