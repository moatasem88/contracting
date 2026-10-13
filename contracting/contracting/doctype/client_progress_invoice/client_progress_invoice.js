// 2026-09-27 auto-sync BRD, Part B: live 3-way calculator, ported from
// contractor_invoice.js's recompute_item_row/load_item_context. Formulas
// match contracting.contracting.utils.progress_invoicing.
// compute_progress_invoice_items byte-for-byte - any change to one needs
// the matching change in the other.
const ITEM_CONTEXT_METHOD = 'contracting.contracting.utils.progress_invoicing.get_item_progress_context';
const CPI_ITEMS_ENTRY_FIELDS = ['cumulative_qty_complete', 'percent_complete', 'this_period_qty'];

// Client Progress Invoice Item.percent_complete/this_period_qty are
// read_only:1 in the doctype JSON (only cumulative_qty_complete is hand-
// edited there today) - a genuine 3-way calculator needs all three
// editable client-side. Same runtime-override idiom Contractor Invoice's
// own set_items_entry_readonly already uses; CPI has no condition-table
// toggle scenario to guard against, so this is unconditional.
function unlock_cpi_items_entry_fields(frm) {
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
	if (!grid) return;
	CPI_ITEMS_ENTRY_FIELDS.forEach(fieldname => grid.update_docfield_property(fieldname, 'read_only', 0));
	grid.refresh();
}

// A row's source is whichever of the two read-only link fields "Get
// Items" populated - neither set (a stray manually-added row; the items
// grid is not cannot_add_rows-locked, a known out-of-scope gap) means
// skip, not throw.
function resolve_cpi_row_source_doctype(row) {
	if (row.sales_order_item) return 'Sales Order Item';
	if (row.tender_item) return 'Tender Item';
	return null;
}

function recompute_cpi_item_row(frm, row, source_field) {
	const qty_allocated = flt(row.qty_allocated);
	const prior_max_qty = flt(row.__prior_max_qty);

	if (source_field === 'percent_complete') {
		row.cumulative_qty_complete = qty_allocated ? flt(row.percent_complete) / 100 * qty_allocated : 0;
	} else if (source_field === 'this_period_qty') {
		row.cumulative_qty_complete = prior_max_qty + flt(row.this_period_qty);
	}

	// Clamp to the valid range instead of only alerting - a derived or
	// typed value outside [prior_max_qty, qty_allocated] is pulled back in
	// line before percent/this_period/amounts are derived from it.
	row.cumulative_qty_complete = Math.min(qty_allocated, Math.max(prior_max_qty, flt(row.cumulative_qty_complete)));

	row.percent_complete = qty_allocated ? flt(row.cumulative_qty_complete) / qty_allocated * 100 : 0;
	row.this_period_qty = flt(row.cumulative_qty_complete) - prior_max_qty;

	const rate = flt(row.rate);
	const prior_invoiced_amount = flt(row.__prior_invoiced_amount);
	row.cumulative_amount = flt(row.cumulative_qty_complete) * rate;
	row.previously_invoiced_amount = prior_invoiced_amount;
	row.this_period_amount = row.cumulative_amount - prior_invoiced_amount;
	row.retention_amount = row.this_period_amount * flt(frm.doc.retention_percent) / 100;
	row.net_amount = row.this_period_amount - row.retention_amount;

	// Non-blocking - typing continues, the hard block stays at validate()
	// (save time)/sync_live_edits's own pre-clamp, unchanged.
	if (flt(row.cumulative_qty_complete) > qty_allocated + 1e-6) {
		frappe.show_alert({message: __('Row {0}: cumulative quantity exceeds the allocated quantity.', [row.idx]), indicator: 'orange'});
	} else if (flt(row.cumulative_qty_complete) + 1e-6 < prior_max_qty) {
		frappe.show_alert({message: __('Row {0}: cumulative quantity cannot regress below the previous invoice.', [row.idx]), indicator: 'orange'});
	}
}

