// Copyright (c) 2024, kazem and contributors
// For license information, please see license.txt

frappe.ui.form.on('Costing Note Template', {
	// refresh: function(frm) {

	// }


	calc_depresionsions_and_cost:(frm,row)=>{
	
		if(row.qty && row.unit_cost ){
			if(!row.depreciasion_percentage ){
				row.depreciasion_percentage =0
				row.total_expected_amount =row.qty
			}else{
				row.total_expected_amount = row.qty + row.qty * row.depreciasion_percentage / 100
			}
			row.total_cost = row.total_expected_amount * row.unit_cost
			row.total_cost_with_vat = row.total_cost 

			if(!row.vat ){
				row.vat_amount =0
				row.total_cost_with_vat = row.total_cost 
			}else{
				row.total_cost_with_vat = 0

				row.vat_amount =  row.total_cost * row.vat / 100
				row.total_cost_with_vat = (row.total_cost * row.vat / 100) + row.total_cost 
			}
			

			
			
			
		frm.events.calc_totals_table(frm)

		}
		frm.refresh_field("material_costs")
	},
	calc_totals:(frm,row,table_name)=>{
		if(row.qty && row.cost){
			row.total_cost = row.qty * row.cost
			row.total_cost_with_vat = row.total_cost 


			if(!row.vat ){
				row.vat_amount =0
				row.total_cost_with_vat = row.total_cost 
			}else{
				row.total_cost_with_vat = 0

				row.vat_amount =  row.total_cost * row.vat / 100
				row.total_cost_with_vat = (row.total_cost * row.vat / 100) + row.total_cost 
			}
			

			frm.events.calc_totals_table(frm)
		}
		
		frm.refresh_field(table_name);
	},
	calc_totals_table(frm){
		var total_material_costs = 0;
		var total_labor_costs = 0;
		var total_contractors_table = 0;
		var total_expenses_table = 0;
		var total_equibments_cost = 0
		var grand_totals = 0;
		try{
		for(let i=0;i<frm.doc.material_costs.length;i++){
			total_material_costs += frm.doc.material_costs[i].total_cost_with_vat
		}
		frm.set_value("total_material_costs",total_material_costs)
	}catch(r){

	}
	try{
		for(let i=0;i<frm.doc.labor_costs.length;i++){
			total_labor_costs += frm.doc.labor_costs[i].total_cost_with_vat
			
		}
		frm.set_value("total_labor_costs",total_labor_costs)
	}catch(r){

	}	
		try{
		for(let i=0;i<frm.doc.contractors_table.length;i++){
			total_contractors_table += frm.doc.contractors_table[i].total_cost_with_vat
		}
		//console.log("total_contractors_table",total_contractors_table)
		frm.set_value("total_contractors",total_contractors_table)
	}catch(r){
		console.log("error",r)
	}
	try{
		for(let i=0;i<frm.doc.expenses_table.length;i++){
			total_expenses_table += frm.doc.expenses_table[i].total_cost_with_vat
		}
		frm.set_value("total_expenses",total_expenses_table)
	}catch(r){

	}
	try{
		for(let i=0;i<frm.doc.equibments.length;i++){
			total_equibments_cost += frm.doc.equibments[i].total_cost_with_vat
		}
		frm.set_value("total_equibments_cost",total_equibments_cost)
	}catch(r){

	}
		grand_totals = total_material_costs + total_labor_costs  + total_contractors_table + total_expenses_table + total_equibments_cost
		frm.set_value("total_cost",grand_totals)
		frm.refresh_field("total_cost")
	}


});


frappe.ui.form.on('Material costs', {
	qty:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_depresionsions_and_cost(frm,row)
	},
	depreciasion_percentage:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_depresionsions_and_cost(frm,row)
	},
	unit_cost:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_depresionsions_and_cost(frm,row)
	},
	vat:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_depresionsions_and_cost(frm,row)
	}
});

frappe.ui.form.on('Labor costs', {
	qty:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"labor_costs")
	},
	cost:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"labor_costs")
	},
	vat:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"labor_costs")
	}
})


frappe.ui.form.on('Contractors table', {
	qty:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"contractors_table")
	},
	cost:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"contractors_table")
	},
	vat:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"contractors_table")
	}
})

frappe.ui.form.on('Expenses Table', {
	qty:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"expenses_table")
	},
	cost:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"expenses_table")
	},
	vat:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"expenses_table")
	}
}


)

frappe.ui.form.on('Equibment', {
	qty:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"equibments")
	},
	cost:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"equibments")
	},
	vat:(frm,cdt,cdn)=>{
		var row = locals[cdt][cdn]
		frm.events.calc_totals(frm,row,"equibments")
	}
})