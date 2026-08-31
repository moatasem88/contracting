// Copyright (c) 2024, kazem and contributors
// For license information, please see license.txt

frappe.ui.form.on('Costing Note', {
	setup: function (frm) {
		frm.set_query("item", function () {

			return {
				filters: {
					item_group: frm.doc.contracting_item_group

				},
			};
		});
	},

	refresh: function (frm) {
		if (!frm.doc.__islocal) {



			// let show_button = false;

			// frm.doc.costing_note_merge_items.forEach(function(row) {
			//     if (row.remaining_qty > 0) {
			//         show_button = true;
			//     }
			// });

			if (frm.doc.docstatus == 1) {
				frm.add_custom_button(__('Create Cost Control Planning'), () => {


					let material_costs_items = []

					frm.doc.material_costs.forEach(function (item) {
						if (item.qty > 0) {
							material_costs_items.push({

								"item": item.item,
								"qty": item.qty,
								"depreciasion_percentage": item.depreciasion_percentage,
								"total_expected_amount": item.total_expected_amount,
								"unit_cost": item.unit_cost,
								"vat": item.vat,
								"vat_amount": item.vat_amount,
								"total_cost_with_vat": item.total_cost_with_vat

							});
						}
					});

					let labor_costs = []

					frm.doc.labor_costs.forEach(function (item) {
						if (item.qty > 0) {
							labor_costs.push({

								"item": item.item,
								"uom": item.uom,
								"qty": item.qty,
								"cost": item.cost,
								"total_cost": item.total_cost,
								"vat": item.vat,
								"vat_amount": item.vat_amount,
								"total_cost_with_vat": item.total_cost_with_vat
							});
						}
					});
					let contractors_table = []

					frm.doc.contractors_table.forEach(function (item) {
						if (item.qty > 0) {
							contractors_table.push({

								"item": item.item,
								"uom": item.uom,
								"qty": item.qty,
								"cost": item.cost,
								"total_cost": item.total_cost,
								"vat": item.vat,
								"vat_amount": item.vat_amount,
								"total_cost_with_vat": item.total_cost_with_vat
							});
						}
					});

					let expenses_table = []

					frm.doc.contractors_table.forEach(function (item) {
						if (item.qty > 0) {
							expenses_table.push({

								"item": item.item,
								"uom": item.uom,
								"qty": item.qty,
								"cost": item.cost,
								"total_cost": item.total_cost,
								"vat": item.vat,
								"vat_amount": item.vat_amount,
								"total_cost_with_vat": item.total_cost_with_vat
							});
						}
					});

					let equibments = []

					frm.doc.equibments.forEach(function (item) {
						if (item.qty > 0) {
							equibments.push({


								"item": item.item,
								"uom": item.uom,
								"qty": item.qty,
								"cost": item.cost,
								"total_cost": item.total_cost,
								"vat": item.vat,
								"vat_amount": item.vat_amount,
								"total_cost_with_vat": item.total_cost_with_vat
							});
						}
					});


					frappe.model.open_mapped_doc({
						method: "contracting.contracting.doctype.costing_note.costing_note.create_cost_control_planning",
						frm: cur_frm,
						args: {
							"contracting_item_group": frm.doc.contracting_item_group,
							"item": frm.doc.item,
							"name": frm.doc.name,

							"project_qty": frm.doc.project_qty,
							"unit": frm.doc.unit,
							"project": frm.doc.project,
							"tender": frm.doc.tender,
							"project_qty": frm.doc.project_qty,
							"costing_note_template": frm.doc.costing_note_template,
							"expected_time_period": frm.doc.expected_time_period,
							"project_qty": frm.doc.project_qty,
							"material_costs_items": material_costs_items,
							"labor_costs": labor_costs,
							"contractors_table": contractors_table,
							"expenses_table": expenses_table,
							"equibments": equibments,

							// "custom_project_type":frm.doc.project_type

						}
					})
				}
				)
			}
			// 	show_button = false

			//     frm.doc.purchase_items.forEach(function(row) {
			//         if (row.remaining_qty > 0) {
			//             show_button = true;
			//         }
			//     });
			// 	if (show_button) {

			// 	frm.add_custom_button(__('Purchase Order'), () => {


			// 		let items = []

			// 		frm.doc.purchase_items.forEach(function(row) {
			// 			if (row.remaining_qty > 0){
			// 			items.push({
			// 				"item_code": row.item_code,
			// 				"rate": row.rate,
			// 				"qty":row.remaining_qty,
			// 				"allowed_qty": row.remaining_qty,
			// 				"item_row":row.name


			// 			});}
			// 		});

			// 	frappe.model.open_mapped_doc({
			// 		method: "contracting.contracting.doctype.costing_note.costing_note.create_po",
			// 		frm: cur_frm,
			// 		args: {
			// 			"name": frm.doc.name,
			// 			"project": frm.doc.project,
			// 			"item": items,

			// 		}
			// 	})


			// 	},__('Create'));}
		}
	},

	costing_note_template: function (frm) {
		if (frm.doc.costing_note_template) {
			frappe.call({
				doc: frm.doc,
				method: "get_costing_note_template_data",
				callback: function (r) {
					if (r.message) {
						frm.clear_table("material_costs");
						frm.clear_table("labor_costs");
						frm.clear_table("contractors_table");
						frm.clear_table("expenses_table");
						frm.clear_table("equipments");

						frm.doc.total_material_costs = 0;
						frm.doc.total_labor_costs = 0;
						frm.doc.total_contractors = 0;
						frm.doc.total_expenses = 0;
						frm.doc.total_equibments_cost = 0;
						frm.doc.total_cost = 0;
						$.each(r.message.material_costs, function (i, d) {
							let row = frm.add_child("material_costs");
							row.item = d.item;
							row.qty = d.qty;
							row.depreciasion_percentage = d.depreciasion_percentage;
							row.total_expected_amount = d.total_expected_amount;
							row.unit_cost = d.unit_cost;
							row.total_cost = d.total_cost;
							row.vat = d.vat;
							row.vat_amount = d.vat_amount;
							row.total_cost_with_vat = d.total_cost_with_vat;

						});

						$.each(r.message.labor_costs, function (i, d) {
							let row = frm.add_child("labor_costs");
							row.item = d.item;
							row.uom = d.uom;
							row.qty = d.qty;
							row.cost = d.cost;
							row.total_cost = d.total_cost;
							row.vat = d.vat;
							row.vat_amount = d.vat_amount;
							row.total_cost_with_vat = d.total_cost_with_vat;

						});
						$.each(r.message.contractors_table, function (i, d) {
							let row = frm.add_child("contractors_table");
							row.item = d.item;
							row.uom = d.uom;
							row.qty = d.qty;
							row.cost = d.cost;
							row.total_cost = d.total_cost;
							row.vat = d.vat;
							row.vat_amount = d.vat_amount;
							row.total_cost_with_vat = d.total_cost_with_vat;

						});
						$.each(r.message.expenses_table, function (i, d) {
							let row = frm.add_child("expenses_table");
							row.item = d.item;
							row.uom = d.uom;
							row.qty = d.qty;
							row.cost = d.cost;
							row.total_cost = d.total_cost;
							row.vat = d.vat;
							row.vat_amount = d.vat_amount;
							row.total_cost_with_vat = d.total_cost_with_vat;

						});
						$.each(r.message.equibments, function (i, d) {
							let row = frm.add_child("equibments");
							row.item = d.item;
							row.uom = d.uom;
							row.qty = d.qty;
							row.cost = d.cost;
							row.total_cost = d.total_cost;
							row.vat = d.vat;
							row.vat_amount = d.vat_amount;
							row.total_cost_with_vat = d.total_cost_with_vat;

						});

						frm.set_value("total_material_costs", r.message.total_material_costs)
						frm.set_value("total_labor_costs", r.message.total_labor_costs)
						frm.set_value("total_contractors", r.message.total_contractors)
						frm.set_value("total_expenses", r.message.total_expenses)
						frm.set_value("total_equibments_cost", r.message.total_equibments_cost)
						frm.set_value("total_cost", r.message.total_cost)



						// Similarly for other tables

						frm.refresh_field("material_costs");
						frm.refresh_field("labor_costs");
						frm.refresh_field("contractors_table");
						frm.refresh_field("expenses_table");
						frm.refresh_field("equibments");
						frm.refresh_fields()
					}
				}
			});
		}
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


frappe.ui.form.on('Material costs', {
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
	}
});

frappe.ui.form.on('Labor costs', {
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
	}
})


frappe.ui.form.on('Contractors table', {
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
	}
})

frappe.ui.form.on('Expenses Table', {
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
	}
}


)

frappe.ui.form.on('Equibment', {
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
	}
})