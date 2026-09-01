const RESOURCE_TABLES = ['material_items', 'labor_items', 'equipment_items'];

frappe.ui.form.on('Tender', {
    setup(frm) {
        set_resource_queries(frm);
    },
    onload(frm) {
        set_resource_queries(frm);
    },
    refresh(frm) {
        set_resource_queries(frm);
        apply_indent_styling(frm);
        recalculate_group_totals(frm);

        if (!frm.doc.__islocal && frm.doc.status === 'Complete') {
            frm.add_custom_button(__('Create Revised Version'), () => {
                frappe.confirm(
                    __('Create a new, numbered version of this Tender with a copy of its BOQ? '
                        + 'This Tender stays Complete - re-select the new version in the relevant '
                        + "Project Tender's Direct Cost Detail row afterward."),
                    () => {
                        frappe.call({
                            method: 'contracting.contracting.doctype.tender.tender.create_revised_version',
                            args: {tender_name: frm.doc.name},
                            callback(r) {
                                if (r.message) frappe.set_route('tender', r.message);
                            }
                        });
                    }
                );
            });
        }

        if (!frm.doc.__islocal && frm.doc.status === 'In Progress' && frappe.user.has_role('Tender Manager')) {
            frm.add_custom_button(__('Mark Complete'), () => {
                if (frm.is_dirty()) {
                    frappe.msgprint(__('Save your changes first, then mark this Tender Complete.'));
                    return;
                }
                frappe.confirm(
                    __('Mark this Tender Complete? It will be locked - further changes require Create Revised Version.'),
                    () => {
                        frappe.call({
                            method: 'contracting.contracting.doctype.tender.tender.mark_tender_complete',
                            args: {tender_name: frm.doc.name},
                            freeze: true,
                            callback(r) {
                                if (!r.exc) frm.reload_doc();
                            }
                        });
                    }
                );
            });
        }

        frm.fields_dict.boq_items.grid.add_custom_button(__('Add Item from Template'), () => {
            open_add_boq_item_dialog(frm);
        });
        // The native "Add Row" bypasses add_boq_item_from_template's atomic
        // insert - two users adding rows that way around the same time hit
        // a TimestampMismatchError on save. Routing everyone through the
        // button above avoids that; group/section-header rows can no
        // longer be added from the UI as a result.
        frm.fields_dict.boq_items.grid.cannot_add_rows = true;
        frm.fields_dict.boq_items.grid.setup_toolbar();

        [frm.fields_dict.boq_items, frm.fields_dict.material_items, frm.fields_dict.labor_items, frm.fields_dict.equipment_items]
            .forEach(f => make_grid_filter_toggle(f.grid));
    },
    boq_items_add(frm) { recalculate_group_totals(frm); },
    boq_items_remove(frm) { recalculate_group_totals(frm); },
    safety_factor_percent(frm) { recalculate_group_totals(frm); refresh_new_field_preview(frm); }
});

frappe.ui.form.on('Tender BOQ Item', {
    work_item_template(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        if (!row.work_item_template) return;
        frappe.call({
            method: 'contracting.contracting.api.work_item_template_v2.get_v2_template_rows',
            args: {template_name: row.work_item_template},
            callback(r) {
                if (r.message) apply_v2_template(frm, row, r.message);
            }
        });
    },
    original_quantity(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        recalc_resources_for_work_item(frm, row);
        recalculate_group_totals(frm);
    },
    vat_percentage(frm, cdt, cdn) { recalculate_group_totals(frm); },
    other_additions_pct(frm, cdt, cdn) { recalculate_group_totals(frm); },
    fixed_additions(frm, cdt, cdn) { recalculate_group_totals(frm); },
    margin_percent(frm, cdt, cdn) { recalculate_group_totals(frm); },
    display_currency(frm) { refresh_new_field_preview(frm); }
});

