const ALLOCATION = 'contracting.contracting.utils.allocation';
const PROGRESS_INVOICING = 'contracting.contracting.utils.progress_invoicing';
const LINK = 'contracting.contracting.api.link';
const LABOR_TYPES = ['Installing'];

function is_material_type(frm) {
	return frm.doc.type_subcontractor === 'Supplying and installing';
}

function is_labor_type(frm) {
	return LABOR_TYPES.includes(frm.doc.type_subcontractor);
}

function is_equipment_type(frm) {
	return frm.doc.type_subcontractor === 'Equipment';
}

// FR-01, extended 2026-08-30: a project (and so a contract) can be in
// scope of more than one Tender - every picker filters against this list
// instead of a single tender_id.
function project_tenders(frm) {
	return (frm.doc.project_tenders || []).map((row) => row.tender).filter(Boolean);
}

// fieldname on Subcontractor Contract -> [child table doctype, query method, link fieldname]
const RESOURCE_TABLES = {
	contract_material_items: [`${ALLOCATION}.material_item_query`, 'material_item'],
	contract_labor_items: [`${ALLOCATION}.labor_item_query`, 'labor_item'],
	contract_equipment_items: [`${ALLOCATION}.equipment_item_query`, 'equipment_item'],
};

frappe.ui.form.on('Subcontractor Contract', {
	setup(frm) {
		// Only suppliers actually flagged as subcontractors.
		frm.set_query('contractor_name', () => ({
			filters: { custom_is_subcontractor: 1 },
		}));

		// FR-02: work items are scoped to the contract's Project Tenders.
		frm.set_query('work_item', 'contracted_items', () => ({
			query: `${ALLOCATION}.work_item_query`,
			filters: { tenders: project_tenders(frm) },
		}));

		// FR-04/FR-05: resource rows are scoped to the row's work item.
		// Not a plain link filter - resource rows sit in header-level
		// tables on Tender and point at their BOQ row by id/idx.
		frm.set_query('labor_item', 'contracted_items', (doc, cdt, cdn) => ({
			query: `${ALLOCATION}.labor_item_query`,
			filters: { work_item: locals[cdt][cdn].work_item },
		}));

		frm.set_query('equipment_item', 'contracted_items', (doc, cdt, cdn) => ({
			query: `${ALLOCATION}.equipment_item_query`,
			filters: { work_item: locals[cdt][cdn].work_item },
		}));

		// FR-11-19: the three resource tabs - work_item is scoped to the
		// contract's Project Tenders the same way contracted_items' own is,
		// and each resource link is scoped to the row's own work_item the
		// same way labor_item/equipment_item are above.
		Object.keys(RESOURCE_TABLES).forEach((table) => {
			const [query, link_fieldname] = RESOURCE_TABLES[table];

			frm.set_query('work_item', table, () => ({
				query: `${ALLOCATION}.work_item_query`,
				filters: { tenders: project_tenders(frm) },
			}));

			frm.set_query(link_fieldname, table, (doc, cdt, cdn) => ({
				query,
				filters: { work_item: locals[cdt][cdn].work_item },
			}));
		});

		// FR-16: Payment Conditions' work_item picker scoped to what's
		// actually contracted, not every Tender BOQ Item on site. Values
		// come straight off frm.doc (client-side), so this works even on an
		// unsaved contract.
		frm.set_query('work_item', 'payment_conditions', () => ({
			query: `${LINK}.child_row_query`,
			filters: { name: ['in', (frm.doc.contracted_items || []).map((r) => r.work_item).filter(Boolean)] },
		}));

		// FR-13: resource pickers scoped to this contract's own resource-tab
		// rows, further filtered by the row's own work_item if one is set.
		// NOTE: child_row_query is a server-side query against
		// tabContractor Contract Material/Labor/Equipment Item - unlike
		// work_item above, these rows only exist once the contract has been
		// saved at least once, so these 3 pickers are empty on a brand-new
		// unsaved contract until the first save.
		['material_item', 'labor_item', 'equipment_item'].forEach((fieldname) => {
			frm.set_query(fieldname, 'payment_conditions', (doc, cdt, cdn) => {
				const row = locals[cdt][cdn];
				const filters = { parent: frm.doc.name };
				if (row.work_item) filters.work_item = row.work_item;
				return { query: `${LINK}.child_row_query`, filters };
			});
		});
	},

	refresh(frm) {
		frm.toggle_display('items_subcontracted', (frm.doc.items_subcontracted || []).length > 0);
		setup_buttons(frm);
		refresh_payment_condition_category_options(frm);

		// FR-SC-08: dialog-only entry. Direct grid "Add Row" bypasses the
		// dialog's availability/description/item_code lookups and (until
		// save) the work-item-membership check - same pattern already live
		// on tender.js's boq_items grid.
		frm.fields_dict.contracted_items.grid.cannot_add_rows = true;
		frm.fields_dict.contracted_items.grid.setup_toolbar();
		frm.set_df_property('contracted_items', 'read_only', 1, frm.doc.name, 'work_item');
	},

	project(frm) {
		refresh_project_tenders(frm);
	},

	department(frm) {
		// FR-SC-07: same fetch-and-repopulate flow as project(frm), scoped
		// by the (possibly now blank) department - blank restores the
		// unfiltered list.
		refresh_project_tenders(frm);
	},

	type_subcontractor(frm) {
		// The type decides which level of the breakdown rows point at, so
		// existing rows' resource links no longer make sense.
		(frm.doc.contracted_items || []).forEach((row) => {
			frappe.model.set_value(row.doctype, row.name, 'labor_item', null);
			frappe.model.set_value(row.doctype, row.name, 'equipment_item', null);
			frappe.model.set_value(row.doctype, row.name, 'available_qty', 0);
		});

		// FR-15: the resource tabs are keyed off the old type - none of
		// their rows make sense under the new one either.
		Object.keys(RESOURCE_TABLES).forEach((fieldname) => {
			frm.clear_table(fieldname);
			frm.refresh_field(fieldname);
		});

		// FR-SC-10: repopulate the (now-empty) resource tabs for the new
		// type, same as if each surviving row's work_item were just picked.
		(frm.doc.contracted_items || []).forEach((row) => {
			if (row.work_item && flt(row.qty)) {
				propagate_resources(frm, row.work_item, row.qty);
			}
		});

		refresh_payment_condition_category_options(frm);
		setup_buttons(frm);
	},
});

