// Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
// License: GNU General Public License v3. See license.txt


{% include 'erpnext/selling/sales_common.js' %}

frappe.ui.form.on('Quotation', {
	setup: function(frm) {
		frm.custom_make_buttons = {
			'Sales Order': 'Sales Order'
		},

		frm.set_query("quotation_to", function() {
			return{
				"filters": {
					"name": ["in", ["Customer", "Lead"]],
				}
			}
		});

		frm.set_df_property('packed_items', 'cannot_add_rows', true);
		frm.set_df_property('packed_items', 'cannot_delete_rows', true);

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
		});
	},

	refresh: function(frm) {
		

		frm.trigger("set_label");
		frm.trigger("set_dynamic_field_label");

		// The "Create Contract -> Contract Owner" button lived here. It built
		// a Contract Document from the tender_items table, which this app no
		// longer creates. It is not re-pointed at Sales Order because this
		// same file already registers ERPNext's native Quotation -> Sales
		// Order button (see "Make Sales Order" below), and that one maps the
		// real priced items table the Quotation's own totals come from,
		// rather than the parallel tender_items one.
	},

	quotation_to: function(frm) {
		frm.trigger("set_label");
		frm.trigger("toggle_reqd_lead_customer");
		frm.trigger("set_dynamic_field_label");
	},

	set_label: function(frm) {
		frm.fields_dict.customer_address.set_label(__(frm.doc.quotation_to + " Address"));
	}
});

erpnext.selling.QuotationController = class QuotationController extends erpnext.selling.SellingController {
	onload(doc, dt, dn) {
		var me = this;
		super.onload(doc, dt, dn);

	}
	party_name() {
		var me = this;
		erpnext.utils.get_party_details(this.frm, null, null, function() {
			me.apply_price_list();
		});

		if(me.frm.doc.quotation_to=="Lead" && me.frm.doc.party_name) {
			me.frm.trigger("get_lead_details");
		}
	}
	refresh(doc, dt, dn) {
		super.refresh(doc, dt, dn);
				frappe.dynamic_link = {
			doc: this.frm.doc,
			fieldname: 'party_name',
			doctype: doc.quotation_to == 'Customer' ? 'Customer' : 'Lead',
		};

		var me = this;

		if (doc.__islocal && !doc.valid_till) {
			if(frappe.boot.sysdefaults.quotation_valid_till){
				this.frm.set_value('valid_till', frappe.datetime.add_days(doc.transaction_date, frappe.boot.sysdefaults.quotation_valid_till));
			} else {
				this.frm.set_value('valid_till', frappe.datetime.add_months(doc.transaction_date, 1));
			}
		}
        
        
		if (doc.docstatus == 1 && !["Lost", "Ordered"].includes(doc.status)) {

			if (frappe.boot.sysdefaults.allow_sales_order_creation_for_expired_quotation
				|| (!doc.valid_till)
				|| frappe.datetime.get_diff(doc.valid_till, frappe.datetime.get_today()) >= 0) {
					this.frm.add_custom_button(
						__("Sales Order"),
						this.frm.cscript["Make Sales Order"],
						__("Create")
					);
				}
                
			if(doc.status!=="Ordered") {
				this.frm.add_custom_button(__('Set as Lost'), () => {
						this.frm.trigger('set_as_lost_dialog');
					});
				}

			if(!doc.auto_repeat) {
				cur_frm.add_custom_button(__('Subscription'), function() {
					erpnext.utils.make_subscription(doc.doctype, doc.name)
				}, __('Create'))
			}

            

			cur_frm.page.set_inner_btn_group_as_primary(__('Create'));
		}

		if (this.frm.doc.docstatus===0) {
			this.frm.add_custom_button(__('Opportunity'),
				function() {
					erpnext.utils.map_current_doc({
						method: "erpnext.crm.doctype.opportunity.opportunity.make_quotation",
						source_doctype: "Opportunity",
						target: me.frm,
						setters: [
							{
								label: "Party",
								fieldname: "party_name",
								fieldtype: "Link",
								options: me.frm.doc.quotation_to,
								default: me.frm.doc.party_name || undefined
							},
							{
								label: "Opportunity Type",
								fieldname: "opportunity_type",
								fieldtype: "Link",
								options: "Opportunity Type",
								default: me.frm.doc.order_type || undefined
							}
						],
						get_query_filters: {
							status: ["not in", ["Lost", "Closed"]],
							company: me.frm.doc.company
						}
					})
				}, __("Get Items From"), "btn-default");
		}

		this.toggle_reqd_lead_customer();

	}

	set_dynamic_field_label(){
		if (this.frm.doc.quotation_to == "Customer")
		{
			this.frm.set_df_property("party_name", "label", "Customer");
			this.frm.fields_dict.party_name.get_query = null;
		}

		if (this.frm.doc.quotation_to == "Lead")
		{
			this.frm.set_df_property("party_name", "label", "Lead");

			this.frm.fields_dict.party_name.get_query = function() {
				return{	query: "erpnext.controllers.queries.lead_query" }
			}
		}
	}

	toggle_reqd_lead_customer() {
		var me = this;

		// to overwrite the customer_filter trigger from queries.js
		this.frm.toggle_reqd("party_name", this.frm.doc.quotation_to);
		this.frm.set_query('customer_address', this.address_query);
		this.frm.set_query('shipping_address_name', this.address_query);
	}

	tc_name() {
		this.get_terms();
	}

	address_query(doc) {
		return {
			query: 'frappe.contacts.doctype.address.address.address_query',
			filters: {
				link_doctype: frappe.dynamic_link.doctype,
				link_name: doc.party_name
			}
		};
	}

	validate_company_and_party(party_field) {
		if(!this.frm.doc.quotation_to) {
			frappe.msgprint(__("Please select a value for {0} quotation_to {1}", [this.frm.doc.doctype, this.frm.doc.name]));
			return false;
		} else if (this.frm.doc.quotation_to == "Lead") {
			return true;
		} else {
			return this._super(party_field);
		}
	}

	get_lead_details() {
		var me = this;
		if(!this.frm.doc.quotation_to === "Lead") {
			return;
		}

		frappe.call({
			method: "erpnext.crm.doctype.lead.lead.get_lead_details",
			args: {
				'lead': this.frm.doc.party_name,
				'posting_date': this.frm.doc.transaction_date,
				'company': this.frm.doc.company,
			},
			callback: function(r) {
				if(r.message) {
					me.frm.updating_party_details = true;
					me.frm.set_value(r.message);
					me.frm.refresh();
					me.frm.updating_party_details = false;

				}
			}
		})
	}
};

