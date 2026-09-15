const RETENTION_METHOD = 'contracting.contracting.utils.progress_invoicing.get_contract_retention_rate';
const ITEM_CONTEXT_METHOD = 'contracting.contracting.utils.progress_invoicing.get_item_progress_context';
const CONDITION_CONTEXT_METHOD = 'contracting.contracting.utils.progress_invoicing.get_condition_progress_context';
const CONDITION_SEED_METHOD = 'contracting.contracting.utils.progress_invoicing.get_condition_seed_rows';
const DOCTYPE_PATH = 'contracting.contracting.doctype.contractor_invoice.contractor_invoice';
const TOTALS_PREVIEW_METHOD = `${DOCTYPE_PATH}.get_totals_preview`;
const GET_BILLING_ACCOUNT_ROWS_METHOD = `${DOCTYPE_PATH}.get_billing_account_rows`;
const CONFIRM_BILLING_ACCOUNTS_METHOD = `${DOCTYPE_PATH}.confirm_billing_accounts`;

// FR-05: once condition_progress is seeded, Items becomes a pure rollup
// (compute_condition_progress_invoice overwrites qty_allocated/percent_
// complete/this_period_qty from the condition rows on every save) - these
// three are the only Items fields a user could otherwise hand-edit.
const ITEMS_ENTRY_FIELDS = ['cumulative_qty_complete', 'percent_complete', 'this_period_qty'];

function set_items_entry_readonly(frm, readonly) {
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
	if (!grid) return;
	ITEMS_ENTRY_FIELDS.forEach(fieldname => grid.update_docfield_property(fieldname, 'read_only', readonly ? 1 : 0));
	grid.refresh();
}

// FR-01-11: live 3-way progress entry. Mirrors progress_invoicing.py's
// server-side formulas byte-for-byte (NFR-01) - any change to one of the
// two recompute_*_row functions below needs the matching change in
// compute_progress_invoice_items/compute_condition_progress_invoice, and
// vice versa.

// FR-15-17, NFR-01: live header-totals preview, debounced so a burst of row
// edits doesn't fire one request per keystroke. Calls the exact same
// server-side code path validate() uses on save (get_totals_preview), so the
// preview can never drift from the saved values for the same row data.
let __totals_preview_timeout;
function refresh_totals_preview(frm) {
	if (!frm.doc.contractor_contract) return;

	clearTimeout(__totals_preview_timeout);
	__totals_preview_timeout = setTimeout(() => {
		frappe.call({
			method: TOTALS_PREVIEW_METHOD,
			args: {doc: frm.doc},
			callback(r) {
				if (!r.message) return;
				frm.set_value('total_this_period', r.message.total_this_period);
				frm.set_value('total_retention_held', r.message.total_retention_held);
				frm.set_value('total_additional_charges', r.message.total_additional_charges);
				frm.set_value('net_payable', r.message.net_payable);
				frm.clear_table('additional_costs');
				(r.message.additional_costs || []).forEach(row => frm.add_child('additional_costs', row));
				frm.refresh_field('additional_costs');
			}
		});
	}, 400);
}

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

	// FR-12/13/14: clamp to the valid range instead of only alerting - a
	// derived or typed value outside [prior_max_qty, qty_allocated] is
	// pulled back in line before percent/this_period/amounts are derived
	// from it.
	row.cumulative_qty_complete = Math.min(qty_allocated, Math.max(prior_max_qty, flt(row.cumulative_qty_complete)));

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
			row.description = ctx.description;
			row.__prior_max_qty = flt(ctx.prior_max_qty);
			row.__prior_invoiced_amount = flt(ctx.prior_invoiced_amount);
			// FR-09/10/11: a freshly-selected row seeds at the previous
			// invoice's own cumulative qty, not 0 - otherwise this_period_qty
			// (cumulative - prior_max_qty) goes negative on first load.
			if (reset) {
				row.cumulative_qty_complete = row.__prior_max_qty;
			}
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

	// FR-12/13/14: clamp to the valid range instead of only alerting.
	row.cumulative_qty_complete = Math.min(qty_allocated, Math.max(prior_max_qty, flt(row.cumulative_qty_complete)));

	row.percent_complete = qty_allocated ? flt(row.cumulative_qty_complete) / qty_allocated * 100 : 0;
	row.this_period_qty = flt(row.cumulative_qty_complete) - prior_max_qty;

	const rate = flt(row.rate);
	const condition_percent = flt(row.condition_percent);
	const prior_invoiced_amount = flt(row.__prior_invoiced_amount);
	// FR-03/NFR-01: byte-for-byte match to compute_condition_progress_invoice -
	// the condition's percent scales the amount, not the qty ceiling.
	row.cumulative_amount = flt(row.cumulative_qty_complete) * rate * condition_percent / 100;
	row.previously_invoiced_amount = prior_invoiced_amount;
	row.this_period_amount = row.cumulative_amount - prior_invoiced_amount;
	row.retention_amount = row.this_period_amount * flt(frm.__retention_rate) / 100;
	row.net_amount = row.this_period_amount - row.retention_amount;

	if (flt(row.cumulative_qty_complete) > qty_allocated + 1e-6) {
		frappe.show_alert({message: __('Row {0}: cumulative quantity exceeds the contract line\'s contracted quantity.', [row.idx]), indicator: 'orange'});
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
			row.description = ctx.description;
			row.condition_percent = flt(ctx.condition_percent);
			row.__prior_max_qty = flt(ctx.prior_max_qty);
			row.__prior_invoiced_amount = flt(ctx.prior_invoiced_amount);
			if (reset) {
				row.cumulative_qty_complete = row.__prior_max_qty;
			}
			recompute_condition_row(frm, row, 'cumulative_qty_complete');
			frm.refresh_field('condition_progress');
		}
	});
}

