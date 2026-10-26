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
        refresh_billing_percentages(frm);
    },

    current_invoice_percentage: function(frm) {
        // FR-20: bulk-applies the header % as every current item row's own
        // percentage, then recomputes each such row's qty. A one-time
        // bulk-default action, not a live-bound ceiling - re-setting the
        // header later re-applies to all rows again, including ones a user
        // already hand-edited (FR-21, confirmed assumption, no sticky lock).
        if (single_sales_order(frm) === null) return;

        const percent = frm.doc.current_invoice_percentage || 0;
        const rows = (frm.doc.items || []).filter(row => row.so_detail);
        // Written straight onto the rows rather than via set_value - each
        // set_value fires the row % and qty triggers (ERPNext's qty one
        // re-fetches pricing rules and recalculates the whole table), which
        // froze the form on a 157-row invoice.
        rows.forEach(row => { row.current_invoice_percentage = percent; });
        apply_row_percentages(frm, rows);
    },

    items_add: function(frm) {
        toggle_percentage_billing_fields(frm);
        refresh_billing_percentages(frm);
    },

    items_remove: function(frm) {
        toggle_percentage_billing_fields(frm);
        refresh_billing_percentages(frm, {rows: false});
    }
});

frappe.ui.form.on("Sales Invoice Item", {
    so_detail: function(frm) {
        toggle_percentage_billing_fields(frm);
        refresh_billing_percentages(frm, {rows: false});
    },

    current_invoice_percentage: function(frm, cdt, cdn) {
        // FR-21: independent of the header/other rows - editing one row's
        // own percentage only recomputes that row's own qty.
        apply_row_percentages(frm, [locals[cdt][cdn]]);
    },

    qty: function(frm, cdt, cdn) {
        // FR-23: a direct qty edit recomputes that row's own percentage for
        // display only, without touching qty again.
        refresh_row_percentages(frm, [locals[cdt][cdn]]);
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

function get_billing_context(frm, rows) {
    // One call for every row, keyed by so_detail.
    return frappe.xcall('contracting.contracting.utils.progress_invoicing.get_sales_order_items_billing_context', {
        sales_order_items: rows.map(row => row.so_detail),
        exclude_invoice: frm.doc.name
    }).then(context => context || {});
}

function available_qty(ctx) {
    return ctx ? ctx.ordered_qty - ctx.invoiced_qty_excluding_this_draft : 0;
}

function set_row_percentages(rows, context) {
    rows.forEach(row => {
        const available = available_qty(context[row.so_detail]);
        row.current_invoice_percentage = available ? (row.qty / available * 100) : 0;
    });
}

function refresh_row_percentages(frm, rows) {
    rows = rows.filter(row => row.so_detail);
    if (!rows.length) return;
    get_billing_context(frm, rows).then(context => {
        set_row_percentages(rows, context);
        frm.refresh_field("items");
    });
}

function apply_row_percentages(frm, rows) {
    // qty/stock_qty set directly instead of through ERPNext's qty trigger:
    // progress billing keeps the Sales Order rate, so there's no pricing
    // rule to re-apply, and totals are recalculated once for all rows.
    rows = rows.filter(row => row.so_detail);
    if (!rows.length) return;
    get_billing_context(frm, rows).then(context => {
        rows.forEach(row => {
            if (!context[row.so_detail]) return;
            const qty = (row.current_invoice_percentage || 0) / 100 * available_qty(context[row.so_detail]);
            row.qty = flt(qty, precision("qty", row));
            row.stock_qty = flt(row.qty * row.conversion_factor, precision("stock_qty", row));
            row.total_weight = flt(row.stock_qty * row.weight_per_unit);
        });
        // Also refreshes every field, items grid included.
        frm.cscript.calculate_taxes_and_totals();
        frm.dirty();
    });
}

function refresh_billing_percentages(frm, {rows = true} = {}) {
    // Rows commonly arrive via "Get Items From > Sales Order" (a server-side
    // mapper, not per-field set_value calls), so the qty/so_detail triggers
    // above never fire for them and current_invoice_percentage is left blank
    // even though qty is already populated - backfilled here (rows) along
    // with FR-19's header previous_invoiced_percentage, scoped to the Sales
    // Order Items actually represented on this invoice's own rows
    // (confirmed assumption), not every item on the whole Sales Order.
    const so_detail_rows = (frm.doc.items || []).filter(r => r.so_detail);
    if (single_sales_order(frm) === null || !so_detail_rows.length) return;

    get_billing_context(frm, so_detail_rows).then(context => {
        if (rows) {
            set_row_percentages(so_detail_rows.filter(row => row.qty), context);
            frm.refresh_field("items");
        }
        let total_ordered = 0, total_invoiced = 0;
        so_detail_rows.forEach(row => {
            const ctx = context[row.so_detail];
            if (!ctx) return;
            total_ordered += ctx.ordered_qty;
            total_invoiced += ctx.invoiced_qty_excluding_this_draft;
        });
        frm.set_value("previous_invoiced_percentage", total_ordered ? (total_invoiced / total_ordered * 100) : 0);
    });
}