function load_cpi_item_context(frm, cdt, cdn, reset) {
	const row = locals[cdt][cdn];
	const source_doctype = resolve_cpi_row_source_doctype(row);
	if (!source_doctype) return;

	if (reset) {
		row.cumulative_qty_complete = 0;
		row.percent_complete = 0;
		row.this_period_qty = 0;
	}

	frappe.call({
		method: ITEM_CONTEXT_METHOD,
		args: {
			contract_item: row.sales_order_item || row.tender_item,
			own_doctype: 'Client Progress Invoice',
			doc_name: frm.doc.name,
			source_doctype: source_doctype,
		},
		callback(r) {
			const ctx = r.message || {};
			row.qty_allocated = flt(ctx.qty_allocated);
			row.rate = flt(ctx.rate);
			row.__prior_max_qty = flt(ctx.prior_max_qty);
			row.__prior_invoiced_amount = flt(ctx.prior_invoiced_amount);
			if (reset) row.cumulative_qty_complete = row.__prior_max_qty;
			recompute_cpi_item_row(frm, row, 'cumulative_qty_complete');
			frm.refresh_field('items');
		}
	});
}

// 2026-09-27 auto-sync BRD, Part A: background sync engine, direct port of
// tender_expand_view.js's throttle/idle-checkpoint/self-echo-suppression
// idiom - CPI has one child table needing it (items / Client Progress
// Invoice Item) instead of Tender's four, so no per-doctype forEach.
const AUTO_SYNC_SAVE_INTERVAL_MS = 2000;
const IDLE_CHECKPOINT_SAVE_MS = 8000;
const SYNC_LIVE_EDITS_METHOD =
	'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.sync_live_edits';

function checkpoint_save(frm) {
	if (!frm.is_new() && frm.is_dirty() && !frappe.ui.form.is_saving) frm.save();
}

