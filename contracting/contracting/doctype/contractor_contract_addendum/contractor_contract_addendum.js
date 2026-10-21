const ADDENDUM_ALLOCATION = 'contracting.contracting.utils.allocation';

frappe.ui.form.on('Contractor Contract Addendum', {
	setup(frm) {
		// Only Approved, open contracts can be amended; narrowed to the
		// chosen Project when one is set.
		frm.set_query('subcontractor_contract', () => ({
			filters: Object.assign(
				{docstatus: 1, workflow_state: 'Approved', is_closed: 0},
				frm.doc.project ? {project: frm.doc.project} : {}
			),
		}));

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

	refresh(frm) {
		// 2026-10-18 BRD FR-14/30: shared Expand View (public/js/expand_view.js).
		contracting.expand_view.attach(frm, [
			{fieldname: 'addendum_items', doctype: 'Contractor Contract Addendum Item'},
			{fieldname: 'addendum_payment_conditions', doctype: 'Contractor Contract Addendum Payment Condition'},
		]);

		const grid = frm.fields_dict.addendum_payment_conditions && frm.fields_dict.addendum_payment_conditions.grid;
		if (grid && is_addendum_draft(frm)) {
			grid.add_custom_button(__('Load Contract Conditions'), () => load_contract_conditions(frm));
		}
	},

	project(frm) {
		if (!frm.doc.subcontractor_contract || !frm.doc.project) return;
		frappe.db.get_value('Subcontractor Contract', frm.doc.subcontractor_contract, 'project').then((r) => {
			if ((r.message || {}).project !== frm.doc.project) frm.set_value('subcontractor_contract', null);
		});
	},

	subcontractor_contract(frm) {
		// Load the base contract's rows (FR-20) and populate Project Tenders
		// as soon as it is chosen, rather than waiting for the first save -
		// validate() would fill the tenders in either way, but the
		// work_item picker needs them sooner.
		load_base_items(frm);
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
	addendum_items_add(frm, cdt, cdn) {
		// A row added by hand is a new work item (the contract's own rows
		// arrive pre-loaded, with base_contract_item set).
		frappe.model.set_value(cdt, cdn, 'action', 'Add');
	},

	update_row(frm, cdt, cdn) {
		open_update_dialog(frm, locals[cdt][cdn]);
	},

	delete_row(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.base_contract_item) {
			frappe.msgprint(__('Only rows of the contract can be marked for deletion. Remove an added row with the grid\'s own delete.'));
			return;
		}
		if (!is_addendum_draft(frm)) return;
		// Delete is a toggle: pressing it again on a marked row undoes it.
		set_row_action(frm, row, row.action === 'Delete' ? '' : 'Delete');
	},

	work_item_template(frm, cdt, cdn) {
		if (locals[cdt][cdn].work_item_template) frappe.model.set_value(cdt, cdn, 'work_item', null);
	},

	work_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.work_item) frappe.model.set_value(cdt, cdn, 'work_item_template', null);
		frappe.model.set_value(cdt, cdn, 'labor_item', null);
		frappe.model.set_value(cdt, cdn, 'equipment_item', null);
		frappe.model.set_value(cdt, cdn, 'resource_item', null);
		frappe.model.set_value(cdt, cdn, 'resource_lines', null);
		frappe.model.set_value(cdt, cdn, 'available_qty', 0);
		// 2026-10-20 (partial contracts): an added work item takes only the
		// lines picked here - e.g. the subcontract line the base contract
		// doesn't carry yet.
		if (row.work_item && row.action === 'Add' && !row.base_contract_item) open_addendum_lines_dialog(frm, row);
	},

	pick_lines(frm, cdt, cdn) {
		open_addendum_lines_dialog(frm, locals[cdt][cdn]);
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
	if (row.base_contract_item) return;  // computed server-side from the base row
	frappe.model.set_value(cdt, cdn, 'amount', flt(row.qty_delta) * flt(row.unit_price));
}

function refresh_available_qty(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row.work_item || !frm.doc.type_subcontractor) return;
	// Single-resource types show availability only once a resource is
	// named; Installing's Subcontract pool has no picker on addendum rows.
	if (['Labor', 'Installing'].includes(frm.doc.type_subcontractor) && !row.labor_item) return;
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

// ---------------------------------------------------------------------------
// 2026-10-18 BRD: amend an Approved contract - load its rows, Update / Delete
// per row, add new rows, edit payment conditions. Nothing is written to the
// contract until final approval; the server recomputes old values, invoiced
// qty, delta and amount on every save, so what is shown here is a preview.
// ---------------------------------------------------------------------------

function is_addendum_draft(frm) {
	return frm.doc.docstatus === 0 && ['', 'Draft'].includes(frm.doc.workflow_state || '');
}

function load_base_items(frm) {
	// Replace the loaded (base) rows; rows the user added themselves stay.
	const kept = (frm.doc.addendum_items || []).filter((row) => !row.base_contract_item);
	frm.clear_table('addendum_items');
	kept.forEach((row) => frm.add_child('addendum_items', row));
	frm.clear_table('addendum_payment_conditions');
	frm.refresh_field('addendum_payment_conditions');

	if (!frm.doc.subcontractor_contract) {
		frm.refresh_field('addendum_items');
		return;
	}
	frappe.call({
		method: 'contracting.contracting.doctype.contractor_contract_addendum.contractor_contract_addendum.get_contract_items_for_addendum',
		args: {subcontractor_contract: frm.doc.subcontractor_contract},
		callback(r) {
			const rows = r.message || [];
			rows.forEach((row) => frm.add_child('addendum_items', row));
			frm.refresh_field('addendum_items');
			if (!rows.length) {
				frappe.show_alert({message: __('This contract has no items yet. Add new rows below.'), indicator: 'orange'});
			}
		}
	});
}

function set_row_action(frm, row, action) {
	const values = {action};
	if (!action) {
		// Back to unchanged: the preview figures return to the base values.
		Object.assign(values, {
			new_qty: row.old_qty, new_unit_price: row.old_unit_price,
			qty_delta: 0, unit_price: row.old_unit_price, amount: 0,
		});
	} else if (action === 'Delete') {
		Object.assign(values, {
			new_qty: 0, new_unit_price: 0,
			qty_delta: -flt(row.old_qty), unit_price: row.old_unit_price,
			amount: -flt(row.old_qty) * flt(row.old_unit_price),
		});
	}
	frappe.model.set_value(row.doctype, row.name, values).then(() => frm.refresh_field('addendum_items'));
}

function open_update_dialog(frm, row) {
	if (!is_addendum_draft(frm)) return;
	if (!row.base_contract_item) {
		frappe.msgprint(__('Added rows are edited directly. Update applies to the contract\'s existing rows.'));
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: __('Update {0}', [row.description || row.work_item || row.work_item_template || row.base_contract_item]),
		fields: [
			{fieldname: 'old', fieldtype: 'HTML', options: `<p class="text-muted">${__('Current')}: ${flt(row.old_qty)} × ${format_currency(row.old_unit_price)}
				&nbsp;·&nbsp; ${__('Invoiced Qty')}: ${flt(row.invoiced_qty)}</p>`},
			{fieldname: 'new_qty', fieldtype: 'Float', label: __('New Qty'), reqd: 1,
				default: row.action === 'Update' ? row.new_qty : row.old_qty},
			{fieldname: 'new_unit_price', fieldtype: 'Currency', label: __('New Unit Price'), reqd: 1,
				default: row.action === 'Update' ? row.new_unit_price : row.old_unit_price},
		],
		primary_action_label: __('Confirm'),
		primary_action(values) {
			if (flt(values.new_qty) < flt(row.invoiced_qty)) {
				frappe.msgprint(__('New Qty cannot be below the {0} already invoiced.', [flt(row.invoiced_qty)]));
				return;
			}
			frappe.model.set_value(row.doctype, row.name, {
				action: 'Update',
				new_qty: values.new_qty,
				new_unit_price: values.new_unit_price,
				qty_delta: flt(values.new_qty) - flt(row.old_qty),
				unit_price: values.new_unit_price,
				amount: flt(values.new_qty) * flt(values.new_unit_price) - flt(row.old_qty) * flt(row.old_unit_price),
			}).then(() => {
				frm.refresh_field('addendum_items');
				dialog.hide();
			});
		},
		secondary_action_label: __('Undo Update'),
		secondary_action() {
			set_row_action(frm, row, '');
			dialog.hide();
		}
	});
	dialog.show();
}

