// Copyright (c) 2024, kazem and contributors
// For license information, please see license.txt

frappe.ui.form.on('Cost Control Planning', {
	refresh: function (frm) {
		if (!frm.doc.__islocal && frm.doc.docstatus == 1) {
		let show_button = false

		frm.doc.purchase_items.forEach(function (row) {
			if (row.remaining_qty > 0) {
				show_button = true;
			}
		});

		console.log(show_button)
		if (show_button) {

			frm.add_custom_button(__('Purchase Order'), () => {


				let items = []

				frm.doc.purchase_items.forEach(function (row) {
					if (row.remaining_qty > 0) {
						items.push({
							"item_code": row.item_code,
							"rate": row.rate,
							"qty": row.remaining_qty,
							"allowed_qty": row.remaining_qty,
							"item_row": row.name


						});
					}
				});

				frappe.model.open_mapped_doc({
					method: "contracting.contracting.doctype.cost_control_planning.cost_control_planning.create_purchase_order",
					frm: cur_frm,
					args: {
						"name": frm.doc.name,
						"project": frm.doc.project,
						"item": items,

					}
				})


			}, __('Create'));

		}


	

			 show_button = false;

			frm.doc.costing_note_merge_items.forEach(function (row) {
				if (row.remaining_qty > 0) {
					show_button = true;
				}
			});

			if (show_button) {
				frm.add_custom_button(__('Material Request'), () => {
					let mitems = []

				frm.doc.costing_note_merge_items.forEach(function (row) {
					if (row.remaining_qty > 0) {
						mitems.push({
							"item_code": row.item_code,
							"rate": row.rate,
							"qty": row.remaining_qty,
							"allowed_qty": row.remaining_qty,
							"item_row": row.name


						});
					}
				});

				frappe.model.open_mapped_doc({
					method: "contracting.contracting.doctype.cost_control_planning.cost_control_planning.create_material_request",
					frm: cur_frm,
					args: {
						"name": frm.doc.name,
						"project": frm.doc.project,
						"costing_note_merge_items": mitems,

					}
				})

				}, __('Create'));
			}

		}
	},
	

		

	
	create_tasks: function (frm) {
		frappe.call({
			doc: frm.doc,
			method: "create_tasks",

			callback: function (r) {
				if (r.message) {
					frappe.msgprint(r.message);
				}
			}
		});
	},

	calc_depresionsions_and_cost: (frm, row) => {

		if (row.qty && row.unit_cost) {
			if (!row.depreciasion_percentage) {
				row.depreciasion_percentage = 0
				row.total_expected_amount = row.qty
			} else {
				row.total_expected_amount = row.qty + row.qty * row.depreciasion_percentage / 100
			}
			row.total_cost = row.total_expected_amount * row.unit_cost
			row.total_cost_with_vat = row.total_cost

			if (!row.vat) {
				row.vat_amount = 0
				row.total_cost_with_vat = row.total_cost
			} else {
				row.total_cost_with_vat = 0

				row.vat_amount = row.total_cost * row.vat / 100
				row.total_cost_with_vat = (row.total_cost * row.vat / 100) + row.total_cost
			}

			frm.events.calc_totals_table(frm)

		}
		frm.refresh_field("material_costs")
	},
	calc_totals: (frm, row, table_name) => {
		if (row.qty && row.cost) {
			row.total_cost = row.qty * row.cost
			row.total_cost_with_vat = row.total_cost


			if (!row.vat) {
				row.vat_amount = 0
				row.total_cost_with_vat = row.total_cost
			} else {
				row.total_cost_with_vat = 0

				row.vat_amount = row.total_cost * row.vat / 100
				row.total_cost_with_vat = (row.total_cost * row.vat / 100) + row.total_cost
			}


			frm.events.calc_totals_table(frm)
		}

		frm.refresh_field(table_name);
	},
	calc_totals_table(frm) {
		var total_material_costs = 0;
		var total_labor_costs = 0;
		var total_contractors_table = 0;
		var total_expenses_table = 0;
		var total_equibments_cost = 0
		var grand_totals = 0;
		try {
			for (let i = 0; i < frm.doc.material_costs.length; i++) {
				total_material_costs += frm.doc.material_costs[i].total_cost_with_vat
			}
			frm.set_value("total_material_costs", total_material_costs)
		} catch (r) {

		}
		try {
			for (let i = 0; i < frm.doc.labor_costs.length; i++) {
				total_labor_costs += frm.doc.labor_costs[i].total_cost_with_vat

			}
			frm.set_value("total_labor_costs", total_labor_costs)
		} catch (r) {

		}
		try {
			for (let i = 0; i < frm.doc.contractors_table.length; i++) {
				total_contractors_table += frm.doc.contractors_table[i].total_cost_with_vat
			}
			//console.log("total_contractors_table",total_contractors_table)
			frm.set_value("total_contractors", total_contractors_table)
		} catch (r) {
			console.log("error", r)
		}
		try {
			for (let i = 0; i < frm.doc.expenses_table.length; i++) {
				total_expenses_table += frm.doc.expenses_table[i].total_cost_with_vat
			}
			frm.set_value("total_expenses", total_expenses_table)
		} catch (r) {

		}
		try {
			for (let i = 0; i < frm.doc.equibments.length; i++) {
				total_equibments_cost += frm.doc.equibments[i].total_cost_with_vat
			}
			frm.set_value("total_equibments_cost", total_equibments_cost)
		} catch (r) {

		}
		grand_totals = total_material_costs + total_labor_costs + total_contractors_table + total_expenses_table + total_equibments_cost
		frm.set_value("total_cost", grand_totals)
		frm.refresh_field("total_cost")
	}
});
// Reusable function to create the dialog for task allocation
function createTaskAllocationDialog(data, frm) {
	let d = new frappe.ui.Dialog({
		title: __("Select Task"),
		size: "large",
		fields: [
			{
				fieldname: "tasks",
				fieldtype: "Table",
				label: "Tasks",
				fields: [
					{
						fieldtype: "Data",
						fieldname: "task",
						label: __("Task"),
						read_only: 1,
						in_list_view: 1,
					},
					{
						fieldtype: "Float",
						fieldname: "rate",
						label: __("Rate"),
						reqd: 1,
						in_list_view: 1,
					},
					{
						fieldtype: "Float",
						fieldname: "qty",
						label: __("Qty"),
						reqd: 1,
						in_list_view: 1,
					},
				],
			},
		],
		primary_action_label: __("Create"),
		primary_action(values) {
			let selected_items = d.fields_dict.tasks.grid.get_selected_children();
			if (selected_items.length === 0) {
				frappe.throw({
					message: "Please select <b>Task</b> from the Table",
					title: __("Task Required"),
					indicator: "blue",
				});
			}

			frappe.call({
				method: "contracting.contracting.doctype.cost_control_planning.cost_control_planning.append_task_items",
				args: {
					args: selected_items,
					row: data,
				},
				callback: function (r) {
					frm.reload_doc();
				},
			});

			d.hide();
		},
	});

	// Function to set the task data in the dialog
	function setTaskData(dialog) {
		let task_items = [];
		frm.doc.tasks.forEach((item) => {
			if (frm.doc.tasks.length) {
				task_items.push({
					task: item.task_name,
					rate: data.total_cost_with_vat / data.qty,
					qty: data.qty,
				});
			}
		});
		dialog.fields_dict["tasks"].df.data = task_items;
		dialog.get_field("tasks").refresh();
	}

	setTaskData(d);
	d.wrapper.find(".grid-heading-row .grid-row-check").click();
	d.show();
}