RESOURCE_TABLES.forEach(fieldname => {
    let child_doctype = {
        material_items: 'Tender Material Item',
        labor_items: 'Tender Labor Item',
        equipment_items: 'Tender Equipment Item'
    }[fieldname];

    frappe.ui.form.on(child_doctype, {
        work_item(frm, cdt, cdn) {
            recalc_single_resource_row(frm, locals[cdt][cdn]);
            recalculate_group_totals(frm);
            refresh_new_field_preview(frm);
        },
        qty_per_unit(frm, cdt, cdn) {
            recalc_single_resource_row(frm, locals[cdt][cdn]);
            recalculate_group_totals(frm);
            refresh_new_field_preview(frm);
        },
        currency(frm, cdt, cdn) {
            let row = locals[cdt][cdn];
            // Pure client-side convenience - tender.py re-resolves this
            // authoritatively from currency_table on save either way.
            let currency_row = (frm.doc.currency_table || []).find(c => c.currency === row.currency);
            row.exchange_rate = currency_row ? currency_row.exchange_rate : 1;
            recalc_single_resource_row(frm, row);
            if (frm.doc.consolidate_rates_by_item) {
                // A currency change also carries the row's current rate to
                // its siblings, so every row of the same item ends up
                // identically priced regardless of which field triggered
                // the sync.
                propagate_pricing_fields(frm, fieldname, row, ['currency', 'exchange_rate', 'rate']);
            }
            recalculate_group_totals(frm);
            refresh_new_field_preview(frm);
        },
        rate(frm, cdt, cdn) {
            let row = locals[cdt][cdn];
            recalc_single_resource_row(frm, row);
            if (frm.doc.consolidate_rates_by_item) {
                propagate_pricing_fields(frm, fieldname, row, ['rate']);
            }
            recalculate_group_totals(frm);
            refresh_new_field_preview(frm);
        }
    });
});

function set_resource_queries(frm) {
    // Item pickers: same Material(stock)/Labor/Equipment Item Group
    // split as Work Item Template V2 - is_stock_item alone doesn't
    // reliably separate service items from physical goods in this
    // bench's data, Item Group is the real signal.
    frm.set_query('item', 'material_items', () => ({filters: {is_stock_item: 1}}));
    frm.set_query('item', 'labor_items', () => ({filters: {item_group: 'Labor'}}));
    frm.set_query('item', 'equipment_items', () => ({filters: {item_group: 'Equipment'}}));

    // Currency picker: only currencies listed on this Tender's own
    // Currencies table, plus whatever the base currency happens to be -
    // tender.py enforces the same restriction server-side.
    RESOURCE_TABLES.forEach(fieldname => {
        frm.set_query('currency', fieldname, () => {
            let codes = (frm.doc.currency_table || []).map(c => c.currency).filter(Boolean);
            codes.push(frappe.defaults.get_default('currency'));
            return {filters: {name: ['in', codes]}};
        });
    });

    // display_currency: same restriction as the resource-row currency
    // pickers above - only currencies listed in this Tender's own
    // Currencies table, so removing one that's still referenced can be
    // blocked (tender.py's resolve_exchange_rate enforces this server-side).
    frm.set_query('display_currency', 'boq_items', () => {
        let codes = (frm.doc.currency_table || []).map(c => c.currency).filter(Boolean);
        codes.push(frappe.defaults.get_default('currency'));
        return {filters: {name: ['in', codes]}};
    });
}

function refresh_new_field_preview(frm) {
    // Server-truth preview for the new cost-layer/rollup fields only
    // (direct_cost_amount..rate_egp, BOQ material_cost..display_amount) -
    // amount/total_amount's existing client recompute (recalc_single_resource_row/
    // recalculate_group_totals) is untouched. Avoids a second, divergent JS
    // formula for these new fields (NFR-02), mirroring project_tender.js's
    // refresh_preview pattern.
    frappe.call({
        method: 'contracting.contracting.doctype.tender.tender.preview_resource_totals',
        args: {tender: frm.doc},
        callback(r) {
            if (!r.message) return;
            let preview = r.message;
            apply_new_field_previews(frm, 'boq_items', preview.boq_items,
                ['material_cost', 'labor_cost', 'equipment_cost', 'boq_direct_cost',
                    'boq_safety_factor_amount', 'boq_indirect_cost_amount',
                    'indirect_cost_percent', 'display_amount']);
            RESOURCE_TABLES.forEach(fieldname => {
                apply_new_field_previews(frm, fieldname, preview[fieldname],
                    ['direct_cost_amount', 'safety_factor_amount', 'indirect_cost_amount',
                        'amount_currency', 'rate_egp']);
            });
        }
    });
}