// FR-12/13: a small always-visible live-filter box above each grid, since
// Frappe's native per-grid filter icon's discoverability varies by row
// count. Hidden entirely when the grid has no rows yet (FR-13).
const GRID_SEARCH_FIELDS = {
	items: ['description'],
	condition_progress: ['description', 'condition_label'],
};

function setup_grid_search(frm, fieldname) {
	const grid = frm.fields_dict[fieldname] && frm.fields_dict[fieldname].grid;
	if (!grid) return;

	let $search = grid.wrapper.find('.contracting-grid-search');
	if (!(frm.doc[fieldname] || []).length) {
		$search.remove();
		return;
	}
	if ($search.length) return;

	$search = $(`<input type="text" class="form-control contracting-grid-search" placeholder="${__('Search')}" style="margin-bottom: 8px; max-width: 300px;">`);
	grid.wrapper.prepend($search);
	$search.on('input', () => {
		const txt = ($search.val() || '').toLowerCase();
		const match_fields = GRID_SEARCH_FIELDS[fieldname] || [];
		grid.grid_rows.forEach(grid_row => {
			const doc = grid_row.doc;
			const matches = !txt || match_fields.some(f => (doc[f] || '').toLowerCase().includes(txt));
			grid_row.wrapper.toggle(matches);
		});
	});
}

