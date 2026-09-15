# Copyright (c) 2026, kazem and Contributors
# See license.txt

import erpnext
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.doctype.contractor_invoice.contractor_invoice import (
	create_purchase_invoice,
)
from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	make_draft_contract,
	make_resource_tender_and_project,
	submit_contract,
)


def _account_like(name_fragment):
	company = erpnext.get_default_company()
	return frappe.db.get_value(
		"Account", {"company": company, "account_name": ["like", f"%{name_fragment}%"]}, "name"
	)


def make_charged_contract(
	suffix, item_qty=10, unit_price=100,
	retention_rate=5, retention_sign="Deduct", vat_rate=14,
	include_retention=True,
):
	"""A submitted Supply-and-Install contract with one contracted_items row
	plus, unless include_retention is False, a Retention row (On Net Total)
	and a VAT/Tax row (On Net Total, Add) in additional_costs - the shape
	needed to test the retention-folded-into-additional_costs mirror
	(FR-12-19) without going through the Payment Conditions branch."""
	project, tender = make_resource_tender_and_project(suffix, qty=item_qty)
	boq_row = tender.boq_items[0]

	contract = make_draft_contract(project, "Supplying and installing")
	contract.append("contracted_items", {"work_item": boq_row.name, "qty": item_qty, "unit_price": unit_price})
	if include_retention:
		contract.append("additional_costs", {
			"cost_category": "Retention",
			"charge_type": "On Net Total",
			"category": "Total",
			"rate": retention_rate,
			"add_deduct_tax": retention_sign,
			"account_head": _account_like("Retention Primary"),
			"description": "Retention",
		})
	contract.append("additional_costs", {
		"cost_category": "Tax",
		"charge_type": "On Net Total",
		"category": "Total",
		"rate": vat_rate,
		"add_deduct_tax": "Add",
		"account_head": _account_like("VAT"),
		"description": "VAT",
	})
	contract.insert(ignore_permissions=True)
	submit_contract(contract)
	return contract