// FR-14: category options restricted to whichever resource tabs the
// server-side truth (LABOR_TYPES/EQUIPMENT/SUPPLY_AND_INSTALL, via these
// same is_material_type/is_labor_type/is_equipment_type helpers) actually
// applies to this contract type - not the JSON depends_on strings, which
// disagree with the server for Labor/Equipment on a Supplying and
// installing contract.
function refresh_payment_condition_category_options(frm) {
	const categories = [];
	if (is_material_type(frm)) categories.push('Material');
	if (is_labor_type(frm)) categories.push('Labor');
	if (is_equipment_type(frm)) categories.push('Equipment');
	frm.fields_dict.payment_conditions.grid.update_docfield_property(
		'category', 'options', [''].concat(categories).join('\n')
	);
}

// FR-01/FR-SC-07: shared by project(frm) and department(frm) - Project
// Tenders follows both the project and, now, the department. Goes through
// the server (not a plain frappe.db.get_value on Project.tender) since
// newer projects only carry their Tender(s) via Project Tender -
// get_project_tenders_for_project resolves both shapes the same way
// set_project_tenders does server-side, and can return more than one Tender.
function refresh_project_tenders(frm) {
	frm.clear_table('project_tenders');
	frm.refresh_field('project_tenders');
	if (!frm.doc.project) {
		setup_buttons(frm);
		return;
	}

	frappe.call({
		method: 'contracting.contracting.doctype.subcontractor_contract.subcontractor_contract.get_project_tenders_for_project',
		args: { project: frm.doc.project, department: frm.doc.department },
	}).then((r) => {
		const tenders = r.message || [];
		if (tenders.length) {
			tenders.forEach((tender) => frm.add_child('project_tenders', { tender }));
			frm.refresh_field('project_tenders');
		} else {
			frappe.msgprint({
				title: __('No Tender on Project'),
				message: frm.doc.department
					? __('Project {0} has no linked Tender in category {1}, so there is no BOQ to contract against.',
						[frm.doc.project, frm.doc.department])
					: __('Project {0} has no linked Tender, so there is no BOQ to contract against.',
						[frm.doc.project]),
				indicator: 'red',
			});
		}
		setup_buttons(frm);
	});
}