frappe.ui.form.on('Client Progress Invoice', {
	// Same reasoning as Contractor Invoice: the legacy Contract Document
	// path claims against that document's own lines and nothing else.
	setup(frm) {
		frm.set_query('tender_item', 'items', () => ({
			filters: {
				parenttype: 'Contract Document',
				parent: frm.doc.contract_document || ''
			}
		}));

		// progress_sales_orders is system-managed (always mirrors the live
		// Project/Invoice Category match, recomputed server-side on every
		// save too) - no add/delete affordance should even appear.
		const grid = frm.fields_dict.progress_sales_orders && frm.fields_dict.progress_sales_orders.grid;
		if (grid) {
			grid.cannot_add_rows = true;
			grid.cannot_delete_rows = true;
		}

		unlock_cpi_items_entry_fields(frm);

		// Auto-sync: push edited fields to the server roughly every 2s
		// while cell edits keep coming in - see tender_expand_view.js for
		// the full reasoning (frm.save() hardcodes freeze:true and a full
		// frm.refresh(), which is what this avoids). sync_live_edits
		// persists only the touched rows' fields directly and
		// re-broadcasts doc_update the same way a real save would - cross-
		// session sync is unchanged, but nothing freezes or re-renders the
		// form on this path. A real frm.save() (freeze included, but now
		// rare rather than continuous) still runs at natural pause points
		// via the idle timeout below.
		let idle_checkpoint = null;
		let sync_save = frappe.utils.throttle(() => {
			if (frm.is_new()) return;
			// Suppress core's realtime doc_update handler from reacting to
			// this call's own broadcast - mirrors the guard a real
			// frm.save() already sets around its own request/response cycle.
			frappe.ui.form.is_saving = true;
			frappe.call({
				method: SYNC_LIVE_EDITS_METHOD,
				args: {client_progress_invoice: frm.doc},
				freeze: false,
				callback(r) {
					if (!r.message || !Object.keys(r.message).length) return;
					// Direct assignment only - a frappe.model.set_value here
					// would re-fire this same wildcard listener.
					frm.doc.modified = r.message.modified;
					frm.doc.total_this_period = r.message.total_this_period;
					frm.doc.total_retention_held = r.message.total_retention_held;
					frm.doc.net_receivable = r.message.net_receivable;
				},
				always() {
					frappe.ui.form.is_saving = false;
				}
			});
		}, AUTO_SYNC_SAVE_INTERVAL_MS);

		frappe.model.on('Client Progress Invoice Item', '*', (fieldname, value, doc) => {
			if (doc.parent !== frm.docname) return;
			sync_save();
			clearTimeout(idle_checkpoint);
			idle_checkpoint = setTimeout(() => checkpoint_save(frm), IDLE_CHECKPOINT_SAVE_MS);
		});
	},

	// Picking the Sales Order does not re-run refresh, so without this the
	// Get Items button never appears on a new invoice - which leaves the
	// user trying to fill the rows by hand against a link picker that
	// cannot search a child doctype.
	sales_order(frm) {
		frm.trigger('refresh');
	},

	project(frm) {
		frm.set_value('invoice_category', null);
		frm.trigger('maybe_fetch_project_sales_orders');
	},

	invoice_category(frm) {
		frm.trigger('maybe_fetch_project_sales_orders');
	},

	maybe_fetch_project_sales_orders(frm) {
		if (!frm.doc.project || !frm.doc.invoice_category) return;
		frappe.call({
			method: 'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.get_project_sales_orders',
			args: {project: frm.doc.project, invoice_category: frm.doc.invoice_category},
			callback(r) {
				frm.clear_table('progress_sales_orders');
				(r.message || []).forEach(row => frm.add_child('progress_sales_orders', row));
				frm.refresh_field('progress_sales_orders');
				if (!r.message || !r.message.length) {
					frappe.msgprint(__('No Sales Orders found for this Project/Invoice Category.'));
				}
				frm.trigger('refresh');
			}
		});
	},

	refresh(frm) {
		if (frm.doc.sales_order && !frm.doc.project && (frm.doc.status === 'Draft' || frm.doc.__islocal)) {
			frm.add_custom_button(__('Get Items from Sales Order'), () => {
				frappe.call({
					method: 'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.get_sales_order_items',
					args: {sales_order: frm.doc.sales_order, client_progress_invoice: frm.doc.name},
					callback(r) {
						if (!r.message || !r.message.length) {
							frappe.msgprint(__('No submitted lines found on {0}.', [frm.doc.sales_order]));
							return;
						}
						frm.clear_table('items');
						r.message.forEach(row => {
							const new_row = frm.add_child('items', row);
							new_row.__prior_max_qty = flt(row.prior_max_qty);
							new_row.__prior_invoiced_amount = flt(row.previously_invoiced_amount);
							recompute_cpi_item_row(frm, new_row, 'cumulative_qty_complete');
						});
						frm.refresh_field('items');
					}
				});
			}).addClass('btn-primary');
		}

		if (frm.doc.project && frm.doc.invoice_category && (frm.doc.progress_sales_orders || []).length
			&& (frm.doc.status === 'Draft' || frm.doc.__islocal)) {
			frm.add_custom_button(__('Get Items from Sales Order(s)'), () => {
				const calls = (frm.doc.progress_sales_orders || []).map(row => new Promise(resolve => {
					frappe.call({
						method: 'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.get_sales_order_items',
						args: {sales_order: row.sales_order, client_progress_invoice: frm.doc.name},
						callback(r) { resolve(r.message || []); }
					});
				}));
				Promise.all(calls).then(results => {
					const rows = [].concat(...results);
					if (!rows.length) {
						frappe.msgprint(__('No submitted lines found on the listed Sales Orders.'));
						return;
					}
					frm.clear_table('items');
					rows.forEach(row => {
						const new_row = frm.add_child('items', row);
						new_row.__prior_max_qty = flt(row.prior_max_qty);
						new_row.__prior_invoiced_amount = flt(row.previously_invoiced_amount);
						recompute_cpi_item_row(frm, new_row, 'cumulative_qty_complete');
					});
					frm.refresh_field('items');
				});
			}).addClass('btn-primary');
		}

		apply_sales_order_grouping(frm);

		if (frm.doc.__islocal) return;

		(frm.doc.items || []).forEach(row => load_cpi_item_context(frm, row.doctype, row.name, false));

		if (frm.doc.status === 'Draft') {
			frm.add_custom_button(__('Mark as Measured'), () => {
				frappe.call({
					method: 'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.mark_as_measured',
					args: {name: frm.doc.name},
					callback() { frm.reload_doc(); }
				});
			});
		}

		if (frm.doc.docstatus === 1 && (frm.doc.sales_order || frm.doc.project)) {
			frm.add_custom_button(__('Sales Invoice'), () => {
				frappe.call({
					method: 'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.create_sales_invoice_from_backlog',
					args: {name: frm.doc.name},
					callback(r) {
						if (r.message) {
							frm.reload_doc();
							frappe.set_route('sales-invoice', r.message);
						}
						// no r.message: nothing outstanding - server already
						// showed the no-op msgprint, nothing more to do here.
					}
				});
			}, __('Create'));
		}

		if (frm.doc.docstatus === 1 && !frm.doc.delivery_note) {
			frm.add_custom_button(__('Delivery Note'), () => {
				frappe.call({
					method: 'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.create_delivery_note',
					args: {name: frm.doc.name},
					callback(r) {
						frm.reload_doc();
						if (r.message) {
							frappe.set_route('delivery-note', r.message);
						}
					}
				});
			}, __('Create'));
		}

		// FR-14: sales_invoice is no longer a 1:1 authoritative link once
		// billing is a cross-CPI backlog sweep - a filtered list is the
		// honest view of "every Sales Invoice actually touching this CPI's
		// lines" instead of trusting that single field.
		const so_details = (frm.doc.items || []).map(row => row.sales_order_item).filter(Boolean);
		if (so_details.length) {
			frm.add_custom_button(__('View Sales Invoices'), () => {
				frappe.set_route('List', 'Sales Invoice', {
					'Sales Invoice Item.so_detail': ['in', so_details]
				});
			}, __('View'));
		}
	}
});

