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

// fieldname -> [the Tender-side resource doctype the link_fieldname above
// points at, the Contractor Contract-side child doctype the row itself is].
// Used only to prime frappe.utils' link-title cache for rows created via
// propagate_resources() below - see its own comment for why that's needed.
const RESOURCE_TITLE_DOCTYPES = {
	contract_material_items: ['Tender Material Item', 'Contractor Contract Material Item'],
	contract_labor_items: ['Tender Labor Item', 'Contractor Contract Labor Item'],
	contract_equipment_items: ['Tender Equipment Item', 'Contractor Contract Equipment Item'],
};

// Follows Tender BRD (2026-10-13): a contract line is either a Tender BOQ
// line (work_item) or a Work Item Template line (work_item_template). Every
// lookup that used to key on row.work_item keys on this instead - keying on
// work_item alone makes every template row match every other one.
// Mirrors allocation.line_key server-side.
function line_key(row) {
	if (row.work_item) return `t:${row.work_item}`;
	if (row.work_item_template) return `w:${row.work_item_template}`;
	return null;
}

function contract_tender_lines(frm) {
	return [...new Set((frm.doc.contracted_items || []).map((r) => r.work_item).filter(Boolean))];
}

function contract_template_lines(frm) {
	return [...new Set((frm.doc.contracted_items || []).map((r) => r.work_item_template).filter(Boolean))];
}

// FR-08: per-tab Item rule for template-sourced and extra rows - same rules
// as Work Item Template V2's own pickers; allocation.validate_resource_item
// enforces them server-side.
const ITEM_FILTERS = {
	contract_material_items: { is_stock_item: 1 },
	contract_labor_items: { item_group: 'Labor' },
	contract_equipment_items: { item_group: 'Equipment' },
};

