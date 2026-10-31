// Copyright (c) 2026, kazem and contributors
// For license information, please see license.txt

const CCP_METHOD = "contracting.contracting.report.project_cost_center_position.project_cost_center_position";

frappe.query_reports["Project Cost Center Position"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: "Friends",
			read_only: 1,
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			description: __("Blank = from inception"),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "cost_center",
			label: __("Cost Center"),
			fieldtype: "MultiSelectList",
			get_data: (txt) =>
				frappe.db.get_link_options("Cost Center", txt, {
					company: frappe.query_report.get_filter_value("company"),
				}),
		},
		{
			fieldname: "project",
			label: __("Project"),
			fieldtype: "MultiSelectList",
			// Accounts roles can't read Project on this site, so the
			// standard link search would come back empty for them.
			get_data: (txt) =>
				frappe.xcall(`${CCP_METHOD}.get_project_options`, { txt }),
		},
	],

	tree: true,
	name_field: "row_id",
	parent_field: "parent_row",
	initial_depth: 1,

	formatter(value, row, column, data, default_formatter) {
		const formatted = default_formatter(value, row, column, data);
		if (column.fieldtype !== "Currency" || !data) return formatted;

		const style = flt(value) < 0 ? "color: var(--red-600);" : "";
		const html = data.is_total_row ? `<b>${formatted}</b>` : formatted;
		if (!flt(value)) return html;
		// Every non-zero amount opens the documents behind it (FR-12).
		return `<a class="ccp-drill" style="${style}"
			data-metric="${column.fieldname}"
			data-cost-center="${encodeURIComponent(data.cost_center || "")}"
			data-project="${encodeURIComponent(data.project || "")}">${html}</a>`;
	},

	onload(report) {
		report.page.wrapper.on("click", ".ccp-drill", function (e) {
			e.preventDefault();
			const $a = $(this);
			show_ccp_drilldown(report, {
				metric: $a.attr("data-metric"),
				cost_center: decodeURIComponent($a.attr("data-cost-center")) || null,
				project: decodeURIComponent($a.attr("data-project")) || null,
			});
		});
	},
};

function show_ccp_drilldown(report, args) {
	const column = report.columns.find((c) => c.fieldname === args.metric);
	frappe
		.xcall(`${CCP_METHOD}.get_drilldown`, { ...args, filters: report.get_values() })
		.then(({ rows, total }) => {
			const money = (v) => format_currency(v, frappe.defaults.get_default("currency"));
			const body = rows
				.map(
					(r) => `<tr>
						<td><a href="/app/${frappe.router.slug(r.voucher_type)}/${encodeURIComponent(r.voucher_no)}" target="_blank">
							${frappe.utils.escape_html(r.voucher_no)}</a>
							<div class="text-muted small">${__(r.voucher_type)}</div></td>
						<td>${frappe.datetime.str_to_user(r.posting_date) || ""}</td>
						<td>${frappe.utils.escape_html(r.party || "")}</td>
						<td>${frappe.utils.escape_html(r.cost_center)} / ${frappe.utils.escape_html(r.project)}</td>
						<td class="text-right">${money(r.amount)}</td>
					</tr>`
				)
				.join("");

			const dialog = new frappe.ui.Dialog({
				title: [column && column.name, args.project || args.cost_center || __("Total")]
					.filter(Boolean)
					.join(" · "),
				size: "extra-large",
				fields: [{ fieldtype: "HTML", fieldname: "lines" }],
			});
			dialog.fields_dict.lines.$wrapper.html(`
				<div style="max-height: 60vh; overflow: auto;">
				<table class="table table-bordered table-condensed">
					<thead><tr>
						<th>${__("Document")}</th><th>${__("Date")}</th><th>${__("Party")}</th>
						<th>${__("Cost Center / Project")}</th><th class="text-right">${__("Amount")}</th>
					</tr></thead>
					<tbody>${body || `<tr><td colspan="5" class="text-muted">${__("No documents")}</td></tr>`}</tbody>
					<tfoot><tr><th colspan="4">${__("Total")}</th><th class="text-right">${money(total)}</th></tr></tfoot>
				</table></div>`);
			dialog.show();
		});
}
