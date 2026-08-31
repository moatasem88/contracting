frappe.ui.form.on("Task", {

    refresh: function (frm) {
		if (!frm.doc.__islocal ) {
		let show_button = false

		frm.doc.purchase_items.forEach(function (row) {
			if (row.remaining_qty > 0) {
				show_button = true;
			}
		});

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
					method:"contracting.contracting.controllers.task.create_purchase_order",
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
					method: "contracting.contracting.controllers.task.create_material_request",
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
	
})