def make_plain_invoice(contract, cumulative_qty_complete):
	contract_item = contract.contracted_items[0]
	invoice = frappe.new_doc("Contractor Invoice")
	invoice.naming_series = "CINV-.YYYY.-"
	invoice.project = contract.project
	invoice.contractor_contract = contract.name
	invoice.append("items", {"contract_item": contract_item.name, "cumulative_qty_complete": cumulative_qty_complete})
	return invoice


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
	invoice.project = contract.project
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
		"""2026-09-13 payment-condition-payout fix: every condition on a line
		shares the same, full, unscaled ceiling (the line's own qty) -
		independently of every other condition - and the condition's percent
		scales the *amount*, not the ceiling. The items rollup is a
		value-weighted sum (percent share of each condition's own qty), not
		a naive one."""
		contract = make_condition_contract("cond_ceiling", item_qty=10, unit_price=100, condition_percent=30)

		# Ceiling is the line's own full qty (10), not 30% of it - claim 1.5.
		invoice = make_condition_invoice(contract, cumulative_qty_complete=1.5)
		invoice.insert(ignore_permissions=True)

		row = invoice.condition_progress[0]
		self.assertAlmostEqual(row.qty_allocated, 10.0, delta=1e-6)
		self.assertAlmostEqual(row.this_period_qty, 1.5, delta=1e-6)
		# Amount = qty * rate * condition percent / 100, not qty * rate alone.
		self.assertAlmostEqual(row.this_period_amount, 1.5 * 100 * 0.30, delta=1e-6)
		self.assertEqual(row.condition_label, "_Test Condition")

		item_row = invoice.items[0]
		# Weighted rollup: this condition's own qty times its percent share.
		self.assertAlmostEqual(item_row.cumulative_qty_complete, 1.5 * 0.30, delta=1e-6)
		self.assertAlmostEqual(item_row.percent_complete, 1.5 * 0.30 / 10 * 100, delta=1e-6)

	def test_condition_progress_ceiling_guard_rejects_overclaim(self):
		"""A condition's cumulative qty can't exceed the contract line's own
		full qty - the only qty-vs-contract validation, evaluated per
		condition independently of every other condition on the same line."""
		contract = make_condition_contract("cond_over", item_qty=10, unit_price=100, condition_percent=30)

		invoice = make_condition_invoice(contract, cumulative_qty_complete=11)  # ceiling is 10
		with self.assertRaises(frappe.ValidationError):
			invoice.insert(ignore_permissions=True)

	def test_condition_progress_no_regression_guard(self):
		"""FR-27: a later invoice against the same condition can't claim
		less cumulative progress than an earlier Invoiced one already did."""
		contract = make_condition_contract("cond_regress", item_qty=10, unit_price=100, condition_percent=30)

		first = make_condition_invoice(contract, cumulative_qty_complete=2)
		first.insert(ignore_permissions=True)
		first.submit()

		second = make_condition_invoice(contract, cumulative_qty_complete=1)  # regresses from 2 to 1
		with self.assertRaises(frappe.ValidationError):
			second.insert(ignore_permissions=True)

	def test_retention_mirrored_into_additional_costs(self):
		"""TC-09/FR-12: the contract's Retention row is no longer excluded
		from the additional_costs mirror - it's prorated exactly like Tax."""
		contract = make_charged_contract("retention_mirror", item_qty=10, unit_price=100,
			retention_rate=5, vat_rate=14)

		invoice = make_plain_invoice(contract, cumulative_qty_complete=5)  # claims 500
		invoice.insert(ignore_permissions=True)

		retention_rows = [r for r in invoice.additional_costs if r.cost_category == "Retention"]
		self.assertEqual(len(retention_rows), 1)
		self.assertAlmostEqual(retention_rows[0].tax_amount, 25.0, delta=1e-6)  # 500 * 5%
		self.assertEqual(retention_rows[0].add_deduct_tax, "Deduct")
		self.assertAlmostEqual(invoice.total_retention_held, retention_rows[0].tax_amount, delta=1e-6)

	def test_no_retention_percent_field(self):
		"""TC-10/FR-13: retention_percent no longer exists on the doctype."""
		self.assertIsNone(frappe.get_meta("Contractor Invoice").get_field("retention_percent"))

	def test_net_payable_two_term_formula(self):
		"""TC-11/FR-15: net_payable = total_this_period + total_additional_charges,
		with total_additional_charges already carrying Retention's signed
		contribution - no separate subtraction."""
		contract = make_charged_contract("net_payable", item_qty=10, unit_price=100,
			retention_rate=5, vat_rate=14)

		invoice = make_plain_invoice(contract, cumulative_qty_complete=10)  # claims 1000
		invoice.insert(ignore_permissions=True)

		self.assertAlmostEqual(invoice.total_this_period, 1000.0, delta=1e-6)
		self.assertAlmostEqual(invoice.total_additional_charges, 140.0 - 50.0, delta=1e-6)
		self.assertAlmostEqual(invoice.net_payable, 1090.0, delta=1e-6)

	def test_create_purchase_invoice_reflects_retention(self):
		"""TC-12/FR-16: create_purchase_invoice() needs no special-casing -
		once Retention isn't excluded from additional_costs, its existing
		loop over that table picks it up automatically."""
		contract = make_charged_contract("pi_retention", item_qty=10, unit_price=100,
			retention_rate=5, vat_rate=14)

		invoice = make_plain_invoice(contract, cumulative_qty_complete=10)
		invoice.insert(ignore_permissions=True)

		pi = create_purchase_invoice(invoice)

		self.assertTrue(any(r.add_deduct_tax == "Deduct" for r in pi.taxes))
		self.assertAlmostEqual(pi.grand_total, 1000.0 + 140.0 - 50.0, delta=1e-6)

	def test_per_line_retention_columns_populate(self):
		"""TC-13/FR-19: retention_amount/net_amount on the Items row are
		informational, sourced from the contract's Retention rate directly -
		independent of the header total_retention_held rollup."""
		contract = make_charged_contract("per_line_retention", item_qty=10, unit_price=100,
			retention_rate=5, vat_rate=14)

		invoice = make_plain_invoice(contract, cumulative_qty_complete=4)  # claims 400
		invoice.insert(ignore_permissions=True)

		row = invoice.items[0]
		self.assertAlmostEqual(row.retention_amount, 20.0, delta=1e-6)  # 400 * 5%
		self.assertAlmostEqual(row.net_amount, 380.0, delta=1e-6)

	def test_retention_row_wrong_sign_blocked(self):
		"""TC-14/FR-17: a Retention row must be Deduct, not Add - validate()
		throws naming the contract and row, before this reaches any invoice."""
		with self.assertRaises(frappe.ValidationError):
			make_charged_contract("wrong_sign", retention_sign="Add")

	def test_no_retention_row_edge_case(self):
		"""TC-16: a contract with no Retention row at all is legitimate -
		the mirror simply has no Retention row and nothing is withheld."""
		contract = make_charged_contract("no_retention", item_qty=10, unit_price=100,
			vat_rate=14, include_retention=False)

		invoice = make_plain_invoice(contract, cumulative_qty_complete=5)
		invoice.insert(ignore_permissions=True)

		self.assertFalse([r for r in invoice.additional_costs if r.cost_category == "Retention"])
		self.assertAlmostEqual(invoice.total_retention_held, 0.0, delta=1e-6)
		self.assertAlmostEqual(invoice.items[0].retention_amount, 0.0, delta=1e-6)

	def test_charge_referencing_retention_row(self):
		"""TC-17: a charge referencing the (now-included) Retention row's
		row_id resolves against its own prorated tax_amount, not a dangling
		reference - Retention is no longer specially excluded from the copy."""
		project, tender = make_resource_tender_and_project("charge_ref_retention", qty=10)
		boq_row = tender.boq_items[0]

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 10, "unit_price": 100})
		contract.append("additional_costs", {
			"cost_category": "Retention", "charge_type": "On Net Total", "category": "Total",
			"rate": 5, "add_deduct_tax": "Deduct", "account_head": _account_like("Retention Primary"),
			"description": "Retention",
		})
		contract.append("additional_costs", {
			"cost_category": "Charge", "charge_type": "On Previous Row Amount", "category": "Total",
			"rate": 10, "add_deduct_tax": "Add", "row_id": "1",
			"account_head": _account_like("VAT"), "description": "Charge on Retention",
		})
		contract.insert(ignore_permissions=True)
		submit_contract(contract)

		invoice = make_plain_invoice(contract, cumulative_qty_complete=10)  # claims 1000
		invoice.insert(ignore_permissions=True)

		retention_row = next(r for r in invoice.additional_costs if r.cost_category == "Retention")
		referencing_row = next(r for r in invoice.additional_costs if r.cost_category == "Charge")
		self.assertAlmostEqual(retention_row.tax_amount, 50.0, delta=1e-6)  # 1000 * 5%
		self.assertAlmostEqual(referencing_row.tax_amount, 5.0, delta=1e-6)  # 50 * 10%
