frappe.ui.form.on("Material Request", {
    setup: function(frm) {
        frm.set_query("costing_note", function () {
			
			return {
				filters: {
					project : frm.doc.project
				
				},
			};
		});
		frm.custom_make_buttons = {
			'Stock Entry': 'Issue Material',
			'Pick List': 'Pick List',
			'Purchase Order': 'Purchase Order',
			'Request for Quotation': 'Request for Quotation',
			'Supplier Quotation': 'Supplier Quotation',
			'Work Order': 'Work Order',
			'Purchase Receipt': 'Purchase Receipt'
		};

		// formatter for material request item
		frm.set_indicator_formatter('item_code',
			function(doc) { return (doc.stock_qty<=doc.ordered_qty) ? "green" : "orange"; });

		frm.set_query("item_code", "items", function() {
			return {
				query: "erpnext.controllers.queries.item_query"
			};
		});

		frm.set_query("from_warehouse", "items", function(doc) {
			return {
				filters: {'company': doc.company}
			};
		});

		frm.set_query("bom_no", "items", function(doc, cdt, cdn) {
			var row = locals[cdt][cdn];
			return {
				filters: {
					"item": row.item_code
				}
			}
		});

		// Work items are the project's own BOQ lines.
		frm.set_query("project_work_item", "custom_work_items", function(doc) {
			return {
				filters: { boq_proj: doc.project }
			};
		});

		// Each item line draws against one of the work items selected above.
		frm.set_query("custom_project_work_item", "items", function(doc) {
			return {
				filters: {
					name: ["in", (doc.custom_work_items || [])
						.map(function(r) { return r.project_work_item; })
						.filter(Boolean)]
				}
			};
		});
	},
    costing_note: function(frm){
        if (frm.doc.costing_note){
        frappe.call({
            method: "contracting.utilis.get_costing_note_items_mt",
            args: {
                doc: frm.doc,
            },
            callback: function(r) {
                if(r.message) {
                    frm.clear_table("items");
                    $.each(r.message, function(i, d) {
                        let row = frm.add_child("items");
                        console.log(d)
                        $.extend(row, d);
                    });
                    frm.refresh_field("items");
                }
            }
        });
    }}
});
