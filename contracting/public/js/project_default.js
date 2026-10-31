// Project default from Cost Center (Project & Cost Center Position BRD,
// FR-15). When a Cost Center is picked and Project is still empty, fill in
// the Cost Center's Project - but only when exactly one Project maps to it;
// on a shared Cost Center the user picks and the submit check (FR-14)
// enforces it. The lookup is server-side because Accounts roles can't read
// Project on this site.
//
// Usage, from a doctype's `cost_center` field event:
//
//     contracting.project_position.default_project(frm);            // header
//     contracting.project_position.default_project(frm, cdt, cdn);  // item row

frappe.provide("contracting.project_position");

contracting.project_position.default_project = function (frm, cdt, cdn) {
	const doc = cdt ? locals[cdt][cdn] : frm.doc;
	if (!doc || !doc.cost_center || doc.project) return;

	const cost_center = doc.cost_center;
	frappe.call({
		method: "contracting.contracting.utils.project_position.project_for_cost_center",
		args: { cost_center },
		callback(r) {
			// The user may have typed a Project, or changed the Cost Center
			// again, while the call was in flight.
			if (!r.message || doc.project || doc.cost_center !== cost_center) return;
			if (cdt) {
				frappe.model.set_value(cdt, cdn, "project", r.message);
			} else {
				frm.set_value("project", r.message);
			}
		},
	});
};