// FR-20: cascade-delete. Removing a Contracted Items row orphans any
// Material/Labor/Equipment rows that pointed at the same work item - drop
// them from the grids immediately rather than waiting for save to notice
// (subcontractor_contract.py's prune_orphaned_resource_rows is the
// server-side guarantee for API/Data Import paths this can't reach).
function prune_orphaned_resource_rows(frm) {
	const surviving = new Set((frm.doc.contracted_items || []).map((r) => r.work_item));

	Object.keys(RESOURCE_TABLES).forEach((fieldname) => {
		const rows = frm.doc[fieldname] || [];
		const kept = rows.filter((r) => surviving.has(r.work_item));
		if (kept.length !== rows.length) {
			frm.set_value(fieldname, kept);
			frm.refresh_field(fieldname);
		}
	});
}

// Both the tender and the contract type have to be known before items can
// be pulled, and neither is set when refresh() first runs on a new form -
// so the button is (re)built whenever either changes, not only on refresh.
// clear_custom_buttons() first, or repeated calls stack duplicates.
function setup_buttons(frm) {
	frm.clear_custom_buttons();

	if (project_tenders(frm).length && frm.doc.type_subcontractor && frm.doc.docstatus === 0) {
		frm.add_custom_button(__('Select Work Item'), () => open_work_item_dialog(frm));
	}
}

// Tender BOQ/Labor/Equipment rows are hash-named, so the readable value comes
// from the doctypes' title_field. The link control caches it on selection, so
// this is a local read - never frappe.db.get_value(), which goes through
// frappe.client.get_value and has no parent_doctype escape hatch for child
// doctypes (it PermissionErrors for anyone but Administrator).
function link_title(doctype, name) {
	return name ? frappe.utils.get_link_title(doctype, name) || null : null;
}

// FR-06/FR-09: auto-populate the resource tabs from a work item's own
// Tender-linked resources, instead of requiring them to be re-picked one by
// one. Fires from both the work_item and qty handlers below - a manually
// added row normally gets its work_item picked before its qty is typed, so
// qty is still 0 at that point; firing again on qty (guarded to run only
// once per row, via frm.__propagated_rows) catches that without re-running
// on a later qty edit. The dialog's own add path (render_work_item_dialog)
// already knows both values up front and calls propagate_resources directly.
function propagate_resources(frm, work_item, qty) {
	frappe.call({
		method: `${ALLOCATION}.get_propagated_resource_rows`,
		args: {
			work_item, qty,
			contract_type: frm.doc.type_subcontractor,
			subcontractor_contract: frm.doc.name,
		},
		callback(r) {
			const by_table = r.message || {};
			Object.keys(RESOURCE_TABLES).forEach((fieldname) => {
				(by_table[fieldname] || []).forEach((row) => frm.add_child(fieldname, row));
				frm.refresh_field(fieldname);
			});
		},
	});
}

function maybe_propagate_row(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frm.__propagated_rows = frm.__propagated_rows || new Set();
	if (!row.work_item || !flt(row.qty) || frm.__propagated_rows.has(cdn)) return;

	frm.__propagated_rows.add(cdn);
	propagate_resources(frm, row.work_item, row.qty);
}

frappe.ui.form.on('Contractor Contract Item', {
	work_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, 'labor_item', null);
		frappe.model.set_value(cdt, cdn, 'equipment_item', null);
		frappe.model.set_value(cdt, cdn, 'resource_item', null);

		// validate() re-derives this authoritatively on save; setting it now
		// just keeps the grid readable while the row is being entered.
		frappe.model.set_value(cdt, cdn, 'description', link_title('Tender BOQ Item', row.work_item));

		if (row.work_item) refresh_available_qty(frm, cdt, cdn);
		maybe_propagate_row(frm, cdt, cdn);
	},

	qty(frm, cdt, cdn) {
		set_amount(cdt, cdn);
		maybe_propagate_row(frm, cdt, cdn);
	},

	labor_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, 'resource_item', link_title('Tender Labor Item', row.labor_item));
		refresh_available_qty(frm, cdt, cdn);
	},

	equipment_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, 'resource_item',
			link_title('Tender Equipment Item', row.equipment_item));
		refresh_available_qty(frm, cdt, cdn);
	},

	unit_price(frm, cdt, cdn) {
		set_amount(cdt, cdn);
	},

	contracted_items_remove(frm) {
		prune_orphaned_resource_rows(frm);
		frm.trigger('refresh');
	},
});

