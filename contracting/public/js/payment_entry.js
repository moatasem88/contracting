frappe.ui.form.on("Payment Entry", {
	// A field event, not setup(): custom_payment_entry and cheques_management
	// also script this form, and field events from every app all run.
	cost_center(frm) {
		contracting.project_position.default_project(frm);
	},
});