frappe.ui.form.on('Client Progress Invoice Item', {
	cumulative_qty_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!resolve_cpi_row_source_doctype(row)) return;
		recompute_cpi_item_row(frm, row, 'cumulative_qty_complete');
		frm.refresh_field('items');
	},
	percent_complete(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!resolve_cpi_row_source_doctype(row)) return;
		recompute_cpi_item_row(frm, row, 'percent_complete');
		frm.refresh_field('items');
	},
	this_period_qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!resolve_cpi_row_source_doctype(row)) return;
		recompute_cpi_item_row(frm, row, 'this_period_qty');
		frm.refresh_field('items');
	}
});

// FR-04/FR-05: group the items grid by Sales Order for project-mode claims.
// No existing DOM-row-injection precedent in this app to build on (tender.js's
// apply_indent_styling only restyles existing rows) - this is new. The
// install-once wrap-grid.refresh idiom below mirrors tender.js's
// make_grid_filter_toggle, which solves the same "survive grid.refresh()"
// problem for a different feature.
function apply_sales_order_grouping(frm) {
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
	if (!grid) return;
	install_grouping_refresh_hook(grid, frm);
	render_sales_order_headers(frm, grid);
}

function install_grouping_refresh_hook(grid, frm) {
	if (grid.__grouping_installed) return;
	grid.__grouping_installed = true;

	const original_refresh = grid.refresh.bind(grid);
	grid.refresh = function() {
		original_refresh();
		render_sales_order_headers(frm, grid);
	};
}

function render_sales_order_headers(frm, grid) {
	grid.wrapper.find('.cpi-so-header-row').remove();
	if (!frm.doc.project) return;

	const so_meta = {};
	(frm.doc.progress_sales_orders || []).forEach(row => { so_meta[row.sales_order] = row; });

	let last_so = null;
	grid.wrapper.find('.grid-row').each(function() {
		const $row = $(this);
		// A row hidden by the grid's own filter row shouldn't keep its
		// group header visible either - satisfies the §10 edge case for
		// free, with no separate filter-change handling needed.
		if ($row.is(':hidden')) return;

		const idx = parseInt($row.attr('data-idx'), 10) - 1;
		const row = (frm.doc.items || [])[idx];
		if (!row || !row.sales_order) return;

		if (row.sales_order !== last_so) {
			const meta = so_meta[row.sales_order] || {};
			const label = __('Sales Order: {0} — Tender: {1} ({2}) — Total: {3}', [
				row.sales_order,
				meta.tender || '',
				meta.tender_category || '',
				format_currency(meta.grand_total || 0)
			]);
			$('<div class="cpi-so-header-row">').text(label).css({
				'font-weight': 'bold',
				padding: '6px 8px',
				background: 'var(--control-bg)',
				'border-top': '1px solid var(--border-color)'
			}).insertBefore($row);
			last_so = row.sales_order;
		}
	});
}
