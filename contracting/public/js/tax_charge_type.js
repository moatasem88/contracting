// Populates a Sales/Purchase Taxes and Charges row's native fields from the
// selected Tax and Charge Type - deliberately NOT a fetch_from Property
// Setter (see contracting.custom_fields.PROPERTY_SETTERS): core's Link
// control clears every fetch_from target the instant it parses an empty
// value, which wiped these rows' description/charge_type/account_head/rate
// back to blank well after a user had typed them (surfacing at save as
// "Value missing for: Description"). This handler only ever fires on a
// genuine selection and never touches these fields when the link is blank.

frappe.ui.form.on("Sales Taxes and Charges", {
    custom_tax_charge_type: function(frm, cdt, cdn) {
        apply_tax_charge_type_defaults(frm, cdt, cdn);
    }
});

frappe.ui.form.on("Purchase Taxes and Charges", {
    custom_tax_charge_type: function(frm, cdt, cdn) {
        apply_tax_charge_type_defaults(frm, cdt, cdn);
    }
});

function apply_tax_charge_type_defaults(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (!row.custom_tax_charge_type) return;

    frappe.db.get_doc("Tax and Charge Type", row.custom_tax_charge_type).then(doc => {
        frappe.model.set_value(cdt, cdn, "charge_type", doc.default_charge_type);
        frappe.model.set_value(cdt, cdn, "account_head", doc.default_account_head);
        frappe.model.set_value(cdt, cdn, "description", doc.default_description);
        frappe.model.set_value(cdt, cdn, "rate", doc.default_rate);
        frappe.model.set_value(cdt, cdn, "included_in_print_rate", doc.default_included_in_print_rate);
        if (cdt === "Purchase Taxes and Charges") {
            frappe.model.set_value(cdt, cdn, "add_deduct_tax", doc.default_add_deduct_tax);
        }
    });
}