function seed_condition_progress(frm) {
	// FR-03/04/06/07/08: a contract with Payment Conditions auto-seeds
	// condition_progress (one row per contract_item x payment_condition) and
	// items (one empty rollup row per distinct contract_item) the moment the
	// contract is chosen - a plain contract leaves both tables untouched.
	if (!frm.doc.contractor_contract) {
		set_items_entry_readonly(frm, false);
		return;
	}

	frappe.call({
		method: CONDITION_SEED_METHOD,
		args: {contractor_contract: frm.doc.contractor_contract},
		callback(r) {
			const pairs = r.message || [];
			if (!pairs.length) {
				set_items_entry_readonly(frm, false);
				return;
			}

			frm.clear_table('condition_progress');
			const contract_items = [];
			pairs.forEach(pair => {
				const row = frm.add_child('condition_progress', {
					contract_item: pair.contract_item,
					payment_condition: pair.payment_condition,
				});
				load_condition_context(frm, row.doctype, row.name, true);
				if (!contract_items.includes(pair.contract_item)) contract_items.push(pair.contract_item);
			});
			frm.refresh_field('condition_progress');
			setup_grid_search(frm, 'condition_progress');

			frm.clear_table('items');
			contract_items.forEach(contract_item => {
				const row = frm.add_child('items', {contract_item});
				load_item_context(frm, row.doctype, row.name, true);
			});
			frm.refresh_field('items');
			setup_grid_search(frm, 'items');

			set_items_entry_readonly(frm, true);
		}
	});
}

frappe.ui.form.on('Contractor Invoice', {
	// FR-05/06: contract picker scoped to the chosen Project, excluding
	// closed/draft/fully-invoiced contracts. FR-08/09: within the chosen
	// contract, item picker additionally excludes lines already 100%
	// invoiced by submitted invoices. With no contract chosen yet the item
	// filter matches nothing, which is the honest answer: there is no set
	// of items to pick from.
	setup(frm) {
		frm.set_query('contractor_contract', () => ({
			query: `${DOCTYPE_PATH}.contract_query`,
			filters: {project: frm.doc.project || ''}
		}));
		frm.set_query('contract_item', 'items', () => ({
			query: `${DOCTYPE_PATH}.contract_item_query`,
			filters: {parent: frm.doc.contractor_contract || ''}
		}));
		frm.set_query('contract_item', 'condition_progress', () => ({
			query: `${DOCTYPE_PATH}.contract_item_query`,
			filters: {parent: frm.doc.contractor_contract || ''}
		}));
	},

	contractor_contract(frm) {
		fetch_retention_rate(frm);
		refresh_totals_preview(frm);
		seed_condition_progress(frm);
	},

	refresh(frm) {
		if (frm.doc.__islocal) return;

		fetch_retention_rate(frm);
		// FR-05 load-time case: an existing row already has contract_item
		// set, so its live-entry context needs to exist before the user
		// edits anything - fetched without the zero-reset new selections get.
		(frm.doc.items || []).forEach(row => load_item_context(frm, row.doctype, row.name, false));
		(frm.doc.condition_progress || []).forEach(row => load_condition_context(frm, row.doctype, row.name, false));
		set_items_entry_readonly(frm, (frm.doc.condition_progress || []).length > 0);
		setup_grid_search(frm, 'items');
		setup_grid_search(frm, 'condition_progress');

		if (frm.doc.purchase_invoice) {
			frm.add_custom_button(__('Purchase Invoice'), () => frappe.set_route('purchase-invoice', frm.doc.purchase_invoice), __('View'));
		}
	},

	// FR-04/05: gates on the one docstatus 0->1 transition (Pending Accounts
	// Manager Approval -> Approve -> Approved) that triggers on_submit ->
	// create_purchase_invoice. Every other state/action combination resolves
	// immediately with no dialog, leaving those six transitions untouched.
	before_workflow_action(frm) {
		if (frm.doc.workflow_state !== 'Pending Accounts Manager Approval' || frm.selected_workflow_action !== 'Approve') {
			return Promise.resolve();
		}

		// show_actions() froze the page via frappe.dom.freeze() immediately
		// before triggering this event, and won't unfreeze until the whole
		// workflow action's promise chain settles - left frozen, the dialog
		// below would sit visually behind/under it.
		frappe.dom.unfreeze();

		return new Promise((resolve, reject) => {
			frappe.call({
				method: GET_BILLING_ACCOUNT_ROWS_METHOD,
				args: {contractor_invoice: frm.doc.name},
				freeze: true,
			}).then(r => {
				show_billing_account_dialog(frm, r.message || [], resolve, reject);
			}).catch(reject);
		});
	}
});

