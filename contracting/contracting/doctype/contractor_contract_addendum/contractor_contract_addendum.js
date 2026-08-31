const ADDENDUM_ALLOCATION = 'contracting.contracting.utils.allocation';

frappe.ui.form.on('Contractor Contract Addendum', {
	setup(frm) {
		// FR-37: scoped to the base contract's own Project Tenders (there
		// can be more than one - FR-01 extended 2026-08-30), same as the
		// base contract itself scopes contracted_items.
		frm.set_query('work_item', 'addendum_items', () => ({
			query: `${ADDENDUM_ALLOCATION}.work_item_query`,
			filters: { tenders: (frm.doc.project_tenders || []).map((row) => row.tender).filter(Boolean) },
		}));

		frm.set_query('labor_item', 'addendum_items', (doc, cdt, cdn) => ({
			query: `${ADDENDUM_ALLOCATION}.labor_item_query`,
			filters: { work_item: locals[cdt][cdn].work_item },
		}));

		frm.set_query('equipment_item', 'addendum_items', (doc, cdt, cdn) => ({
			query: `${ADDENDUM_ALLOCATION}.equipment_item_query`,
			filters: { work_item: locals[cdt][cdn].work_item },
		}));
	},

	subcontractor_contract(frm) {
		// Populate Project Tenders as soon as the base contract is chosen,
		// rather than waiting for the first save - validate() would fill
		// it in either way, but the work_item picker needs it sooner.
		frm.clear_table('project_tenders');
		frm.refresh_field('project_tenders');
		if (!frm.doc.subcontractor_contract) return;

		frappe.call({
			method: 'contracting.contracting.doctype.contractor_contract_addendum.contractor_contract_addendum.get_project_tenders_for_contract',
			args: { subcontractor_contract: frm.doc.subcontractor_contract },
		}).then((r) => {
			(r.message || []).forEach((tender) => frm.add_child('project_tenders', { tender }));
			frm.refresh_field('project_tenders');
		});
	},
});

function link_title(doctype, name) {
	return name ? frappe.utils.get_link_title(doctype, name) || null : null;
}

frappe.ui.form.on('Contractor Contract Addendum Item', {
	work_item(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, 'labor_item', null);
		frappe.model.set_value(cdt, cdn, 'equipment_item', null);
		frappe.model.set_value(cdt, cdn, 'resource_item', null);
		frappe.model.set_value(cdt, cdn, 'available_qty', 0);
	},

	labor_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, 'resource_item', link_title('Tender Labor Item', row.labor_item));
		refresh_available_qty(frm, cdt, cdn);
	},

	equipment_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, 'resource_item', link_title('Tender Equipment Item', row.equipment_item));
		refresh_available_qty(frm, cdt, cdn);
	},

	qty_delta(frm, cdt, cdn) {
		set_amount(cdt, cdn);
	},

	unit_price(frm, cdt, cdn) {
		set_amount(cdt, cdn);
	},
});

function set_amount(cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, 'amount', flt(row.qty_delta) * flt(row.unit_price));
}

function refresh_available_qty(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row.work_item || !frm.doc.type_subcontractor) return;
	if (frm.doc.type_subcontractor === 'Installing' && !row.labor_item) return;
	if (frm.doc.type_subcontractor === 'Equipment' && !row.equipment_item) return;

	// FR-37: the base contract's own commitment is real and still stands
	// (the addendum only adds new rows to it on approval, never replaces
	// its existing ones), so no exclude_contract here - an addendum's
	// delta has to compete for whatever is left after that commitment,
	// same as any other new claim would.
	frappe.call({
		method: `${ADDENDUM_ALLOCATION}.get_available_qty`,
		args: {
			contract_type: frm.doc.type_subcontractor,
			work_item: row.work_item,
			labor_item: row.labor_item,
			equipment_item: row.equipment_item,
		},
		callback(r) {
			if (r.message === undefined || r.message === null) return;
			frappe.model.set_value(cdt, cdn, 'available_qty', r.message);
		},
	});
}