function load_contract_conditions(frm) {
	if (!frm.doc.subcontractor_contract) {
		frappe.msgprint(__('Select the Subcontractor Contract first.'));
		return;
	}
	frappe.call({
		method: 'contracting.contracting.doctype.contractor_contract_addendum.contractor_contract_addendum.get_contract_payment_conditions',
		args: {subcontractor_contract: frm.doc.subcontractor_contract},
		callback(r) {
			const loaded = new Set((frm.doc.addendum_payment_conditions || []).map((row) => row.base_condition).filter(Boolean));
			const rows = (r.message || []).filter((row) => !loaded.has(row.base_condition));
			rows.forEach((row) => frm.add_child('addendum_payment_conditions', row));
			frm.refresh_field('addendum_payment_conditions');
			frappe.show_alert({
				message: rows.length
					? __('Loaded {0} condition(s). Edit the ones you want to change; rows left as loaded change nothing.', [rows.length])
					: __('No more conditions to load.'),
				indicator: 'blue'
			});
		}
	});
}

// 2026-10-20 (partial contracts): which of the work item's lines an Add row
// takes onto the base contract - only those it doesn't carry yet are
// offered. Leaving every box unticked takes them all (the old behaviour).
const ADDENDUM_LINE_LABELS = {
	contract_material_items: __('Material'),
	contract_labor_items: __('Labor'),
	contract_subcontract_items: __('Subcontract'),
	contract_equipment_items: __('Equipment'),
};

