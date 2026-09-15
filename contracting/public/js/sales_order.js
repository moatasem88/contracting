frappe.ui.form.on("Sales Order", {
	refresh: function (frm) {
		if (frm.doc.tender && frm.doc.docstatus === 0) {
			frm.dashboard.set_headline_alert(
				__(
					"Auto-created from Tender {0} and still in Draft. Review taxes and charges, then submit to finalize.",
					[frm.doc.tender]
				),
				"orange"
			);
		}
	},
});
