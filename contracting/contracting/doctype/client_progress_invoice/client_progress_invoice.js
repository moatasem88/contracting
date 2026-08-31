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
						r.message.forEach(row => frm.add_child('items', row));
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
					rows.forEach(row => frm.add_child('items', row));
					frm.refresh_field('items');
				});
			}).addClass('btn-primary');
		}

		apply_sales_order_grouping(frm);

		if (frm.doc.__islocal) return;

		if (frm.doc.status === 'Draft') {
			frm.add_custom_button(__('Mark as Measured'), () => {
				frappe.call({
					method: 'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.mark_as_measured',
					args: {name: frm.doc.name},
					callback() { frm.reload_doc(); }
				});
			});
		}

		if (frm.doc.status === 'Measured') {
			frm.add_custom_button(__('Approve && Generate Sales Invoice'), () => {
				frappe.confirm(
					__('Approve this Client Progress Invoice and generate a Sales Invoice for {0}?', [format_currency(frm.doc.total_this_period)]),
					() => {
						frappe.call({
							method: 'contracting.contracting.doctype.client_progress_invoice.client_progress_invoice.approve_and_invoice',
							args: {name: frm.doc.name},
							callback(r) {
								frm.reload_doc();
								if (r.message) {
									frappe.set_route('sales-invoice', r.message);
								}
							}
						});
					}
				);
			}).addClass('btn-primary');
		}

		if (frm.doc.sales_invoice) {
			frm.add_custom_button(__('Sales Invoice'), () => frappe.set_route('sales-invoice', frm.doc.sales_invoice), __('View'));
		}
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
