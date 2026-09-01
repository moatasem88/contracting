frappe.ui.form.on('Project Tender', {
    setup(frm) {
        // Live-tick from Tender's ~2s autosync while a linked Tender's BOQ
        // is being edited - patches just the matching row's Tender Total,
        // no reload. Distinct event name (not `doc_update`) so this never
        // rides on core's global reload/conflict listener in model.js.
        frappe.realtime.on('project_tender_direct_cost_tick', (data) => {
            if (frm.doc.name !== data.project_tender) return;
            let changed = false;
            (frm.doc.direct_cost_details || []).forEach(row => {
                if (row.tender === data.tender) {
                    row.tender_total = data.tender_total;
                    row.tender_sell_amount = data.tender_sell_amount;
                    changed = true;
                }
            });
            if (changed) frm.refresh_field('direct_cost_details');
        });
    },
    refresh(frm) {
        set_tender_query(frm);
        set_tender_status_formatter(frm);
    },
    direct_cost_details_add(frm) { refresh_preview(frm); },
    direct_cost_details_remove(frm) { refresh_preview(frm); },
    indirect_cost_details_add(frm) { refresh_preview(frm); },
    indirect_cost_details_remove(frm) { refresh_preview(frm); },
    overhead_deduction_details_add(frm) { refresh_preview(frm); },
    overhead_deduction_details_remove(frm) { refresh_preview(frm); }
});

frappe.ui.form.on('Project Tender Direct Cost Detail', {
    tender(frm) { refresh_preview(frm); }
});

frappe.ui.form.on('Project Tender Cost Line', {
    cost_component(frm) { refresh_preview(frm); },
    calculation_type(frm) { refresh_preview(frm); },
    rate(frm) { refresh_preview(frm); },
    amount(frm) { refresh_preview(frm); }
});

function set_tender_query(frm) {
    // Restricted to Tenders already belonging to this Project Tender -
    // Tender.project_tender is mandatory, so this is a real filter, not
    // one that has to tolerate blanks. tender_category now follows the
    // selected Tender (fetch_from) rather than pre-filtering it.
    // project_tender.py enforces the same rule server-side as a backstop.
    frm.set_query('tender', 'direct_cost_details', (doc) => {
        return {
            filters: {
                project_tender: doc.name
            }
        };
    });
}

function set_tender_status_formatter(frm) {
    // No colored-Select-column primitive exists for grids in this Frappe
    // version - reuse the same indicator-pill markup/colors core uses for
    // list view status badges (frappe/public/js/frappe/list/list_view.js).
    frm.fields_dict.direct_cost_details.grid.update_docfield_property(
        'tender_status', 'formatter',
        (value) => {
            if (!value) return '';
            const colors = { Draft: 'gray', 'In Progress': 'orange', Complete: 'green' };
            return `<span class="indicator-pill ${colors[value] || 'gray'} filterable">${frappe.utils.escape_html(__(value))}</span>`;
        }
    );
}

function refresh_preview(frm) {
    // Rather than re-deriving FR-16's calculation in JS (the exact mistake
    // tender.js made, per CLAUDE.md), ask the server to run the same
    // validate()-time logic on the in-memory doc, without saving.
    frappe.call({
        method: 'contracting.contracting.doctype.project_tender.project_tender.preview_totals',
        args: {project_tender: frm.doc},
        callback(r) {
            if (!r.message) return;
            let totals = r.message;

            frm.set_value('total_direct_cost', totals.total_direct_cost);
            frm.set_value('total_indirect_cost', totals.total_indirect_cost);
            frm.set_value('total_direct_and_indirect_cost', totals.total_direct_and_indirect_cost);
            frm.set_value('sub_total_overhead_and_profit', totals.sub_total_overhead_and_profit);
            frm.set_value('total_sell_price', totals.total_sell_price);
            frm.set_value('total_addition_percent', totals.total_addition_percent);
            frm.set_value('direct_cost_percent_of_total_price', totals.direct_cost_percent_of_total_price);
            frm.set_value('indirect_cost_percent_of_direct_cost', totals.indirect_cost_percent_of_direct_cost);
            frm.set_value('indirect_cost_percent_of_total_price', totals.indirect_cost_percent_of_total_price);
            frm.set_value('overhead_deduction_percent_of_direct_cost', totals.overhead_deduction_percent_of_direct_cost);
            frm.set_value('overhead_deduction_percent_of_total_price', totals.overhead_deduction_percent_of_total_price);

            apply_row_previews(frm, 'direct_cost_details', totals.direct_cost_details);
            apply_row_previews(frm, 'indirect_cost_details', totals.indirect_cost_details);
            apply_row_previews(frm, 'overhead_deduction_details', totals.overhead_deduction_details);
        }
    });
}

function apply_row_previews(frm, fieldname, previewed_rows) {
    let by_name = {};
    previewed_rows.forEach(row => { by_name[row.name] = row; });

    (frm.doc[fieldname] || []).forEach(row => {
        let previewed = by_name[row.name];
        if (!previewed) return;
        ['tender_total', 'calculation_type', 'amount', 'rate', 'percent_of_direct_cost', 'percent_of_total_price'].forEach(f => {
            if (previewed[f] !== undefined) row[f] = previewed[f];
        });
    });
    frm.refresh_field(fieldname);
}
