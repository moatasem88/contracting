# Copyright (c) 2026, kazem and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from contracting.contracting.api import create_work_item_group
from contracting.contracting.doctype.client_progress_invoice.client_progress_invoice import (
	create_sales_invoice_from_backlog,
	get_project_sales_orders,
	get_sales_order_items,
)
from contracting.contracting.doctype.tender.test_tender import ensure_customer
from contracting.contracting.utils.test_project_cost_billing import make_won_project


def won_sales_order(suffix, qty=10, rate=100):
	"""Single-Tender/single-Sales-Order case, via the current Won pipeline
	(Project Tender -> handle_won_automation() -> Sales Order) - Tender no
	longer has its own status="Won"/sales_order fields, so this can't go
	through Tender directly the way it used to."""
	project, tenders = make_won_project("cpi_" + suffix, [("Civil", rate)], qty=qty)
	so_name = frappe.db.get_value(
		"Project Tender Direct Cost Detail", {"tender": tenders[0].name}, "sales_order"
	)
	return frappe.get_doc("Sales Order", so_name)


def won_project_with_sales_orders(suffix, category_rates, qty=10):
	"""One Project carrying one Sales Order per (tender_category, rate) pair
	in `category_rates` - the project-mode equivalent of won_sales_order."""
	project, tenders = make_won_project("cpi_" + suffix, category_rates, qty=qty)
	sales_orders = [
		frappe.get_doc("Sales Order", frappe.db.get_value(
			"Project Tender Direct Cost Detail", {"tender": t.name}, "sales_order"
		))
		for t in tenders
	]
	return project, sales_orders


def make_cpi(sales_order, cumulative, retention_percent=5):
	rows = get_sales_order_items(sales_order.name)
	rows[0]["cumulative_qty_complete"] = cumulative
	cpi = frappe.get_doc({
		"doctype": "Client Progress Invoice",
		"naming_series": "CPI-.YYYY.-",
		"sales_order": sales_order.name,
		"retention_percent": retention_percent,
	})
	cpi.append("items", rows[0])
	return cpi


def make_project_cpi(project, invoice_category, sales_orders, cumulative_by_so, retention_percent=5):
	"""Project-mode equivalent of make_cpi - one item row pulled per Sales
	Order, mirroring what the "Get Items from Sales Order(s)" button does."""
	cpi = frappe.get_doc({
		"doctype": "Client Progress Invoice",
		"naming_series": "CPI-.YYYY.-",
		"project": project.name,
		"invoice_category": invoice_category,
		"retention_percent": retention_percent,
	})
	for so in sales_orders:
		rows = get_sales_order_items(so.name)
		rows[0]["cumulative_qty_complete"] = cumulative_by_so[so.name]
		cpi.append("items", rows[0])
	return cpi


