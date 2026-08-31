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
	},

	refresh(frm) {
		if (frm.doc.__islocal) return;

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