function apply_new_field_previews(frm, fieldname, previewed_rows, fields_to_apply) {
    let by_name = {};
    (previewed_rows || []).forEach(row => { by_name[row.name] = row; });

    (frm.doc[fieldname] || []).forEach(row => {
        let previewed = by_name[row.name];
        if (!previewed) return;
        fields_to_apply.forEach(f => {
            if (previewed[f] !== undefined) row[f] = previewed[f];
        });
    });
    frm.refresh_field(fieldname);
}

function open_add_boq_item_dialog(frm, on_done) {
    if (frm.is_dirty()) {
        frappe.msgprint(__('Save your other changes first, then add the item. This action commits directly to the server so a fresh copy can be reloaded afterward - reloading over unsaved changes would discard them.'));
        return;
    }
    frappe.prompt(
        [
            {fieldname: 'work_item_template', fieldtype: 'Link', options: 'Work Item Template V2', label: __('Work Item Template'), reqd: 1},
            {fieldname: 'original_quantity', fieldtype: 'Float', label: __('Original Quantity'), default: 1},
        ],
        (values) => {
            frappe.call({
                method: 'contracting.contracting.doctype.tender.tender.add_boq_item_from_template',
                args: {
                    tender_name: frm.doc.name,
                    template_name: values.work_item_template,
                    original_quantity: values.original_quantity,
                },
                freeze: true,
                freeze_message: __('Adding item...'),
                callback(r) {
                    if (!r.exc) {
                        // reload_doc() returns a promise that resolves once
                        // frm.refresh() has already run against the new doc -
                        // callers that need to do something with the fresh
                        // state (e.g. Expand View reopening itself) hook in here.
                        frm.reload_doc().then(() => on_done && on_done());
                    }
                }
            });
        },
        __('Add BOQ Item'),
        __('Add')
    );
}

function find_boq_row(frm, work_item_no) {
    return (frm.doc.boq_items || []).find(r => r.idx === work_item_no);
}

function resource_amount(frm, row) {
    let safety_multiplier = 1 + (frm.doc.safety_factor_percent || 0) / 100;
    let pre_indirect = (row.qty || 0) * (row.rate || 0) * (row.exchange_rate || 1) * safety_multiplier;
    let indirect_multiplier = (frm.doc.propagated_addition_percent || 0) / 100;
    return pre_indirect * (1 + indirect_multiplier);
}

function recalc_single_resource_row(frm, row) {
    let work_item = find_boq_row(frm, row.work_item);
    let parent_qty = work_item ? (work_item.original_quantity || 0) : 0;
    row.qty = (row.qty_per_unit || 0) * parent_qty;
    row.amount = resource_amount(frm, row);
    RESOURCE_TABLES.forEach(f => frm.refresh_field(f));
}

function recalc_resources_for_work_item(frm, work_item_row) {
    RESOURCE_TABLES.forEach(fieldname => {
        (frm.doc[fieldname] || []).forEach(row => {
            if (row.work_item === work_item_row.idx) {
                row.qty = (row.qty_per_unit || 0) * (work_item_row.original_quantity || 0);
                row.amount = resource_amount(frm, row);
            }
        });
        frm.refresh_field(fieldname);
    });
}

function propagate_pricing_fields(frm, fieldname, changed_row, fields_to_sync) {
    if (!changed_row.item) return;
    (frm.doc[fieldname] || []).forEach(row => {
        if (row.name !== changed_row.name && row.item === changed_row.item) {
            fields_to_sync.forEach(f => { row[f] = changed_row[f]; });
            row.amount = resource_amount(frm, row);
        }
    });
    frm.refresh_field(fieldname);
}