// The three new resource tabs share the same work_item -> resource_item /
// available_qty shape as Contractor Contract Item above, just against
// their own doctype and single link fieldname. Labor/Equipment reuse the
// same get_available_qty endpoint contracted_items already calls (their
// contract_type branch in get_row_availability already handles a single
// resource link); Material has no contract_type of its own, so it goes
// straight to get_material_available_qty (step 4).

frappe.ui.form.on('Contractor Contract Labor Item', {
	work_item(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, 'labor_item', null);
		frappe.model.set_value(cdt, cdn, 'resource_item', null);
		frappe.model.set_value(cdt, cdn, 'available_qty', 0);
	},

	labor_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, 'resource_item', link_title('Tender Labor Item', row.labor_item));
		if (!row.labor_item) return;

		frappe.call({
			method: `${ALLOCATION}.get_available_qty`,
			args: {
				contract_type: 'Installing',
				work_item: row.work_item,
				labor_item: row.labor_item,
				subcontractor_contract: frm.doc.name,
			},
			callback(r) {
				if (r.message === undefined || r.message === null) return;
				frappe.model.set_value(cdt, cdn, 'available_qty', r.message);
			},
		});
	},
});

frappe.ui.form.on('Contractor Contract Equipment Item', {
	work_item(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, 'equipment_item', null);
		frappe.model.set_value(cdt, cdn, 'resource_item', null);
		frappe.model.set_value(cdt, cdn, 'available_qty', 0);
	},

	equipment_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, 'resource_item', link_title('Tender Equipment Item', row.equipment_item));
		if (!row.equipment_item) return;

		frappe.call({
			method: `${ALLOCATION}.get_available_qty`,
			args: {
				contract_type: 'Equipment',
				work_item: row.work_item,
				equipment_item: row.equipment_item,
				subcontractor_contract: frm.doc.name,
			},
			callback(r) {
				if (r.message === undefined || r.message === null) return;
				frappe.model.set_value(cdt, cdn, 'available_qty', r.message);
			},
		});
	},
});

frappe.ui.form.on('Contractor Contract Material Item', {
	work_item(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, 'material_item', null);
		frappe.model.set_value(cdt, cdn, 'resource_item', null);
		frappe.model.set_value(cdt, cdn, 'available_qty', 0);
	},

	material_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, 'resource_item', link_title('Tender Material Item', row.material_item));
		if (!row.material_item) return;

		frappe.call({
			method: `${ALLOCATION}.get_material_available_qty`,
			args: {
				material_item: row.material_item,
				subcontractor_contract: frm.doc.name,
			},
			callback(r) {
				if (r.message === undefined || r.message === null) return;
				frappe.model.set_value(cdt, cdn, 'available_qty', r.message);
			},
		});
	},
});

function set_amount(cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, 'amount', flt(row.qty) * flt(row.unit_price));
}

function refresh_available_qty(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row.work_item || !frm.doc.type_subcontractor) return;

	// Labor/equipment types have nothing to show until the resource row is
	// picked - availability is a property of that row, not the work item.
	if (is_labor_type(frm) && !row.labor_item) return;
	if (is_equipment_type(frm) && !row.equipment_item) return;

	frappe.call({
		method: `${ALLOCATION}.get_available_qty`,
		args: {
			contract_type: frm.doc.type_subcontractor,
			work_item: row.work_item,
			labor_item: row.labor_item,
			equipment_item: row.equipment_item,
			subcontractor_contract: frm.doc.name,
		},
		callback(r) {
			if (r.message === undefined || r.message === null) return;
			frappe.model.set_value(cdt, cdn, 'available_qty', r.message);
		},
	});
}