const TEMPLATE_DOCTYPE = 'Work Item Template V2';

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

			// Follows Tender BRD FR-08: a resource row belongs to one of this
			// contract's own lines - a Tender line or a template line - not to
			// any BOQ row of the project (a row on a non-line work item would
			// otherwise be cascade-pruned on save).
			frm.set_query('work_item', table, () => ({
				query: `${LINK}.child_row_query`,
				filters: { name: ['in', contract_tender_lines(frm)] },
			}));

			frm.set_query('work_item_template', table, () => ({
				filters: { name: ['in', contract_template_lines(frm)] },
			}));

			frm.set_query('item', table, () => ({ filters: ITEM_FILTERS[table] }));

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
			filters: { name: ['in', contract_tender_lines(frm)] },
		}));

		// FR-16: a condition can target a template line the same way.
		frm.set_query('work_item_template', 'payment_conditions', () => ({
			filters: { name: ['in', contract_template_lines(frm)] },
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
				if (row.work_item_template) filters.work_item_template = row.work_item_template;
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

		// FR-03/04: the switch only moves on a project that has a Tender,
		// and only while there are no lines yet.
		frm.toggle_enable('follows_tender',
			frm.doc.docstatus === 0 && project_tenders(frm).length > 0 && !(frm.doc.contracted_items || []).length);

		paint_out_of_scope(frm);
	},

	// FR-04: revert a change made while lines exist (server rejects it too).
	follows_tender(frm) {
		if (frm.__reverting_follows_tender) return;
		if ((frm.doc.contracted_items || []).length) {
			frm.__reverting_follows_tender = true;
			frm.set_value('follows_tender', frm.doc.follows_tender ? 0 : 1).then(() => {
				frm.__reverting_follows_tender = false;
			});
			frappe.msgprint(__('Remove all Contracted Items before changing Follows Tender.'));
			return;
		}
		setup_buttons(frm);
	},

	// FR-14: Cost Control has to confirm, row by row, before approving a
	// contract that goes beyond the original Tender. workflow.js freezes the
	// screen *before* triggering this and only unfreezes on success, so the
	// dialog has to unfreeze first, and Cancel has to leave it unfrozen.
	before_workflow_action(frm) {
		if (frm.selected_workflow_action !== 'Approve'
			|| frm.doc.workflow_state !== 'Pending Cost Control Approval'
			|| !frm.doc.has_out_of_tender_items) {
			return;
		}

		return new Promise((resolve) => {
			frappe.dom.unfreeze();
			const rows = out_of_scope_rows(frm).map((r) => `
				<tr><td>${frappe.utils.escape_html(r.tab)}</td><td>${r.idx}</td>
				<td>${frappe.utils.escape_html(r.label || '')}</td><td>${frappe.utils.escape_html(r.reason)}</td></tr>`).join('');
			let settled = false;
			const dialog = new frappe.ui.Dialog({
				title: __('Items Outside the Original Tender'),
				size: 'large',
				fields: [{
					fieldtype: 'HTML',
					options: `
						<table class="table table-bordered table-sm">
							<thead><tr><th>${__('Tab')}</th><th>${__('Row')}</th><th>${__('Description / Item')}</th><th>${__('Reason')}</th></tr></thead>
							<tbody>${rows}</tbody>
						</table>
						<p class="text-danger"><b>${__('Approving will send this contract to CEO approval.')}</b></p>`,
				}],
				primary_action_label: __('Continue'),
				primary_action() {
					settled = true;
					dialog.hide();
					frappe.dom.freeze();
					resolve();
				},
				secondary_action_label: __('Cancel'),
				secondary_action() {
					dialog.hide();
				},
			});
			// Cancel leaves the promise unsettled: workflow.js has no catch on
			// this chain, so rejecting would only surface as an "Uncaught (in
			// promise)" console error. Unsettled, apply_workflow simply never
			// runs.
			dialog.onhide = () => {
				if (settled) return;
				settled = true;
				frappe.dom.unfreeze();
				frm.selected_workflow_action = null;
			};
			dialog.show();
		});
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
			// Forget that these work items were ever propagated too -
			// otherwise propagate_resources (below) sees the tables just
			// cleared next, finds no *current* row, but still sees this flag
			// from before the clear and treats the rebuild as a no-op
			// rescale instead of a fresh create.
			frappe.model.set_value(row.doctype, row.name, 'resources_propagated', 0);
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
			if (line_key(row) && flt(row.qty)) {
				propagate_resources(frm, row, row.qty);
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
		const has_lines = (frm.doc.contracted_items || []).length > 0;
		if (tenders.length) {
			tenders.forEach((tender) => frm.add_child('project_tenders', { tender }));
			frm.refresh_field('project_tenders');
			// FR-01: back to the default once a Tender is in scope - unless
			// lines already pin the mode (FR-04).
			if (!has_lines && !frm.doc.follows_tender) frm.set_value('follows_tender', 1);
		} else {
			// FR-03: not an error any more - lines come from templates.
			if (frm.doc.follows_tender && !has_lines) frm.set_value('follows_tender', 0);
			frappe.msgprint({
				title: __('No Tender on Project'),
				message: __('This project has no Tender. Lines are picked from Work Item Templates.'),
				indicator: 'blue',
			});
		}
		frm.toggle_enable('follows_tender', tenders.length > 0 && !has_lines);
		setup_buttons(frm);
	});
}

// FR-20: cascade-delete. Removing a Contracted Items row orphans any
// Material/Labor/Equipment rows that pointed at the same work item - drop
// them from the grids immediately rather than waiting for save to notice
// (subcontractor_contract.py's prune_orphaned_resource_rows is the
// server-side guarantee for API/Data Import paths this can't reach).
function prune_orphaned_resource_rows(frm) {
	const surviving = new Set((frm.doc.contracted_items || []).map(line_key).filter(Boolean));

	Object.keys(RESOURCE_TABLES).forEach((fieldname) => {
		const rows = frm.doc[fieldname] || [];
		const kept = rows.filter((r) => surviving.has(line_key(r)));
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

	if (frm.doc.follows_tender && project_tenders(frm).length && frm.doc.type_subcontractor && frm.doc.docstatus === 0) {
		frm.add_custom_button(__('Select Work Item'), () => open_work_item_dialog(frm));
	}

	// Follows Tender BRD FR-05: in both modes. On a Tender-following
	// contract a line added here is the per-line override (shown red).
	// Draft only: lines are frozen server-side once the contract leaves it.
	if (frm.doc.type_subcontractor && frm.doc.docstatus === 0 && ['', 'Draft'].includes(frm.doc.workflow_state || '')) {
		frm.add_custom_button(__('Select Work Item Template'), () => open_template_dialog(frm));
	}

	// FR-05/06 (2026-09-27 BRD): a Contracted Items price over its work
	// item's ceiling blocks save server-side (validate_price_ceiling) until
	// this is set - offered whenever the contract is still editable, not
	// only after a block is hit, same as the other Draft-only buttons here.
	if (frm.doc.docstatus === 0 && !frm.doc.price_exception_acknowledged) {
		frm.add_custom_button(__('Delegate to Cost Control'), () => {
			frappe.confirm(
				__('Acknowledge that one or more Contracted Items prices exceed this work item\'s approved ceiling, and save anyway? This will require CEO approval later in the approval chain.'),
				() => {
					frm.set_value('price_exception_acknowledged', 1).then(() => frm.save());
				}
			);
		});
	}

	// FR-01-04: "Create" shortcuts for an Approved, still-open contract - no
	// role restriction here, since creating a Contractor Invoice / Payment
	// Entry is already gated by each target doctype's own permissions.
	if (frm.doc.workflow_state === 'Approved' && !frm.doc.is_closed) {
		frm.add_custom_button(__('Contractor Invoice'), () => {
			frappe.new_doc('Contractor Invoice', {
				project: frm.doc.project,
				contractor_contract: frm.doc.name,
				subcontractor: frm.doc.contractor_name,
			});
		}, __('Create'));

		frm.add_custom_button(__('Advance Payment'), () => {
			// frappe.new_doc()'s prefill dict only ever copies Link/Data/
			// Select/Dynamic Link fields into the new document - a Table
			// field like `references` is silently dropped, and even `party`
			// (which is copied) never fires its own on-change trigger that
			// way, so paid_to/paid_from never got resolved. Routing this
			// through set_value/add_child on the real, loaded form instead
			// fixes both. party's own handler also clear_table()s references
			// as part of its account-resolution chain, so the reference row
			// has to be added *after* that promise resolves, not before.
			//
			// frappe.new_doc()'s own 3rd-argument callback fires too early -
			// cur_frm is still the *previous* form (this Subcontractor
			// Contract) at that point, not yet the new Payment Entry, so
			// set_value('party', ...) below would throw "Field party not
			// found" against the wrong doctype. Its *returned promise*
			// (verified directly against this Frappe version) resolves only
			// once routing has actually completed and cur_frm is the new
			// form - use that instead.
			frappe.db.get_single_value('Contracting Settings', 'subcontractor_payable_account').then((payable_account) => {
				frappe.new_doc('Payment Entry', {
					payment_type: 'Pay',
					party_type: 'Supplier',
					company: frm.doc.company,
				}).then(() => {
					const pe_frm = cur_frm;
					pe_frm.set_value('party', frm.doc.contractor_name).then(() => {
						const add_reference = () => {
							const row = pe_frm.add_child('references', {
								reference_doctype: 'Subcontractor Contract',
								reference_name: frm.doc.name,
							});
							pe_frm.refresh_field('references');
							// Reuses the same reference_name trigger a manual
							// grid selection fires (payment_entry.js), rather
							// than duplicating its total_amount/outstanding_
							// amount/allocated_amount logic here.
							pe_frm.script_manager.trigger('reference_name', row.doctype, row.name);
						};

						// create_purchase_invoice() already routes generated
						// Purchase Invoices to this same configured account
						// when set (Contracting Settings) - the advance and
						// the invoice it later reconciles into have to land
						// on the *same* account, or the match fails. party's
						// own generic account resolution (just above) has no
						// way to know that, so it's overridden explicitly
						// here, after that resolution has already run.
						if (payable_account) {
							pe_frm.set_value('paid_to', payable_account).then(add_reference);
						} else {
							add_reference();
						}
					});
				});
			});
		}, __('Create'));
	}

	// FR-11/12: only on an Approved, still-open contract, and only for the
	// roles close_contract's own server-side check allows - hiding the
	// button for everyone else is defense in depth, not the real gate.
	if (frm.doc.workflow_state === 'Approved' && !frm.doc.is_closed
		&& frappe.user.has_role(['Contracts Manager', 'System Manager'])) {
		frm.add_custom_button(__('Close Contract'), () => {
			frappe.confirm(
				__('Close this contract? Any never-invoiced remaining quantity will be released back to general availability.'),
				() => {
					frappe.call({
						method: 'contracting.contracting.doctype.subcontractor_contract.subcontractor_contract.close_contract',
						args: {name: frm.doc.name},
						callback() { frm.reload_doc(); }
					});
				}
			);
		});
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

// FR-06/FR-09 (2026-08-30), rescale added 2026-09-12: auto-populate the
// resource tabs from a work item's own Tender-linked resources, instead of
// requiring them to be re-picked one by one. Fires from both the work_item
// and qty handlers below, and is safe to call repeatedly for the same work
// item: if it has never been propagated before, this is first-time
// propagation (create fresh, exactly as before, and only once qty is
// actually non-zero); once it has, every later call is a rescale - only
// qty/available_qty are updated in place on rows that are still
// auto_propagated, nothing is created or deleted, so a deleted row (FR-03)
// or a hand-edited/manually-added row (FR-04/FR-05) is left untouched, and
// a qty corrected all the way to 0 (FR-10) still updates them rather than
// being mistaken for "nothing to propagate yet". The owning Contracted
// Items row's own resources_propagated flag is what lets "rescale, every
// row since deleted" be told apart from "never propagated" once the
// resource tables hold no rows either way - a plain "does a current row
// exist" check can't distinguish those, and FR-03 requires it to. Unlike an
// in-memory flag, this one is a real field on an already-saved row, so it
// survives a reload - deleting the last propagated row, saving, reopening
// the document and editing qty again must still not resurrect it. The
// dialog's own add path (render_work_item_dialog) calls this directly with
// both values known up front.
//
// Follows Tender BRD: `line` is the Contracted Items row (or anything with
// its work_item / work_item_template) - a template line propagates from its
// Work Item Template V2 instead (FR-07), and rescales by matching rows on
// their Item rather than on a Tender resource link.
function propagate_resources(frm, line, qty) {
	const key = line_key(line);
	if (!key) return;
	const is_template = !line.work_item;
	const contracted_row = (frm.doc.contracted_items || []).find((row) => line_key(row) === key);
	const has_existing = (contracted_row && contracted_row.resources_propagated)
		|| Object.keys(RESOURCE_TABLES).some((fieldname) =>
			(frm.doc[fieldname] || []).some((row) => line_key(row) === key));

	if (!has_existing && !flt(qty)) return;

	frappe.call({
		method: is_template
			? `${ALLOCATION}.get_template_resource_rows`
			: `${ALLOCATION}.get_propagated_resource_rows`,
		args: is_template
			? { work_item_template: line.work_item_template, qty, contract_type: frm.doc.type_subcontractor }
			: {
				work_item: line.work_item, qty,
				contract_type: frm.doc.type_subcontractor,
				subcontractor_contract: frm.doc.name,
			},
		callback(r) {
			const by_table = r.message || {};

			const updates = [];
			Object.keys(RESOURCE_TABLES).forEach((fieldname) => {
				const [, link_fieldname] = RESOURCE_TABLES[fieldname];
				const [tender_doctype, contract_doctype] = RESOURCE_TITLE_DOCTYPES[fieldname];
				const new_rows = by_table[fieldname] || [];

				if (!has_existing) {
					new_rows.forEach((row) => {
						const added = frm.add_child(fieldname, row);
						// A propagated row is added via frm.add_child(), which
						// never calls frappe.utils.add_link_title() the way a
						// real dropdown selection does - so both the Tender-
						// side resource link (this table's own grid column)
						// and the Contractor Contract-side row itself (what
						// Payment Condition's material_item/labor_item/
						// equipment_item pickers point at) would otherwise
						// render as a raw docname until the whole document is
						// saved for real. Priming both here fixes display
						// immediately; it doesn't make an unsaved row
						// *searchable* in Payment Condition's picker, which
						// still queries the database.
						if (row[link_fieldname]) {
							frappe.utils.add_link_title(tender_doctype, row[link_fieldname], row.resource_item);
						}
						frappe.utils.add_link_title(contract_doctype, added.name, row.resource_item);
					});
				} else {
					const existing_rows = (frm.doc[fieldname] || []).filter((row) => line_key(row) === key);
					const match_field = is_template ? 'item' : link_fieldname;
					new_rows.forEach((new_row) => {
						const match = existing_rows.find((row) => row[match_field] === new_row[match_field]);
						if (match && match.auto_propagated) {
							updates.push(frappe.model.set_value(match.doctype, match.name, 'qty', new_row.qty));
							if (is_template) {
								updates.push(frappe.model.set_value(match.doctype, match.name, 'amount',
									flt(new_row.qty) * flt(match.rate)));
							} else {
								updates.push(frappe.model.set_value(match.doctype, match.name, 'available_qty', new_row.available_qty));
							}
						}
					});
				}
				frm.refresh_field(fieldname);
			});

			if (!has_existing && contracted_row) {
				updates.push(frappe.model.set_value(contracted_row.doctype, contracted_row.name, 'resources_propagated', 1));
			}

			// Guards the qty handlers below (FR-05) from mistaking this
			// rescale's own writes for a hand edit. set_value's trigger
			// fires asynchronously, so the flag has to stay up until every
			// update above has actually resolved, not just until this
			// forEach returns.
			frm.__rescaling = true;
			Promise.all(updates).then(() => { frm.__rescaling = false; });
		},
	});
}

function maybe_propagate_row(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	// Whether a qty of 0 is worth acting on (skip first-time creation, still
	// rescale existing rows down to 0 per FR-10) is propagate_resources' own
	// call to make now, not this guard's.
	if (!line_key(row)) return;
	propagate_resources(frm, row, row.qty);
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

	// FR-01: server enforces unit_price's read_only_depends_on
	// (validate_price_ceiling/compute_component_pricing are the real
	// authority, per NFR-01) - this just makes the grid re-evaluate that
	// condition immediately instead of on next redraw.
	pricing_mode(frm) {
		frm.fields_dict.contracted_items.grid.refresh();
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

	// FR-05: a direct hand-edit of qty in this tab permanently protects the
	// row from further rescaling - unless this write is the rescale's own
	// (FR-06), guarded by propagate_resources' own frm.__rescaling flag.
	qty(frm, cdt, cdn) {
		if (frm.__rescaling) return;
		frappe.model.set_value(cdt, cdn, 'auto_propagated', 0);
		set_resource_amount(cdt, cdn);
	},

	rate(frm, cdt, cdn) {
		set_resource_amount(cdt, cdn);
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

	// FR-05: a direct hand-edit of qty in this tab permanently protects the
	// row from further rescaling - unless this write is the rescale's own
	// (FR-06), guarded by propagate_resources' own frm.__rescaling flag.
	qty(frm, cdt, cdn) {
		if (frm.__rescaling) return;
		frappe.model.set_value(cdt, cdn, 'auto_propagated', 0);
		set_resource_amount(cdt, cdn);
	},

	rate(frm, cdt, cdn) {
		set_resource_amount(cdt, cdn);
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

	// FR-05: a direct hand-edit of qty in this tab permanently protects the
	// row from further rescaling - unless this write is the rescale's own
	// (FR-06), guarded by propagate_resources' own frm.__rescaling flag.
	qty(frm, cdt, cdn) {
		if (frm.__rescaling) return;
		frappe.model.set_value(cdt, cdn, 'auto_propagated', 0);
		set_resource_amount(cdt, cdn);
	},

	rate(frm, cdt, cdn) {
		set_resource_amount(cdt, cdn);
	},
});

function set_amount(cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, 'amount', flt(row.qty) * flt(row.unit_price));
}

// Same shape as set_amount above, for the three resource tabs' rate/amount
// pair - which compute_component_pricing (server) rolls up into the parent
// Contracted Items row's unit_price when that row is in Components mode.
function set_resource_amount(cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, 'amount', flt(row.qty) * flt(row.rate));
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
				frm.add_child('contracted_items', child);
				// FR-06: qty is already known here (get_unallocated_boq_items'
				// own availability default), so propagate immediately rather
				// than waiting on the work_item/qty handler below.
				propagate_resources(frm, row, row.qty);
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

// ---------------------------------------------------------------------------
// Follows Tender BRD (2026-10-13): template lines, extra rows, red flags
// ---------------------------------------------------------------------------

// FR-05: "Select Work Item Template" - every Work Item Template V2, with a
// qty per selected template. A template already on the contract (outside
// addendum-inserted rows) is shown but not selectable (FR-06).
function open_template_dialog(frm) {
	frappe.call({
		method: 'contracting.contracting.doctype.subcontractor_contract.subcontractor_contract.get_work_item_templates',
		freeze: true,
		callback(r) {
			const templates = r.message || [];
			if (!templates.length) {
				frappe.msgprint(__('No Work Item Templates exist yet.'));
				return;
			}
			render_template_dialog(frm, templates);
		},
	});
}

function render_template_dialog(frm, templates) {
	const taken = new Set((frm.doc.contracted_items || [])
		.filter((r) => r.work_item_template && !r.addendum).map((r) => r.work_item_template));

	const dialog = new frappe.ui.Dialog({
		title: __('Select Work Item Template'),
		size: 'large',
		fields: [
			{ fieldname: 'search', fieldtype: 'Data', label: __('Search') },
			{ fieldname: 'rows_html', fieldtype: 'HTML' },
		],
		primary_action_label: __('Add Selected'),
		primary_action() {
			const checked = dialog.$wrapper.find('.template-row input[type=checkbox]:checked');
			if (!checked.length) {
				frappe.msgprint(__('Select at least one template.'));
				return;
			}

			const picked = [];
			let missing_qty = false;
			checked.each(function () {
				const $row = $(this).closest('.template-row');
				const qty = flt($row.find('input.template-qty').val());
				if (qty <= 0) missing_qty = true;
				picked.push([templates[$row.data('idx')], qty]);
			});
			if (missing_qty) {
				frappe.msgprint(__('Enter a quantity greater than 0 for every selected template.'));
				return;
			}

			picked.forEach(([template, qty]) => {
				const row = frm.add_child('contracted_items', {
					work_item_template: template.name,
					follows_tender: 0,
					description: template.description || template.template_name,
					uom: template.uom,
					qty,
				});
				frappe.utils.add_link_title(TEMPLATE_DOCTYPE, template.name, template.template_name);
				propagate_resources(frm, row, qty);
			});
			frm.refresh_field('contracted_items');
			setup_buttons(frm);
			dialog.hide();
		},
	});

	function render(filter_text) {
		const term = (filter_text || '').toLowerCase();
		const html = templates.map((t, idx) => {
			const label = t.template_name + (t.description ? ` — ${t.description}` : '');
			if (term && !label.toLowerCase().includes(term)) return '';
			const is_taken = taken.has(t.name);
			return `
				<div class="template-row" data-idx="${idx}"
					style="display:flex; align-items:center; gap:12px; padding:6px 0; border-bottom:1px solid var(--border-color);${is_taken ? ' opacity:0.5;' : ''}">
					<input type="checkbox" ${is_taken ? 'disabled' : ''}>
					<span style="flex:3">${frappe.utils.escape_html(label)}</span>
					<span style="flex:1; color:var(--text-muted);">${frappe.utils.escape_html(t.uom || '')}</span>
					<span style="flex:1">${is_taken
						? __('Already on this contract')
						: `<input type="number" min="0" step="any" class="form-control input-xs template-qty" placeholder="${__('Qty')}">`}</span>
				</div>
			`;
		}).join('');
		dialog.fields_dict.rows_html.$wrapper.html(html || `<p>${__('No matches.')}</p>`);
	}

	dialog.fields_dict.search.df.onchange = () => render(dialog.get_value('search'));
	render();
	dialog.show();
}

// Free Item on template-sourced / extra resource rows: keep the readable
// label and UOM in step with it (server re-derives both on save).
['Contractor Contract Material Item', 'Contractor Contract Labor Item', 'Contractor Contract Equipment Item']
	.forEach((doctype) => {
		frappe.ui.form.on(doctype, {
			item(frm, cdt, cdn) {
				const row = locals[cdt][cdn];
				if (!row.item) return;
				frappe.db.get_value('Item', row.item, ['item_name', 'stock_uom']).then((r) => {
					const item = r.message || {};
					frappe.model.set_value(cdt, cdn, 'resource_item', item.item_name || row.item);
					frappe.model.set_value(cdt, cdn, 'uom', item.stock_uom);
				});
			},

			// FR-08: an extra row belongs to exactly one line.
			work_item(frm, cdt, cdn) {
				if (locals[cdt][cdn].work_item) frappe.model.set_value(cdt, cdn, 'work_item_template', null);
			},

			work_item_template(frm, cdt, cdn) {
				if (locals[cdt][cdn].work_item_template) frappe.model.set_value(cdt, cdn, 'work_item', null);
			},

			form_render(frm, cdt, cdn) {
				paint_row_form(frm, cdt, cdn);
			},
		});
	});

frappe.ui.form.on('Contractor Contract Item', {
	form_render(frm, cdt, cdn) {
		paint_row_form(frm, cdt, cdn);
	},
});

frappe.ui.form.on('Contractor Contract Payment Condition', {
	work_item(frm, cdt, cdn) {
		if (locals[cdt][cdn].work_item) frappe.model.set_value(cdt, cdn, 'work_item_template', null);
	},

	work_item_template(frm, cdt, cdn) {
		if (locals[cdt][cdn].work_item_template) frappe.model.set_value(cdt, cdn, 'work_item', null);
	},
});

const OUT_OF_SCOPE_GRIDS = {
	contracted_items: __('Contracted Items'),
	contract_material_items: __('Material'),
	contract_labor_items: __('Labor'),
	contract_equipment_items: __('Equipment'),
};

// FR-12: red rows in all four grids. Frappe v14 has no row-colour API, so
// this tags each grid row's element directly; out_of_tender_scope is only
// ever (re)computed server-side, so painting on refresh (which follows
// every save) keeps it current.
function paint_out_of_scope(frm) {
	if (!document.getElementById('out-of-tender-style')) {
		$('<style id="out-of-tender-style">'
			+ '.out-of-tender-row .row-index, .out-of-tender-row .static-area, .out-of-tender-row .field-area'
			+ ' { color: var(--red-600) !important; }'
			+ '.out-of-tender-row { background-color: var(--red-50) !important; }'
			+ '</style>').appendTo('head');
	}

	Object.keys(OUT_OF_SCOPE_GRIDS).forEach((fieldname) => {
		const grid = frm.fields_dict[fieldname] && frm.fields_dict[fieldname].grid;
		(grid && grid.grid_rows || []).forEach((grid_row) => {
			const red = !!(grid_row.doc && grid_row.doc.out_of_tender_scope);
			$(grid_row.row).toggleClass('out-of-tender-row', red);
		});
	});

	if (frm.doc.has_out_of_tender_items) {
		frm.dashboard.set_headline_alert(
			__('This contract contains items outside the original Tender. Rows marked red need Cost Control and CEO approval.'),
			'red'
		);
	} else {
		frm.dashboard.clear_headline();
	}
}

// FR-12, editing view: the open row form gets the same red treatment.
function paint_row_form(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const parentfield = row.parentfield;
	const grid_row = frm.fields_dict[parentfield] && frm.fields_dict[parentfield].grid.get_row(cdn);
	if (grid_row && grid_row.grid_form) {
		$(grid_row.grid_form.wrapper).toggleClass('out-of-tender-row', !!row.out_of_tender_scope);
	}
}

// FR-14: every red row with its FR-10 reason, in words - never a figure
// (NFR-03).
function out_of_scope_rows(frm) {
	const result = [];
	Object.keys(OUT_OF_SCOPE_GRIDS).forEach((fieldname) => {
		(frm.doc[fieldname] || []).forEach((row) => {
			if (!row.out_of_tender_scope) return;
			let reason;
			if (fieldname === 'contracted_items') {
				reason = row.work_item
					? __('Unit price is above the Tender cost (delegated to Cost Control)')
					: __('Line built from a Work Item Template, not the Tender');
			} else {
				reason = row.source === 'Tender'
					? __('Rate is above the Tender rate')
					: __('Row added outside the Tender ({0})', [row.source === 'Template' ? __('template') : __('extra')]);
			}
			result.push({
				tab: OUT_OF_SCOPE_GRIDS[fieldname],
				idx: row.idx,
				label: row.description || row.resource_item || row.item,
				reason,
			});
		});
	});
	return result;
}
