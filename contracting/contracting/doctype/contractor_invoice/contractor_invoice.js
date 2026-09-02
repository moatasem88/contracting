const RETENTION_METHOD = 'contracting.contracting.utils.progress_invoicing.get_contract_retention_rate';
const ITEM_CONTEXT_METHOD = 'contracting.contracting.utils.progress_invoicing.get_item_progress_context';
const CONDITION_CONTEXT_METHOD = 'contracting.contracting.utils.progress_invoicing.get_condition_progress_context';

// FR-01-11: live 3-way progress entry. Mirrors progress_invoicing.py's
// server-side formulas byte-for-byte (NFR-01) - any change to one of the
// two recompute_*_row functions below needs the matching change in
// compute_progress_invoice_items/compute_condition_progress_invoice, and
// vice versa.

function fetch_retention_rate(frm) {
	if (!frm.doc.contractor_contract) {
		frm.__retention_rate = 0;
		return;
	}
	frappe.call({
		method: RETENTION_METHOD,
		args: {contract_name: frm.doc.contractor_contract},
		callback(r) {
			frm.__retention_rate = flt(r.message) || 0;
		}
	});
}

function recompute_item_row(frm, row, source_field) {
	const qty_allocated = flt(row.qty_allocated);
	const prior_max_qty = flt(row.__prior_max_qty);

	if (source_field === 'percent_complete') {
		row.cumulative_qty_complete = qty_allocated ? flt(row.percent_complete) / 100 * qty_allocated : 0;
	} else if (source_field === 'this_period_qty') {
		row.cumulative_qty_complete = prior_max_qty + flt(row.this_period_qty);
	}

	row.percent_complete = qty_allocated ? flt(row.cumulative_qty_complete) / qty_allocated * 100 : 0;
	row.this_period_qty = flt(row.cumulative_qty_complete) - prior_max_qty;

	const rate = flt(row.rate);
	const prior_invoiced_amount = flt(row.__prior_invoiced_amount);
	row.cumulative_amount = flt(row.cumulative_qty_complete) * rate;
	row.previously_invoiced_amount = prior_invoiced_amount;
	row.this_period_amount = row.cumulative_amount - prior_invoiced_amount;
	row.retention_amount = row.this_period_amount * flt(frm.__retention_rate) / 100;
	row.net_amount = row.this_period_amount - row.retention_amount;

	// FR-11: non-blocking - typing continues, the hard block stays at
	// validate() (save time), unchanged.
	if (flt(row.cumulative_qty_complete) > qty_allocated + 1e-6) {
		frappe.show_alert({message: __('Row {0}: cumulative quantity exceeds the allocated quantity.', [row.idx]), indicator: 'orange'});
	} else if (flt(row.cumulative_qty_complete) + 1e-6 < prior_max_qty) {
		frappe.show_alert({message: __('Row {0}: cumulative quantity cannot regress below the previous invoice.', [row.idx]), indicator: 'orange'});
	}
}

function load_item_context(frm, cdt, cdn, reset) {
	const row = locals[cdt][cdn];
	if (!row.contract_item) return;

	if (reset) {
		row.cumulative_qty_complete = 0;
		row.percent_complete = 0;
		row.this_period_qty = 0;
	}

	frappe.call({
		method: ITEM_CONTEXT_METHOD,
		args: {contract_item: row.contract_item, own_doctype: 'Contractor Invoice', doc_name: frm.doc.name},
		callback(r) {
			const ctx = r.message || {};
			row.qty_allocated = flt(ctx.qty_allocated);
			row.rate = flt(ctx.rate);
			row.__prior_max_qty = flt(ctx.prior_max_qty);
			row.__prior_invoiced_amount = flt(ctx.prior_invoiced_amount);
			recompute_item_row(frm, row, 'cumulative_qty_complete');
			frm.refresh_field('items');
		}
	});
}

function recompute_condition_row(frm, row, source_field) {
	const qty_allocated = flt(row.qty_allocated);
	const prior_max_qty = flt(row.__prior_max_qty);

	if (source_field === 'percent_complete') {
		row.cumulative_qty_complete = qty_allocated ? flt(row.percent_complete) / 100 * qty_allocated : 0;
	} else if (source_field === 'this_period_qty') {
		row.cumulative_qty_complete = prior_max_qty + flt(row.this_period_qty);
	}

	row.percent_complete = qty_allocated ? flt(row.cumulative_qty_complete) / qty_allocated * 100 : 0;
	row.this_period_qty = flt(row.cumulative_qty_complete) - prior_max_qty;

	const rate = flt(row.rate);
	const prior_invoiced_amount = flt(row.__prior_invoiced_amount);
	row.cumulative_amount = flt(row.cumulative_qty_complete) * rate;
	row.previously_invoiced_amount = prior_invoiced_amount;
	row.this_period_amount = row.cumulative_amount - prior_invoiced_amount;
	row.retention_amount = row.this_period_amount * flt(frm.__retention_rate) / 100;
	row.net_amount = row.this_period_amount - row.retention_amount;

	if (flt(row.cumulative_qty_complete) > qty_allocated + 1e-6) {
		frappe.show_alert({message: __('Row {0}: cumulative quantity exceeds this condition\'s share of the allocated quantity.', [row.idx]), indicator: 'orange'});
	} else if (flt(row.cumulative_qty_complete) + 1e-6 < prior_max_qty) {
		frappe.show_alert({message: __('Row {0}: cumulative quantity cannot regress below the previous invoice.', [row.idx]), indicator: 'orange'});
	}
}