// Event handlers for Material costs and Labor costs
frappe.ui.form.on("Material costs", {
	qty: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_depresionsions_and_cost(frm, row)
	},
	depreciasion_percentage: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_depresionsions_and_cost(frm, row)
	},
	unit_cost: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_depresionsions_and_cost(frm, row)
	},
	vat: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_depresionsions_and_cost(frm, row)
	},
	allocate: function (frm, cdt, cdn) {

		var data = locals[cdt][cdn];
		createTaskAllocationDialog(data, frm);

	},
});

frappe.ui.form.on("Labor costs", {
	qty: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "labor_costs")
	},
	cost: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "labor_costs")
	},
	vat: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "labor_costs")
	},
	allocate: function (frm, cdt, cdn) {
		var data = locals[cdt][cdn];
		createTaskAllocationDialog(data, frm);
	},
});


frappe.ui.form.on("Equibment", {
	qty: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "equibments")
	},
	cost: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "equibments")
	},
	vat: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "equibments")
	},
	allocate: function (frm, cdt, cdn) {
		var data = locals[cdt][cdn];
		createTaskAllocationDialog(data, frm);
	},
});

frappe.ui.form.on("Expenses Table", {
	qty: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "expenses_table")
	},
	cost: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "expenses_table")
	},
	vat: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "expenses_table")
	},
	allocate: function (frm, cdt, cdn) {
		var data = locals[cdt][cdn];
		createTaskAllocationDialog(data, frm);
	},
});

frappe.ui.form.on("Contractors table", {
	qty: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "contractors_table")
	},
	cost: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "contractors_table")
	},
	vat: (frm, cdt, cdn) => {
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm, row, "contractors_table")
	},
	allocate: function (frm, cdt, cdn) {
		var data = locals[cdt][cdn];
		createTaskAllocationDialog(data, frm);
	},
});