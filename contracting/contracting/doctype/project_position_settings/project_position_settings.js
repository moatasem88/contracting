// Copyright (c) 2026, kazem and contributors
// For license information, please see license.txt

frappe.ui.form.on("Project Position Settings", {
	setup(frm) {
		frm.set_query("account", "guarantee_accounts", () => ({
			filters: { is_group: 0, company: frm.doc.company },
		}));
	},
});
