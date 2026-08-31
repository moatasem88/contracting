# Copyright (c) 2026, kazem and Contributors
# See license.txt

"""Tests for the Project "Cost and Billing" rollup (Won Project Tender BRD,
2026-08-28) - contracting.contracting.utils.project_cost_billing.
TC-03/04/05/08, plus the new row-level purchase-tracing plumbing this BRD
added beyond the source document's own scope (see OQ-1 resolution)."""

import erpnext
import frappe
from frappe.tests.utils import FrappeTestCase

from contracting.contracting.doctype.tender.test_tender import make_template
from contracting.contracting.doctype.project_tender.test_project_tender import (
	ensure_ic_item,
	make_ic_project_tender,
)


def make_won_project(project_label, rates, qty=1):
	"""Wins a Project Tender with one Tender per (category, rate) pair in
	`rates` and returns the resulting Project, freshly reloaded. `qty` is the
	original_quantity/qty_per_unit shared by every Tender in this call - it
	only varies per-call, not per-tender, since callers so far only ever
	need one qty per project (default of 1 preserves every existing caller's
	behavior unchanged)."""
	pt = make_ic_project_tender(f"__cb_{project_label}__")
	tenders = []
	for category, rate in rates:
		template = make_template("_cb_wit_" + frappe.generate_hash(length=8))
		tender = frappe.get_doc({
			"doctype": "Tender",
			"naming_series": "TND-.YYYY.-",
			"project_name": f"__cb_{project_label}_{category}__",
			"client": frappe.db.get_value("Customer", {}, "name"),
			"tender_category": category,
			"project_tender": pt.name,
		})
		tender.append("boq_items", {
			"item_name": "Cost Billing Work Item", "original_quantity": qty,
			"work_item_template": template.name,
		})
		tender.insert(ignore_permissions=True)
		tender.append("material_items", {
			"work_item": tender.boq_items[0].idx, "item": ensure_ic_item(),
			"qty_per_unit": 1, "rate": rate, "currency": "EGP",
		})
		tender.save(ignore_permissions=True)
		tender.status = "Complete"
		tender.save(ignore_permissions=True)
		tenders.append(tender)

	pt.reload()
	for tender in tenders:
		pt.append("direct_cost_details", {"tender": tender.name, "tender_category": tender.tender_category})
	pt.status = "Won"
	pt.save(ignore_permissions=True)

	project = frappe.get_doc("Project", frappe.db.get_value("Project", {"project_tender": pt.name}, "name"))
	return project, tenders


def submit_payment_entry_for(sales_invoice_name, project=None):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry("Sales Invoice", sales_invoice_name)
	if project:
		# get_payment_entry doesn't carry project over from the invoice -
		# FR-10's total_paid_amount (and everything computed off it) reads
		# Payment Entry.project directly, same as real data entry would.
		pe.project = project
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe


class TestProjectCostBilling(FrappeTestCase):
	def test_tc03_progress_percentage_rollup(self):
		project, tenders = make_won_project("tc03", [("Civil", 100000)])
		so_row = project.sales_order_details[0]
		soi = frappe.db.get_value(
			"Sales Order Item", {"parent": so_row.sales_order}, ["name", "qty", "rate"], as_dict=True
		)
		so_grand_total = frappe.db.get_value("Sales Order", so_row.sales_order, "grand_total")
		claimed_qty = soi.qty * 0.4
		expected_amount = claimed_qty * soi.rate

		frappe.get_doc({
			"doctype": "Client Progress Invoice",
			"sales_order": so_row.sales_order,
			"status": "Invoiced",
			"items": [{
				"sales_order_item": soi.name,
				"cumulative_qty_complete": claimed_qty,
			}],
		}).insert(ignore_permissions=True)

		project.reload()
		row = project.sales_order_details[0]
		self.assertAlmostEqual(row.progress_amount, expected_amount, delta=0.01)
		self.assertAlmostEqual(row.progress_percentage, expected_amount / so_grand_total * 100, delta=0.01)

	def test_tc04_payments_collected_proportional_split(self):
		project, tenders = make_won_project("tc04", [("Civil", 60000), ("Electrical", 40000)])
		so_a, so_b = (r.sales_order for r in project.sales_order_details)

		si = frappe.new_doc("Sales Invoice")
		si.customer = project.customer
		si.company = erpnext.get_default_company()
		cost_center = frappe.db.get_value("Company", si.company, "cost_center") or frappe.db.get_value(
			"Cost Center", {"company": si.company, "is_group": 0}, "name"
		)
		si.due_date = frappe.utils.today()
		si.project = project.name
		si.cost_center = cost_center
		for so_name in (so_a, so_b):
			soi = frappe.db.get_value(
				"Sales Order Item", {"parent": so_name}, ["item_code", "qty", "rate"], as_dict=True
			)
			si.append("items", {
				"item_code": soi.item_code, "qty": soi.qty, "rate": soi.rate,
				"cost_center": cost_center, "sales_order": so_name, "project": project.name,
			})
		si.insert(ignore_permissions=True)
		si.submit()

		submit_payment_entry_for(si.name, project=project.name)

		project.reload()
		by_so = {r.sales_order: r for r in project.sales_order_details}
		self.assertAlmostEqual(by_so[so_a].payments_collected, 60000, delta=0.01)
		self.assertAlmostEqual(by_so[so_b].payments_collected, 40000, delta=0.01)

	def test_tc05_purchases_percent_can_exceed_100(self):
		project, tenders = make_won_project("tc05", [("Civil", 1000)])

		# total_paid_amount (the % denominator) is outgoing spend only -
		# payment_type "Pay", not a customer "Receive" collection.
		company = erpnext.get_default_company()
		bank_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Bank", "is_group": 0}, "name"
		)
		payable_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Payable", "is_group": 0}, "name"
		)
		cost_center = frappe.db.get_value("Company", company, "cost_center") or frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0}, "name"
		)
		pe = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Pay",
			"party_type": "Supplier",
			"party": frappe.db.get_value("Supplier", {}, "name"),
			"company": company,
			"project": project.name,
			"cost_center": cost_center,
			"paid_from": bank_account,
			"paid_to": payable_account,
			"paid_amount": 500,
			"received_amount": 500,
			"reference_no": "CB-TEST-05",
			"reference_date": frappe.utils.today(),
		})
		pe.insert(ignore_permissions=True)
		pe.submit()
		warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		supplier = frappe.db.get_value("Supplier", {}, "name")
		expense_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Cost of Goods Sold", "is_group": 0}, "name"
		)
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"supplier": supplier,
			"company": company,
			"project": project.name,
			"set_warehouse": warehouse,
			"items": [{
				"item_code": tenders[0].boq_items[0].item_code, "qty": 1, "rate": 1600,
				"warehouse": warehouse, "cost_center": cost_center, "expense_account": expense_account,
			}],
		})
		pr.insert(ignore_permissions=True)
		pr.submit()

		project.reload()
		self.assertAlmostEqual(project.total_purchases_amount, 1600, delta=0.01)
		self.assertGreater(project.total_purchases_percent, 100)

	def test_tc08_targeted_profit_percent_defaults_to_zero(self):
		project, tenders = make_won_project("tc08", [("Civil", 500)])
		self.assertEqual(project.targeted_profit_percent, 0)

	def test_row_level_purchase_tracing_by_tender(self):
		project, tenders = make_won_project("rowpurchase", [("Civil", 1000), ("Electrical", 500)])
		civil_boq_item = tenders[0].boq_items[0]
		project_boq_item = frappe.db.get_value(
			"Project BOQ Item",
			{"boq_proj": project.name, "source_tender_boq_item": civil_boq_item.name},
			"name",
		)
		self.assertTrue(project_boq_item)

		company = erpnext.get_default_company()
		warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		supplier = frappe.db.get_value("Supplier", {}, "name")
		cost_center = frappe.db.get_value("Company", company, "cost_center") or frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0}, "name"
		)
		expense_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Cost of Goods Sold", "is_group": 0}, "name"
		)
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"supplier": supplier,
			"company": company,
			"project": project.name,
			"set_warehouse": warehouse,
			"items": [{
				"item_code": civil_boq_item.item_code, "qty": 1, "rate": 250,
				"warehouse": warehouse, "cost_center": cost_center, "expense_account": expense_account,
				"custom_project_work_item": project_boq_item,
			}],
		})
		pr.insert(ignore_permissions=True)
		pr.submit()

		project.reload()
		by_tender = {r.tender: r for r in project.cost_billing_details}
		self.assertAlmostEqual(by_tender[tenders[0].name].total_purchases_amount, 250, delta=0.01)
		self.assertEqual(by_tender[tenders[1].name].total_purchases_amount, 0)
