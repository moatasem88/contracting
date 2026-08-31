frappe.ui.form.on("Contract Document", {
	setup: function(frm) {
        console.log("fef")
		frm.custom_make_buttons = {
			'Delivery Note': 'Delivery Note',
			'Sales Invoice': 'Sales Invoice',
			'Material Request': 'Material Request',
			'Purchase Order': 'Purchase Order',
			'Project': 'Project',
			'Payment Entry': "Payment",
		}
		frm.add_fetch('customer', 'tax_id', 'tax_id');

		// formatter for material request item
		frm.set_indicator_formatter('item_code',
			function(doc) { return (doc.stock_qty<=doc.delivered_qty) ? "green" : "orange" })

		frm.set_query('company_address', function(doc) {
			if(!doc.company) {
				frappe.throw(__('Please set Company'));
			}

			return {
				query: 'frappe.contacts.doctype.address.address.address_query',
				filters: {
					link_doctype: 'Company',
					link_name: doc.company
				}
			};
		})
        console.log("fef")


        frm.set_query("account_head", "taxes", function (frm, cdt, cdn) {
			var row = locals[cdt][cdn];
            console.log(row)
			return {
				filters: {
				
				
				},
			};
		});
    },
    refresh: function(frm) {

        frm.set_query("account_head", "taxes", function (frm, cdt, cdn) {
			var row = locals[cdt][cdn];
            console.log(row)
			return {
				filters: {
					
				
				},
			};
		});
    },
    onload: function(frm) {

        frm.set_query("account_head", "taxes", function (frm, cdt, cdn) {
			var row = locals[cdt][cdn];
            console.log(row)
			return {
				filters: {
				
				
				},
			};
		});
    }


})