cur_frm.script_manager.make(erpnext.selling.QuotationController);

cur_frm.cscript['Make Sales Order'] = function() {
	frappe.model.open_mapped_doc({
		method: "erpnext.selling.doctype.quotation.quotation.make_sales_order",
		frm: cur_frm
	})
}

frappe.ui.form.on("Quotation Item", "items_on_form_rendered", "packed_items_on_form_rendered", function(frm, cdt, cdn) {
	// enable tax_amount field if Actual
})

frappe.ui.form.on("Quotation Item", "stock_balance", function(frm, cdt, cdn) {
	var d = frappe.model.get_doc(cdt, cdn);
	frappe.route_options = {"item_code": d.item_code};
	frappe.set_route("query-report", "Stock Balance");
})
// frappe.ui.form.on('Tender Item', {
//     qty: function(frm, cdt, cdn) {
//         calculate_totals(frm);
//     },
//     rate: function(frm, cdt, cdn) {
//         calculate_totals(frm);
//     },
//     tender_items_add: function(frm, cdt, cdn) {
//         calculate_totals(frm);
//     },
//     tender_items_remove: function(frm, cdt, cdn) {
//         calculate_totals(frm);
//     }
// });

// function calculate_totals(frm) {
//     let total_quantity = 0;
//     let total_amount = 0;

//     // Loop through tender items and calculate totals
//     $.each(frm.doc.tender_items || [], function(i, item) {
//         let qty = parseFloat(item.qty) || 0;
//         let rate = parseFloat(item.rate) || 0;

//         let item_amount = qty * rate;
//         frappe.model.set_value(item.doctype, item.name, 'amount', item_amount);

//         total_quantity += qty;
//         total_amount += item_amount;
//     });

//     frm.set_value('total_qty', total_quantity);
//     frm.set_value('total', total_amount);

//     let total_taxes_and_charges = 0;
//     $.each(frm.doc.taxes || [], function(i, tax) {
//         let tax_amount = (total_amount * parseFloat(tax.rate)) / 100;
//         total_taxes_and_charges += tax_amount;
//     });

//     frm.set_value('total_taxes_and_charges', total_taxes_and_charges);

//     let grand_total = total_amount + total_taxes_and_charges;
//     frm.set_value('grand_total', grand_total);

//     let rounding_adjustment = Math.round(grand_total * 100) / 100 - grand_total;
//     frm.set_value('rounding_adjustment', rounding_adjustment);

//     let rounded_total = Math.round(grand_total * 100) / 100;
//     frm.set_value('rounded_total', rounded_total);

//     // Trigger a refresh to ensure the form updates
//     frm.refresh_fields(['total_qty', 'total', 'total_taxes_and_charges', 'grand_total', 'rounding_adjustment', 'rounded_total']);
// }