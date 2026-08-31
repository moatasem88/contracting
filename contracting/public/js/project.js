// Layout-only overrides for two collapsible sections that were built purely
// through Customize Form (Custom Field rows, no fixtures - see
// customer_details / cost_and_billing_section on the Project doctype).
//
// Frappe divides a Section's width evenly across every Column Break inside
// it (frappe/public/js/frappe/form/column.js resize_all_columns) - there is
// no per-field override, and no way to reset that division partway through
// a section. Splitting a field into its own Section Break to work around
// that instead breaks the section's collapse/expand toggle, since collapse
// state lives on the individual Section instance
// (frappe/public/js/frappe/form/section.js Section.collapse), not something
// that cascades to sibling sections. So both fixes below stay inside the
// original single section and override sizing with CSS instead.
frappe.ui.form.on("Project", {
	refresh(frm) {
		inject_project_layout_style();
		full_width_sales_order_details(frm);
		grid_layout_kpi_section(frm);
	},
});

function inject_project_layout_style() {
	if (document.getElementById("contracting-project-layout-style")) return;

	const style = document.createElement("style");
	style.id = "contracting-project-layout-style";
	style.textContent = `
		.form-column.contracting-full-width-column {
			flex: 0 0 100% !important;
			max-width: 100% !important;
		}
		.contracting-kpi-grid {
			display: grid;
			grid-template-columns: repeat(3, 1fr);
			column-gap: 15px;
		}
		.contracting-kpi-grid > [data-fieldname="cost_billing_details"] {
			grid-column: 1 / -1;
		}
		@media (max-width: 768px) {
			.contracting-kpi-grid {
				grid-template-columns: 1fr;
			}
		}
	`;
	document.head.appendChild(style);
}

function full_width_sales_order_details(frm) {
	const field = frm.fields_dict.sales_order_details;
	if (!field) return;
	field.$wrapper.closest(".form-column").addClass("contracting-full-width-column");
}

function grid_layout_kpi_section(frm) {
	const field = frm.fields_dict.total_progress_client_invoiced_amount;
	if (!field) return;
	field.$wrapper.parent().addClass("contracting-kpi-grid");
}
