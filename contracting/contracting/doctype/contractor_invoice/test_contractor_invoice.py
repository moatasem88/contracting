# Copyright (c) 2026, kazem and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	make_draft_contract,
	make_resource_tender_and_project,
	submit_contract,
)


def make_condition_contract(suffix, item_qty=10, unit_price=100, condition_percent=30):
	"""A submitted Supply-and-Install contract carrying Payment Conditions
	(FR-21-24) - the first row is the one under test; a second row makes
	up the remainder so the default group still sums to 100% (FR-22)."""
	project, tender = make_resource_tender_and_project(suffix, qty=item_qty)
	boq_row = tender.boq_items[0]

	contract = make_draft_contract(project, "Supplying and installing")
	contract.append("contracted_items", {"work_item": boq_row.name, "qty": item_qty, "unit_price": unit_price})
	contract.append("payment_conditions", {"condition": "_Test Condition", "percent": condition_percent})
	contract.append("payment_conditions", {"condition": "_Test Remainder", "percent": 100 - condition_percent})
	contract.insert(ignore_permissions=True)
	submit_contract(contract)
	return contract


def make_condition_invoice(contract, cumulative_qty_complete):
	contract_item = contract.contracted_items[0]
	condition = contract.payment_conditions[0]

	invoice = frappe.new_doc("Contractor Invoice")
	invoice.naming_series = "CINV-.YYYY.-"
	invoice.contractor_contract = contract.name
	invoice.append("items", {"contract_item": contract_item.name})
	invoice.append("condition_progress", {
		"contract_item": contract_item.name,
		"payment_condition": condition.name,
		"cumulative_qty_complete": cumulative_qty_complete,
	})
	return invoice


class TestContractorInvoice(FrappeTestCase):
	def test_condition_progress_ceiling_and_rollup(self):
		"""FR-25-29: a condition's ceiling is its percent share of the
		line's allocated qty, and Contractor Invoice Item becomes a rollup
		of the matching condition_progress rows."""
		contract = make_condition_contract("cond_ceiling", item_qty=10, unit_price=100, condition_percent=30)

		# 30% of 10 = 3 units is this condition's ceiling; claim half of it.
		invoice = make_condition_invoice(contract, cumulative_qty_complete=1.5)
		invoice.insert(ignore_permissions=True)

		row = invoice.condition_progress[0]
		self.assertAlmostEqual(row.qty_allocated, 3.0, delta=1e-6)
		self.assertAlmostEqual(row.this_period_qty, 1.5, delta=1e-6)
		self.assertAlmostEqual(row.this_period_amount, 1.5 * 100, delta=1e-6)
		self.assertEqual(row.condition_label, "_Test Condition")

		item_row = invoice.items[0]
		self.assertAlmostEqual(item_row.cumulative_qty_complete, 1.5, delta=1e-6)
		# Against the *line's* full qty (10), not this one condition's
		# ceiling (3) - only this one condition has any progress so far.
		self.assertAlmostEqual(item_row.percent_complete, 15.0, delta=1e-6)

	def test_condition_progress_ceiling_guard_rejects_overclaim(self):
		"""FR-27: cumulative qty can't exceed this condition's own share of
		the line, even though the line's own full qty is much larger."""
		contract = make_condition_contract("cond_over", item_qty=10, unit_price=100, condition_percent=30)

		invoice = make_condition_invoice(contract, cumulative_qty_complete=5)  # ceiling is 3
		with self.assertRaises(frappe.ValidationError):
			invoice.insert(ignore_permissions=True)

	def test_condition_progress_no_regression_guard(self):
		"""FR-27: a later invoice against the same condition can't claim
		less cumulative progress than an earlier Invoiced one already did."""
		contract = make_condition_contract("cond_regress", item_qty=10, unit_price=100, condition_percent=30)

		first = make_condition_invoice(contract, cumulative_qty_complete=2)
		first.insert(ignore_permissions=True)
		first.status = "Invoiced"
		first.save(ignore_permissions=True)

		second = make_condition_invoice(contract, cumulative_qty_complete=1)  # regresses from 2 to 1
		with self.assertRaises(frappe.ValidationError):
			second.insert(ignore_permissions=True)