function show_billing_account_dialog(frm, rows, resolve, reject) {
	let confirmed = false;
	const controls = rows.map(row => ({row, control: null}));

	const dialog = new frappe.ui.Dialog({
		title: __('Confirm Billing Accounts'),
		size: 'large',
		fields: [
			{fieldname: 'accounts_html', fieldtype: 'HTML'},
			{fieldname: 'error_html', fieldtype: 'HTML'},
		],
		primary_action_label: __('Confirm & Approve'),
		primary_action() {
			const values = controls.map(({row, control}) => ({
				doctype: row.doctype,
				name: row.name,
				work_item_template: row.work_item_template,
				account: control.get_value(),
			}));

			// FR-06: blocks locally, no server call, while any row is blank.
			if (values.some(v => !v.account)) {
				dialog.fields_dict.error_html.$wrapper.html(
					`<div class="text-danger">${__('Every row needs an Account before you can confirm.')}</div>`
				);
				return;
			}
			dialog.fields_dict.error_html.$wrapper.empty();

			frappe.call({
				method: CONFIRM_BILLING_ACCOUNTS_METHOD,
				args: {contractor_invoice: frm.doc.name, accounts: values},
				freeze: true,
			}).then(() => {
				confirmed = true;
				dialog.hide();
				resolve();
			}).catch(() => {
				// FR-08/other server error: dialog stays open, promise stays
				// unresolved - frappe.call's default handler already showed
				// the error (e.g. the FR-08 conflicting-accounts message).
			});
		},
		on_hide() {
			// FR-09: cancelling/closing without confirming aborts the
			// workflow action entirely - apply_workflow is never called.
			if (!confirmed) reject();
		},
	});

	const rows_html = rows.map((row, idx) => `
		<tr>
			<td>${frappe.utils.escape_html(row.work_item || row.description || row.name)}</td>
			<td>${frappe.utils.escape_html(row.description || '')}</td>
			<td><div class="billing-account-cell" data-idx="${idx}"></div></td>
		</tr>
	`).join('');

	dialog.fields_dict.accounts_html.$wrapper.html(`
		<table class="table table-bordered">
			<thead><tr><th>${__('Work Item')}</th><th>${__('Description')}</th><th>${__('Account')}</th></tr></thead>
			<tbody>${rows_html}</tbody>
		</table>
	`);

	controls.forEach((entry, idx) => {
		const $cell = dialog.fields_dict.accounts_html.$wrapper.find(`.billing-account-cell[data-idx="${idx}"]`);
		const control = frappe.ui.form.make_control({
			parent: $cell,
			df: {fieldtype: 'Link', options: 'Account', fieldname: 'account'},
			render_input: true,
		});
		control.set_value(entry.row.account || '');
		entry.control = control;
	});

	dialog.show();
}

frappe.ui.form.on('Contractor Invoice Item', {
	contract_item(frm, cdt, cdn) {
		load_item_context(frm, cdt, cdn, true);
	},
	cumulative_qty_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item) return;
		recompute_item_row(frm, row, 'cumulative_qty_complete');
		frm.refresh_field('items');
		refresh_totals_preview(frm);
	},
	percent_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item) return;
		recompute_item_row(frm, row, 'percent_complete');
		frm.refresh_field('items');
		refresh_totals_preview(frm);
	},
	this_period_qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item) return;
		recompute_item_row(frm, row, 'this_period_qty');
		frm.refresh_field('items');
		refresh_totals_preview(frm);
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
		refresh_totals_preview(frm);
	},
	percent_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item || !row.payment_condition) return;
		recompute_condition_row(frm, row, 'percent_complete');
		frm.refresh_field('condition_progress');
		refresh_totals_preview(frm);
	},
	this_period_qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.contract_item || !row.payment_condition) return;
		recompute_condition_row(frm, row, 'this_period_qty');
		frm.refresh_field('condition_progress');
		refresh_totals_preview(frm);
	}
});