function recalculate_group_totals(frm) {
    let items = frm.doc.boq_items || [];

    // Roll each work item's cost up from its linked Material/Labor/
    // Equipment rows - Tender BOQ Item no longer carries its own rate.
    let totals_by_work_item = {};
    let direct_by_work_item = {};
    let safety_by_work_item = {};
    let safety_multiplier = 1 + (frm.doc.safety_factor_percent || 0) / 100;
    RESOURCE_TABLES.forEach(fieldname => {
        (frm.doc[fieldname] || []).forEach(row => {
            totals_by_work_item[row.work_item] = (totals_by_work_item[row.work_item] || 0) + (row.amount || 0);
            let direct = (row.qty || 0) * (row.rate || 0) * (row.exchange_rate || 1);
            direct_by_work_item[row.work_item] = (direct_by_work_item[row.work_item] || 0) + direct;
            safety_by_work_item[row.work_item] = (safety_by_work_item[row.work_item] || 0) + direct * (safety_multiplier - 1);
        });
    });

    let indirect_multiplier = (frm.doc.propagated_addition_percent || 0) / 100;
    let total_direct_cost = 0, total_additions = 0, total_safety_factor = 0, total_indirect_cost = 0;
    let has_priced_item = false;
    for (let r of items) {
        if (!r.is_group) {
            let base = totals_by_work_item[r.idx] || 0;
            let vat = base * (r.vat_percentage || 0) / 100;
            let other = base * (r.other_additions_pct || 0) / 100;
            let fixed = (r.fixed_additions || 0);
            let effective_total = base + vat + other + fixed;
            r.total_amount = base;
            r.effective_unit_price = r.original_quantity ? effective_total / r.original_quantity : 0;

            r.sell_rate = r.effective_unit_price * (1 + (r.margin_percent || 0) / 100);
            r.sell_amount = r.sell_rate * (r.original_quantity || 0);

            // Direct Cost/Additions mirror tender.py::rollup_boq_costs() -
            // computed against the raw material+labor+equipment sum, never
            // against `base` above (already Safety-Factor/Indirect-Cost
            // inclusive), so Direct Cost stays structurally free of
            // Indirect Cost (2026-09-01 BRD).
            let raw_direct = direct_by_work_item[r.idx] || 0;
            let row_vat_amount = raw_direct * (r.vat_percentage || 0) / 100;
            let row_additions_amount = raw_direct * (r.other_additions_pct || 0) / 100 + (r.fixed_additions || 0);
            r.boq_direct_cost = raw_direct + row_vat_amount + row_additions_amount;
            r.boq_safety_factor_amount = safety_by_work_item[r.idx] || 0;
            r.boq_indirect_cost_amount = (r.boq_direct_cost + r.boq_safety_factor_amount) * indirect_multiplier;

            total_direct_cost += r.boq_direct_cost;
            total_additions += row_additions_amount;
            total_safety_factor += r.boq_safety_factor_amount;
            total_indirect_cost += r.boq_indirect_cost_amount;

            if (r.item_code && r.total_amount) has_priced_item = true;
        }
    }
    for (let i = items.length - 1; i >= 0; i--) {
        if (items[i].is_group) {
            let total = 0;
            for (let j = i + 1; j < items.length; j++) {
                if (items[j].indent <= items[i].indent) break;
                if (!items[j].is_group) total += items[j].total_amount || 0;
            }
            items[i].total_amount = total;
        }
    }
    // Only write these back when a real edit already dirtied the form (or
    // it's a brand-new doc) - refresh() alone must never mark a clean form
    // dirty just because the client recompute disagrees with a stale
    // server value.
    if (frm.doc.__islocal || frm.is_dirty()) {
        let tender_final_cost = total_direct_cost + total_safety_factor;
        frm.set_value('total_direct_cost', total_direct_cost);
        frm.set_value('total_additions', total_additions);
        frm.set_value('total_safety_factor', total_safety_factor);
        frm.set_value('tender_final_cost', tender_final_cost);
        frm.set_value('total_indirect_cost', total_indirect_cost);
        frm.set_value('sell_amount', tender_final_cost + total_indirect_cost);

        // Non-authoritative UI hint only - the server (evaluate_auto_progress)
        // is what actually flips status on save. Never set the status field
        // itself from here: mutating it client-side during a recompute is
        // exactly the anti-pattern that used to dirty a clean Tender form
        // and break realtime auto-reload (see tender.py's git history).
        if (frm.doc.status === 'Draft' && has_priced_item) {
            frappe.show_alert({message: __('This Tender will move to In Progress on save.'), indicator: 'blue'});
        }
    }
    frm.refresh_field('boq_items');
    apply_indent_styling(frm);
}