function load_condition_context(frm, cdt, cdn, reset) {
	const row = locals[cdt][cdn];
	if (!row.contract_item || !row.payment_condition) return;

	if (reset) {
		row.cumulative_qty_complete = 0;
		row.percent_complete = 0;
		row.this_period_qty = 0;
	}

	frappe.call({
		method: CONDITION_CONTEXT_METHOD,
		args: {contract_item: row.contract_item, payment_condition: row.payment_condition, doc_name: frm.doc.name},
		callback(r) {
			const ctx = r.message || {};
			row.qty_allocated = flt(ctx.qty_allocated);
			row.rate = flt(ctx.rate);
			row.condition_label = ctx.condition_label;
			row.__prior_max_qty = flt(ctx.prior_max_qty);
			row.__prior_invoiced_amount = flt(ctx.prior_invoiced_amount);
			recompute_condition_row(frm, row, 'cumulative_qty_complete');
			frm.refresh_field('condition_progress');
		}
	});
}

frappe.ui.form.on('Contractor Invoice', {
	// Every claimed line has to be a line of the contract being claimed
	// against - compute_progress_invoice_items enforces that on save, and
	// this keeps the picker from offering rows it will only reject. With no
	// contract chosen yet the filter matches nothing, which is the honest
	// answer: there is no set of items to pick from.
	setup(frm) {
		frm.set_query('contract_item', 'items', () => ({
			filters: {
				parenttype: 'Subcontractor Contract',
				parent: frm.doc.contractor_contract || ''
			}
		}));
		frm.set_query('contract_item', 'condition_progress', () => ({
			filters: {
				parenttype: 'Subcontractor Contract',
				parent: frm.doc.contractor_contract || ''
			}
		}));
	},

	contractor_contract(frm) {
		fetch_retention_rate(frm);
	},

	refresh(frm) {
		if (frm.doc.__islocal) return;

		fetch_retention_rate(frm);
		// FR-05 load-time case: an existing row already has contract_item
		// set, so its live-entry context needs to exist before the user
		// edits anything - fetched without the zero-reset new selections get.
		(frm.doc.items || []).forEach(row => load_item_context(frm, row.doctype, row.name, false));
		(frm.doc.condition_progress || []).forEach(row => load_condition_context(frm, row.doctype, row.name, false));

		if (frm.doc.status === 'Draft') {
			frm.add_custom_button(__('Mark as Measured'), () => {
				frappe.call({
					method: 'contracting.contracting.doctype.contractor_invoice.contractor_invoice.mark_as_measured',
					args: {name: frm.doc.name},
					callback() { frm.reload_doc(); }
				});
			});
		}

		if (frm.doc.status === 'Measured') {
			frm.add_custom_button(__('Approve && Generate Purchase Invoice'), () => {
				frappe.confirm(
					__('Approve this Contractor Invoice and generate a Purchase Invoice for {0}?', [format_currency(frm.doc.total_this_period)]),
					() => {
						frappe.call({
							method: 'contracting.contracting.doctype.contractor_invoice.contractor_invoice.approve_and_invoice',
							args: {name: frm.doc.name},
							callback(r) {
								frm.reload_doc();
								if (r.message) {
									frappe.set_route('purchase-invoice', r.message);
								}
							}
						});
					}
				);
			}).addClass('btn-primary');
		}

		if (frm.doc.purchase_invoice) {
			frm.add_custom_button(__('Purchase Invoice'), () => frappe.set_route('purchase-invoice', frm.doc.purchase_invoice), __('View'));
		}
	}
});

frappe.ui.form.on('Contractor Invoice Item', {
	contract_item(frm, cdt, cdn) {
		load_item_context(frm, cdt, cdn, true);
	},
	cumulative_qty_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item) return;
		recompute_item_row(frm, row, 'cumulative_qty_complete');
		frm.refresh_field('items');
	},
	percent_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item) return;
		recompute_item_row(frm, row, 'percent_complete');
		frm.refresh_field('items');
	},
	this_period_qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item) return;
		recompute_item_row(frm, row, 'this_period_qty');
		frm.refresh_field('items');
	}
});

frappe.ui.form.on('Contractor Invoice Condition Progress', {
	contract_item(frm, cdt, cdn) {
		load_condition_context(frm, cdt, cdn, true);
	},
	payment_condition(frm, cdt, cdn) {
		load_condition_context(frm, cdt, cdn, true);
	},
	cumulative_qty_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item || !row.payment_condition) return;
		recompute_condition_row(frm, row, 'cumulative_qty_complete');
		frm.refresh_field('condition_progress');
	},
	percent_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item || !row.payment_condition) return;
		recompute_condition_row(frm, row, 'percent_complete');
		frm.refresh_field('condition_progress');
	},
	this_period_qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item || !row.payment_condition) return;
		recompute_condition_row(frm, row, 'this_period_qty');
		frm.refresh_field('condition_progress');
	}
});
