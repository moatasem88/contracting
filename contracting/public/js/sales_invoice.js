// Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Sales Invoice", {
    refresh: function(frm){
        frm.set_query("party_type", "taxes", function(doc, cdt, cdn) {
            const row = locals[cdt][cdn];

            return {
                filters: {
                    'name': ["in",["Customer","Supplier"]]
                }
            }
        });

        toggle_percentage_billing_fields(frm);
        refresh_previous_invoiced_percentage(frm);
        sync_row_percentages(frm);
    },

    current_invoice_percentage: function(frm) {
        // FR-20: bulk-applies the header % as every current item row's own
        // percentage, then recomputes each such row's qty. A one-time
        // bulk-default action, not a live-bound ceiling - re-setting the
        // header later re-applies to all rows again, including ones a user
        // already hand-edited (FR-21, confirmed assumption, no sticky lock).
        if (single_sales_order(frm) === null) return;

        const percent = frm.doc.current_invoice_percentage || 0;
        (frm.doc.items || []).forEach(row => {
            if (!row.so_detail) return;
            frappe.model.set_value(row.doctype, row.name, "current_invoice_percentage", percent);
            apply_row_percentage(frm, row.doctype, row.name);
        });
    },

    items_add: function(frm) {
        toggle_percentage_billing_fields(frm);
        refresh_previous_invoiced_percentage(frm);
        sync_row_percentages(frm);
    },

    items_remove: function(frm) {
        toggle_percentage_billing_fields(frm);
        refresh_previous_invoiced_percentage(frm);
    }
});

frappe.ui.form.on("Sales Invoice Item", {
    so_detail: function(frm) {
        toggle_percentage_billing_fields(frm);
        refresh_previous_invoiced_percentage(frm);
    },

    current_invoice_percentage: function(frm, cdt, cdn) {
        // FR-21: independent of the header/other rows - editing one row's
        // own percentage only recomputes that row's own qty.
        apply_row_percentage(frm, cdt, cdn);
    },

    qty: function(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        // Set by apply_row_percentage() itself - avoid recomputing the
        // percentage right back from the qty it just derived (that's
        // already consistent by construction).
        if (row.__setting_qty_from_percentage) return;
        // FR-23: a direct qty edit recomputes that row's own percentage for
        // display only, without touching qty again.
        refresh_row_percentage(frm, cdt, cdn);
    }
});

function single_sales_order(frm) {
    const sales_orders = new Set((frm.doc.items || []).filter(r => r.sales_order).map(r => r.sales_order));
    return sales_orders.size === 1 ? [...sales_orders][0] : null;
}

function toggle_percentage_billing_fields(frm) {
    // FR-18/TC-11: hidden (not just inert) whenever items span more than
    // one Sales Order - a static depends_on can't express a "distinct
    // count across child rows" check.
    const show = single_sales_order(frm) !== null;
    frm.toggle_display("current_invoice_percentage", show);
    frm.toggle_display("previous_invoiced_percentage", show);
}

function refresh_row_percentage(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (!row.so_detail) return;
    frappe.call({
        method: 'contracting.contracting.utils.progress_invoicing.get_sales_order_item_billing_context',
        args: {sales_order_item: row.so_detail, exclude_invoice: frm.doc.name},
        callback(r) {
            if (!r.message) return;
            const available = r.message.ordered_qty - r.message.invoiced_qty_excluding_this_draft;
            const percent = available ? (row.qty / available * 100) : 0;
            frappe.model.set_value(cdt, cdn, "current_invoice_percentage", percent);
        }
    });
}

function sync_row_percentages(frm) {
    // Rows commonly arrive via "Get Items From > Sales Order" (a server-side
    // mapper, not per-field set_value calls), so the qty/so_detail triggers
    // above never fire for them and current_invoice_percentage is left blank
    // even though qty is already populated. Called from refresh/items_add to
    // backfill it for every row that already carries a qty and a so_detail.
    (frm.doc.items || []).forEach(row => {
        if (row.so_detail && row.qty) {
            refresh_row_percentage(frm, row.doctype, row.name);
        }
    });
}

function apply_row_percentage(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (!row.so_detail) return;
    frappe.call({
        method: 'contracting.contracting.utils.progress_invoicing.get_sales_order_item_billing_context',
        args: {sales_order_item: row.so_detail, exclude_invoice: frm.doc.name},
        callback(r) {
            if (!r.message) return;
            const available = r.message.ordered_qty - r.message.invoiced_qty_excluding_this_draft;
            const qty = (row.current_invoice_percentage || 0) / 100 * available;
            row.__setting_qty_from_percentage = true;
            frappe.model.set_value(cdt, cdn, "qty", qty).then(() => {
                row.__setting_qty_from_percentage = false;
            });
        }
    });
}

function refresh_previous_invoiced_percentage(frm) {
    // FR-19: scoped to the Sales Order Items actually represented on this
    // invoice's own rows (confirmed assumption), not every item on the
    // whole Sales Order.
    const so_detail_rows = (frm.doc.items || []).filter(r => r.so_detail);
    if (single_sales_order(frm) === null || !so_detail_rows.length) return;

    Promise.all(so_detail_rows.map(row => new Promise(resolve => {
        frappe.call({
            method: 'contracting.contracting.utils.progress_invoicing.get_sales_order_item_billing_context',
            args: {sales_order_item: row.so_detail, exclude_invoice: frm.doc.name},
            callback(r) { resolve(r.message || {ordered_qty: 0, invoiced_qty_excluding_this_draft: 0}); }
        });
    }))).then(results => {
        const total_ordered = results.reduce((sum, r) => sum + r.ordered_qty, 0);
        const total_invoiced = results.reduce((sum, r) => sum + r.invoiced_qty_excluding_this_draft, 0);
        frm.set_value("previous_invoiced_percentage", total_ordered ? (total_invoiced / total_ordered * 100) : 0);
    });
}