function apply_indent_styling(frm) {
    setTimeout(() => {
        let wrapper = frm.fields_dict.boq_items && frm.fields_dict.boq_items.grid && frm.fields_dict.boq_items.grid.wrapper;
        if (!wrapper) return;
        wrapper.find('.grid-row').each(function() {
            let idx = parseInt($(this).attr('data-idx')) - 1;
            let row = (frm.doc.boq_items || [])[idx];
            if (!row) return;
            let padding = (row.indent || 0) * 20;
            $(this).find('[data-fieldname="item_name"]').css('padding-left', padding + 'px');
            if (row.is_group) $(this).find('[data-fieldname="item_name"]').css('font-weight', 'bold');
        });
    }, 200);
}

function apply_v2_template(frm, row, payload) {
    // The row the template was picked on stays a normal work-item row
    // (no forced is_group) - its cost now comes from whatever lands in
    // the Material/Labor/Equipment tabs below, tagged to this row via
    // work_item. VAT/other-additions carry over from the template onto
    // this row, since that's where those fields live now.
    let parent = payload.parent;
    frappe.model.set_value(row.doctype, row.name, 'item_name', parent.item_name);
    frappe.model.set_value(row.doctype, row.name, 'item_type', parent.item_type);
    frappe.model.set_value(row.doctype, row.name, 'uom', parent.uom);
    frappe.model.set_value(row.doctype, row.name, 'vat_percentage', parent.vat_percentage);
    frappe.model.set_value(row.doctype, row.name, 'other_additions_pct', parent.other_additions_pct);
    frappe.model.set_value(row.doctype, row.name, 'fixed_additions', parent.fixed_additions);

    let table_by_type = {material: 'material_items', labor: 'labor_items', equipment: 'equipment_items'};
    payload.children.forEach(child => {
        let fieldname = table_by_type[child.resource_type];
        let new_row = frm.add_child(fieldname, {
            work_item: row.idx,
            item: child.item,
            qty_per_unit: child.qty_per_unit,
            rate: child.rate,
            currency: frappe.defaults.get_default('currency'),
            exchange_rate: 1
        });
        new_row.qty = (child.qty_per_unit || 0) * (row.original_quantity || 0);
        new_row.amount = resource_amount(frm, new_row);
        frm.refresh_field(fieldname);
    });

    recalculate_group_totals(frm);
}

function make_grid_filter_toggle(grid) {
    // Frappe's grid already has a per-column search row, but grid_row.js
    // only auto-shows it once a table has >= 20 rows, and Grid.refresh()
    // unconditionally recomputes filter_applied from this.filter on every
    // call - so naively setting grid.filter_applied = true gets silently
    // reset the next time anything (e.g. recalculate_group_totals's own
    // frm.refresh_field calls) triggers a refresh. Wrapping refresh() to
    // re-assert it afterward is what makes the toggle sticky.
    if (grid.__filter_toggle_installed) return;
    grid.__filter_toggle_installed = true;

    const original_refresh = grid.refresh.bind(grid);
    grid.refresh = function() {
        original_refresh();
        if (grid.__filter_forced) {
            grid.filter_applied = true;
            grid.make_head();
        }
    };

    grid.add_custom_button(__('Toggle Filter Row'), () => {
        grid.__filter_forced = !grid.__filter_forced;
        grid.filter_applied = grid.__filter_forced;
        grid.make_head();
    });
}