function open_addendum_lines_dialog(frm, row) {
	if (!frm.doc.subcontractor_contract || !row.work_item) return;
	// Link validation re-fires the work_item event - one picker per row.
	frm.__lines_dialog_open = frm.__lines_dialog_open || {};
	if (frm.__lines_dialog_open[row.name]) return;
	frm.__lines_dialog_open[row.name] = true;
	const release = () => { frm.__lines_dialog_open[row.name] = false; };
	frappe.call({
		method: 'contracting.contracting.doctype.contractor_contract_addendum.contractor_contract_addendum.get_addable_lines',
		args: { subcontractor_contract: frm.doc.subcontractor_contract, work_item: row.work_item },
		callback(r) {
			const lines = r.message || [];
			if (!lines.length) {
				release();
				frappe.msgprint(__('Contract {0} already carries every line of this work item - use Update on its row instead.',
					[frm.doc.subcontractor_contract]));
				return;
			}
			const current = new Set(row.resource_lines ? JSON.parse(row.resource_lines) : []);
			const dialog = new frappe.ui.Dialog({
				title: __('Pick Lines'),
				fields: [{
					fieldname: 'lines', fieldtype: 'MultiCheck', columns: 1,
					options: lines.map((line) => ({
						label: `${ADDENDUM_LINE_LABELS[line.table] || line.table}: ${line.item}`
							+ ` (${__('Available')} ${flt(line.available_qty)} / ${flt(line.budget)}, ${__('Per Unit')} ${flt(line.ratio)})`,
						value: line.name,
						checked: current.has(line.name),
						disabled: flt(line.available_qty) <= 0,
					})),
				}],
				primary_action_label: __('Set Lines'),
				primary_action(values) {
					const picked = values.lines || [];
					frappe.model.set_value(row.doctype, row.name, 'resource_lines', picked.length ? JSON.stringify(picked) : null);
					frappe.model.set_value(row.doctype, row.name, 'resource_item',
						lines.filter((line) => picked.includes(line.name)).map((line) => line.item).join(', ') || null);
					dialog.hide();
				},
			});
			dialog.onhide = release;
			dialog.show();
		},
		error: release,
	});
}