class TestClientProgressInvoice(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		create_work_item_group()
		ensure_customer()
		if not frappe.db.exists("Item", "_Test Tender Labor"):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": "_Test Tender Labor",
				"item_group": "Labor",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)

	def test_get_items_populates_from_sales_order(self):
		so = won_sales_order("Get")

		rows = get_sales_order_items(so.name)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["sales_order_item"], so.items[0].name)
		self.assertEqual(rows[0]["qty_allocated"], so.items[0].qty)
		self.assertEqual(rows[0]["rate"], so.items[0].rate)
		self.assertEqual(rows[0]["cumulative_qty_complete"], 0)

	def test_percent_complete_and_retention(self):
		so = won_sales_order("Calc", qty=10)
		rate = so.items[0].rate

		cpi = make_cpi(so, cumulative=4)
		cpi.insert(ignore_permissions=True)

		row = cpi.items[0]
		self.assertEqual(row.qty_allocated, 10)
		self.assertEqual(row.percent_complete, 40)
		self.assertEqual(row.this_period_qty, 4)
		self.assertEqual(row.this_period_amount, 4 * rate)
		self.assertEqual(row.retention_amount, 4 * rate * 0.05)
		self.assertEqual(cpi.net_receivable, cpi.total_this_period - cpi.total_retention_held)

	def test_sales_invoice_carries_tender_description_and_so_link(self):
		so = won_sales_order("Invoice")

		cpi = make_cpi(so, cumulative=4)
		cpi.insert(ignore_permissions=True)
		cpi.status = "Measured"
		cpi.save(ignore_permissions=True)
		cpi.submit()

		si = frappe.get_doc("Sales Invoice", create_sales_invoice_from_backlog(cpi.name))
		line = si.items[0]

		self.assertEqual(line.item_code, so.items[0].item_code)
		self.assertEqual(line.qty, 4)
		# The tender's own scope wording, end to end - won_sales_order's BOQ
		# row doesn't set its own description, so this is the template's,
		# fetched onto the BOQ row and carried through to the Sales Invoice.
		self.assertEqual(line.description, so.items[0].description)
		self.assertEqual(line.description, "template reference text")
		# These two are what let the Sales Order's per_billed advance.
		self.assertEqual(line.sales_order, so.name)
		self.assertEqual(line.so_detail, so.items[0].name)
		self.assertTrue(line.income_account)

	def test_submitting_invoice_advances_per_billed(self):
		so = won_sales_order("Billed", qty=10)

		cpi = make_cpi(so, cumulative=4)
		cpi.insert(ignore_permissions=True)
		cpi.status = "Measured"
		cpi.save(ignore_permissions=True)
		cpi.submit()
		frappe.get_doc("Sales Invoice", create_sales_invoice_from_backlog(cpi.name)).submit()

		so.reload()
		self.assertAlmostEqual(so.per_billed, 40, places=2)

	def test_second_period_nets_off_prior_claim(self):
		so = won_sales_order("Period2", qty=10)

		first = make_cpi(so, cumulative=4)
		first.insert(ignore_permissions=True)
		first.status = "Measured"
		first.save(ignore_permissions=True)
		first.submit()

		# get_sales_order_items seeds from the prior submitted claim, since
		# the claim is cumulative and cannot regress.
		rows = get_sales_order_items(so.name)
		self.assertEqual(rows[0]["cumulative_qty_complete"], 4)

		second = make_cpi(so, cumulative=7)
		second.insert(ignore_permissions=True)
		self.assertEqual(second.items[0].this_period_qty, 3)
		self.assertEqual(second.items[0].previously_invoiced_amount, first.total_this_period)

	def test_progress_cannot_regress(self):
		so = won_sales_order("Regress", qty=10)
		first = make_cpi(so, cumulative=4)
		first.insert(ignore_permissions=True)
		first.status = "Measured"
		first.save(ignore_permissions=True)
		first.submit()

		with self.assertRaises(frappe.ValidationError):
			make_cpi(so, cumulative=2).insert(ignore_permissions=True)

	def test_cannot_exceed_allocated_quantity(self):
		so = won_sales_order("Over", qty=10)

		with self.assertRaises(frappe.ValidationError):
			make_cpi(so, cumulative=11).insert(ignore_permissions=True)

	def test_invoiced_tracking_refreshes_without_resaving_cpi(self):
		so = won_sales_order("InvTrack", qty=10)

		cpi = make_cpi(so, cumulative=4)
		cpi.insert(ignore_permissions=True)
		cpi.status = "Measured"
		cpi.save(ignore_permissions=True)
		cpi.submit()

		si = frappe.get_doc("Sales Invoice", create_sales_invoice_from_backlog(cpi.name))
		si.submit()

		# FR-08/09: refreshed via the Sales Invoice on_submit hook - reload
		# only, the CPI itself is never resaved.
		cpi.reload()
		row = cpi.items[0]
		self.assertEqual(row.invoiced_qty, 4)
		self.assertAlmostEqual(row.invoiced_percent, 40, places=2)
		self.assertEqual(row.outstanding_qty, 0)

		si.cancel()
		cpi.reload()
		row = cpi.items[0]
		self.assertEqual(row.invoiced_qty, 0)
		self.assertEqual(row.outstanding_qty, 4)

	def test_create_sales_invoice_from_backlog_sweeps_prior_cpi(self):
		so = won_sales_order("Backlog", qty=10)

		cpi_a = make_cpi(so, cumulative=4)
		cpi_a.insert(ignore_permissions=True)
		cpi_a.status = "Measured"
		cpi_a.save(ignore_permissions=True)
		cpi_a.submit()  # never billed

		cpi_b = make_cpi(so, cumulative=7)
		cpi_b.insert(ignore_permissions=True)
		cpi_b.status = "Measured"
		cpi_b.save(ignore_permissions=True)
		cpi_b.submit()

		# TC-05: billing from CPI-B's own Create button covers the full 70%
		# worth (CPI-A's 40% plus CPI-B's own delta), not just its own share.
		si = frappe.get_doc("Sales Invoice", create_sales_invoice_from_backlog(cpi_b.name))
		self.assertEqual(si.items[0].qty, 7)

		si.submit()
		self.assertIsNone(create_sales_invoice_from_backlog(cpi_b.name))

	def test_source_must_be_unambiguous(self):
		so = won_sales_order("Mixed")

		# A row carrying both links would otherwise be computed against
		# whichever source the parent happened to name.
		cpi = make_cpi(so, cumulative=1)
		cpi.items[0].tender_item = frappe.db.get_value("Tender Item", {}, "name")
		if cpi.items[0].tender_item:
			with self.assertRaises(frappe.ValidationError):
				cpi.insert(ignore_permissions=True)

		# And a parent with no source at all is not billable.
		orphan = make_cpi(so, cumulative=1)
		orphan.sales_order = None
		with self.assertRaises(frappe.ValidationError):
			orphan.insert(ignore_permissions=True)

	# ---------- Project mode ----------

	def test_get_project_sales_orders_matches_project_and_category(self):
		project, sales_orders = won_project_with_sales_orders(
			"ProjMatch", [("Electrical", 100), ("Electrical", 150)]
		)

		matches = get_project_sales_orders(project.name, "Electrical")

		self.assertEqual({r["sales_order"] for r in matches}, {so.name for so in sales_orders})

	def test_get_project_sales_orders_no_match(self):
		project, _sales_orders = won_project_with_sales_orders("ProjNoMatch", [("Civil", 100)])

		matches = get_project_sales_orders(project.name, "Electrical")

		self.assertEqual(matches, [])

	def test_project_mode_claims_across_multiple_sales_orders(self):
		project, sales_orders = won_project_with_sales_orders(
			"ProjClaim", [("Electrical", 100), ("Electrical", 150)]
		)

		cpi = make_project_cpi(
			project, "Electrical", sales_orders,
			{sales_orders[0].name: 4, sales_orders[1].name: 6},
		)
		cpi.insert(ignore_permissions=True)

		# progress_sales_orders is server-recomputed on save (system-managed) -
		# confirms it ends up populated even though nothing set it by hand.
		self.assertEqual(
			{row.sales_order for row in cpi.progress_sales_orders},
			{so.name for so in sales_orders},
		)
		self.assertEqual({row.sales_order for row in cpi.items}, {so.name for so in sales_orders})
		self.assertGreater(cpi.total_this_period, 0)

	def test_project_and_sales_order_both_set_throws(self):
		project, sales_orders = won_project_with_sales_orders("ProjBoth", [("Electrical", 100)])

		cpi = make_project_cpi(project, "Electrical", sales_orders, {sales_orders[0].name: 1})
		cpi.sales_order = sales_orders[0].name

		with self.assertRaises(frappe.ValidationError):
			cpi.insert(ignore_permissions=True)

	def test_item_row_outside_progress_sales_orders_throws(self):
		project, sales_orders = won_project_with_sales_orders(
			"ProjOrphan", [("Electrical", 100), ("Civil", 100)]
		)
		electrical_so, civil_so = sales_orders

		# A Civil Sales Order's line doesn't belong to an Electrical-only
		# claim. progress_sales_orders is recomputed from the live
		# project+category match on save (system-managed, not hand-edited),
		# so this is caught even though nothing was manually removed to
		# cause it - the equivalent of the BRD's "SO removed from picker"
		# edge case under the resolved system-managed design.
		cpi = make_project_cpi(project, "Electrical", [electrical_so], {electrical_so.name: 1})
		stray_row = get_sales_order_items(civil_so.name)[0]
		stray_row["cumulative_qty_complete"] = 1
		cpi.append("items", stray_row)

		with self.assertRaises(frappe.ValidationError):
			cpi.insert(ignore_permissions=True)

	def test_project_mode_invoice_updates_project_rollup(self):
		from contracting.contracting.utils.project_cost_billing import recompute_and_save

		project, sales_orders = won_project_with_sales_orders("ProjRollup", [("Electrical", 100)])

		cpi = make_project_cpi(project, "Electrical", sales_orders, {sales_orders[0].name: 4})
		cpi.insert(ignore_permissions=True)
		cpi.status = "Measured"
		cpi.save(ignore_permissions=True)
		cpi.submit()
		cpi.reload()
		self.assertEqual(cpi.docstatus, 1)
		self.assertEqual(cpi.status, "Approved")

		recompute_and_save(project.name)

		project.reload()
		self.assertAlmostEqual(
			project.total_progress_client_invoiced_amount, cpi.total_this_period, places=2
		)

		# FR-26/TC-13: on_update already fires through cancel() too (Frappe's
		# submit()/cancel() both route through save()) - this is the first
		# time it matters for *this* doctype's on_update, since it only
		# started running through a real submit/cancel transition once CPI
		# became submittable.
		cpi.cancel()
		recompute_and_save(project.name)
		project.reload()
		self.assertEqual(project.total_progress_client_invoiced_amount, 0)
