"""Won Tender -> native Sales Order.

Replaces create_contract_from_tender (contracting/utilis.py), which built a
Contract Document - this app's fork of ERPNext's Sales Order. That fork only
existed because a BOQ work item is a composite of Material/Labor/Equipment and
had no Item of its own, so it could never be a real Sales Order line. Work Item
Template V2 now auto-creates that Item, so the BOQ row carries a real item_code
and the native document works.

create_contract_from_tender is left in place, unused, as the provenance of the
Contract Documents created before this change.
"""

import erpnext
import frappe
from frappe.utils import today


def create_sales_order_from_tender(tender):
	company = erpnext.get_default_company()
	currency = erpnext.get_company_currency(company)

	so = frappe.new_doc("Sales Order")
	so.customer = tender.client
	so.company = company
	so.order_type = "Sales"
	so.transaction_date = today()
	# A contracting BOQ is billed by progress claim, never delivered on a
	# Delivery Note. Without this, validate_delivery_date() throws "Please
	# enter Delivery Date"; with it the order also lands straight in "To
	# Bill" instead of "To Deliver and Bill", which is what we want.
	so.skip_delivery_note = 1
	so.currency = currency
	so.conversion_rate = 1
	# Not frappe.db.get_value("Price List", {"selling": 1}) - that has no
	# order_by and resolves to whichever selling price list was touched most
	# recently, which on this bench is a USD one.
	so.selling_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	so.price_list_currency = currency
	so.plc_conversion_rate = 1
	so.project = tender.project
	so.tender = tender.name

	for row in tender.boq_items:
		if row.is_group:
			continue
		so.append("items", {
			"item_code": row.item_code,
			"item_name": row.item_name,
			# The per-tender scope text, not the template's reference copy.
			# description isn't in force_item_fields, so set_missing_values
			# leaves it alone and it survives through to the Sales Invoice.
			"description": row.description or row.item_name,
			"qty": row.original_quantity,
			"uom": row.uom,
			"conversion_factor": 1,
			"rate": row.sell_rate,
			"tender_boq_item": row.name,
		})

	so.run_method("set_missing_values")
	so.run_method("calculate_taxes_and_totals")
	so.insert(ignore_permissions=True)
	# Submitted, unlike the Contract Document this replaces: only a submitted
	# order runs update_project() (populating Project.total_sales_amount), and
	# only a submitted order can carry so_detail links from progress invoices,
	# which is what keeps per_billed coherent.
	so.submit()
	return so