// FR-04-10: search + multi-select in place of the old "add everything"
// button. Availability comes from the existing get_unallocated_boq_items
// (unchanged - it already computes the right row shape); invoiced qty per
// work item comes from the new get_invoiced_qty_for_work_item (FR-07), so
// the picker shows both before anything is added.
function open_work_item_dialog(frm) {
	frappe.call({
		method: 'contracting.contracting.doctype.subcontractor_contract.subcontractor_contract.get_unallocated_boq_items',
		args: {
			tenders: project_tenders(frm),
			contract_type: frm.doc.type_subcontractor,
			subcontractor_contract: frm.doc.name,
		},
		freeze: true,
		callback(r) {
			const rows = r.message || [];
			if (!rows.length) {
				frappe.msgprint(__('Nothing left available across this contract\'s Project Tenders for a {0} contract.',
					[frm.doc.type_subcontractor]));
				return;
			}
			show_work_item_dialog(frm, rows);
		},
	});
}

function show_work_item_dialog(frm, rows) {
	const work_items = [...new Set(rows.map((row) => row.work_item))];

	Promise.all(work_items.map((work_item) => frappe.call({
		method: `${PROGRESS_INVOICING}.get_invoiced_qty_for_work_item`,
		args: { work_item },
	}).then((r) => [work_item, r.message || 0]))).then((pairs) => {
		const invoiced_by_work_item = Object.fromEntries(pairs);
		render_work_item_dialog(frm, rows, invoiced_by_work_item);
	});
}

function render_work_item_dialog(frm, rows, invoiced_by_work_item) {
	const dialog = new frappe.ui.Dialog({
		title: __('Select Work Item'),
		size: 'large',
		fields: [
			{ fieldname: 'search', fieldtype: 'Data', label: __('Search') },
			{ fieldname: 'rows_html', fieldtype: 'HTML' },
		],
		primary_action_label: __('Add Selected'),
		primary_action() {
			const checked = dialog.$wrapper.find('.work-item-row input[type=checkbox]:checked');
			if (!checked.length) {
				frappe.msgprint(__('Select at least one row.'));
				return;
			}

			checked.each(function () {
				const row = rows[$(this).closest('.work-item-row').data('idx')];
				frappe.utils.add_link_title('Tender BOQ Item', row.work_item, row.item_name);
				const child = Object.assign({}, row);
				delete child.item_name;  // not a field on Contractor Contract Item
				delete child.tender;  // display-only, not a field on Contractor Contract Item
				delete child.item_code;  // display-only, not a field on Contractor Contract Item
				delete child.contracted;  // display-only, not a field on Contractor Contract Item
				const added = frm.add_child('contracted_items', child);
				// FR-06: qty is already known here (get_unallocated_boq_items'
				// own availability default), so propagate immediately rather
				// than waiting on the work_item/qty handler guard below.
				frm.__propagated_rows = frm.__propagated_rows || new Set();
				frm.__propagated_rows.add(added.name);
				propagate_resources(frm, row.work_item, row.qty);
			});
			frm.refresh_field('contracted_items');
			dialog.hide();
		},
	});

	function render(filter_text) {
		const term = (filter_text || '').toLowerCase();
		const html = rows.map((row, idx) => {
			// FR-SC-03/04: item_code folded into the label itself, so the
			// existing label match below also matches on it.
			const label = (row.item_code ? `${row.item_code} — ` : '') + (row.description || row.work_item);
			if (term && !label.toLowerCase().includes(term)) return '';
			return `
				<div class="work-item-row" data-idx="${idx}"
					style="display:flex; align-items:center; gap:12px; padding:6px 0; border-bottom:1px solid var(--border-color);">
					<input type="checkbox">
					<span style="flex:2">${frappe.utils.escape_html(label)}</span>
					<span style="flex:1; color:var(--text-muted);">${frappe.utils.escape_html(row.tender || '')}</span>
					<span style="flex:1">${__('Available')}: ${flt(row.available_qty)}</span>
					<span style="flex:1">${__('Contracted')}: ${flt(row.contracted)}</span>
					<span style="flex:1">${__('Invoiced')}: ${flt(invoiced_by_work_item[row.work_item] || 0)}</span>
				</div>
			`;
		}).join('');
		dialog.fields_dict.rows_html.$wrapper.html(html || `<p>${__('No matches.')}</p>`);
	}

	dialog.fields_dict.search.df.onchange = () => render(dialog.get_value('search'));
	render();
	dialog.show();
}
