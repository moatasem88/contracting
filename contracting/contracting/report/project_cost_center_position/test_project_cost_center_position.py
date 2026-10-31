# Copyright (c) 2026, kazem and Contributors
# See license.txt

"""Project Cost Center Position report (BRD 2026-10-30 rev 2, §12).

Each test builds its own Cost Centers/Projects (see World in
utils/test_project_position.py) and runs the report filtered to them, so
the site's own data never reaches the assertions. Mandatory Project is
switched off here: these tests need documents with no Project on purpose.

Cheques (TC-11/12/13/22): the two Payment Entries are real submitted
documents, but the Cheque and its Cheque Payment Entry rows are written
with db_insert - the report only reads those rows, and the full
cheques_management clearance flow needs its workflow and roles. The
Expense Claim settling an advance (TC-51) is written the same way: a real
one needs an Employee with this site's mandatory custom fields.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, today

from contracting.contracting.utils.project_position import NO_COST_CENTER, NO_PROJECT, UNASSIGNED
from contracting.contracting.utils.test_project_position import (
	COMPANY,
	CUSTOMER,
	SUPPLIER,
	World,
	account,
	bank_gl,
	make_internal_transfer,
	make_invoice,
	make_payment,
	set_enforced,
)
from contracting.contracting.report.project_cost_center_position import project_cost_center_position as report

# Documents post today by default, so To Date follows the clock.
TO_DATE = today()
GUARANTEE_ACCOUNT_PREFIX = "125040001%"
NOTES_PAYABLE_PREFIX = "212020001%"


def run_report(world, **filters):
	"""{(cost_center, project): row} plus the raw rows."""
	filters = {"company": COMPANY, "to_date": TO_DATE, "cost_center": world.cost_centers, **filters}
	_columns, data, _message = report.execute(filters)
	leaves = {(r["cost_center"], r["project"]): r for r in data if r["indent"] == 1}
	return leaves, data, filters


def set_guarantee_accounts(accounts):
	settings = frappe.get_doc("Project Position Settings")
	settings.company = COMPANY
	settings.set("guarantee_accounts", [{"account": a} for a in accounts])
	settings.save(ignore_permissions=True)


def make_journal_entry(rows, posting_date=TO_DATE):
	"""rows: [(account, debit, credit, cost_center)]."""
	je = frappe.get_doc({
		"doctype": "Journal Entry",
		"voucher_type": "Journal Entry",
		"company": COMPANY,
		"posting_date": posting_date,
		"cheque_no": frappe.generate_hash(length=8),
		"cheque_date": posting_date,
		"accounts": [
			{"account": acc, "debit_in_account_currency": dr, "credit_in_account_currency": cr, "cost_center": cc}
			for acc, dr, cr, cc in rows
		],
	})
	je.insert(ignore_permissions=True)
	je.submit()
	return je


def make_cheque(payment_entry, status, cost_center, project, amount, clearing_payment_entry=None):
	cheque = frappe.get_doc({
		"doctype": "Cheque",
		"name": "_PP-CHQ-" + frappe.generate_hash(length=8),
		"company": COMPANY,
		"payment_type": payment_entry.payment_type,
		"party_type": payment_entry.party_type,
		"party": payment_entry.party,
		"amount": amount,
		"status": status,
		"cost_center": cost_center,
		"project": project,
		"docstatus": 1,
		"payment_entry": payment_entry.name,
		"clearing_payment_entry": clearing_payment_entry,
	})
	cheque.db_insert()
	row = frappe.get_doc({
		"doctype": "Cheque Payment Entry",
		"name": frappe.generate_hash(length=10),
		"parent": cheque.name,
		"parenttype": "Cheque",
		"parentfield": "payment_entries",
		"idx": 1,
		"docstatus": 1,
		"payment_entry": payment_entry.name,
		"posting_date": payment_entry.posting_date,
		"cost_center": cost_center,
		"project": project,
		"allocated_amount": amount,
		"clearing_payment_entry": clearing_payment_entry,
	})
	row.db_insert()
	return cheque


class TestProjectCostCenterPosition(FrappeTestCase):
	def setUp(self):
		set_enforced(0)
		set_guarantee_accounts([])
		self.w = World.make()

	def tearDown(self):
		set_enforced(1)

	def assertAmounts(self, row, **expected):
		for field, value in expected.items():
			self.assertAlmostEqual(flt(row[field]), value, delta=0.01, msg=field)

	def make_basic(self):
		"""TC-01 to TC-05 data."""
		w = self.w
		make_invoice("Sales Invoice", w.cc_a, [(100000, None, None)], project=w.p1)
		make_invoice("Purchase Invoice", w.cc_a, [(60000, None, None)], project=w.p1)
		# Receipts count only from the client of a Sales Order on the same
		# Cost Center/Project (TC-52), so P1 gets its order.
		self.make_sales_order(1000, w.cc_a, w.p1, tax=False)
		make_payment("Receive", 50000, w.cc_a, project=w.p1)
		make_payment("Pay", 30000, w.cc_a, project=w.p1)
		make_invoice("Purchase Invoice", w.cc_a, [(30000, None, None), (20000, None, w.p2)], project=w.p1)
		make_invoice("Purchase Invoice", w.cc_a, [(10000, None, None)])
		make_invoice("Sales Invoice", w.cc_b, [(30000, None, None)])
		make_payment("Pay", 5000, w.cc_c)

	def test_tc01_to_tc05_keys_and_amounts(self):
		w = self.w
		self.make_basic()
		leaves, _data, _f = run_report(w)

		# TC-01 (+ TC-02's 30,000 line that inherits the header's P1)
		self.assertAmounts(
			leaves[(w.cc_a, w.p1)],
			si_net=100000, si_gross=114000, pi_net=90000, pi_gross=102600,
			received=50000, paid=30000, cash_balance=20000,
			# The PI lines post to an expense account, so Total Expenses
			# equals PI Net here; Accrual Net reads Total Expenses.
			exp_purchase_invoice=90000, total_expenses=90000,
			accrual_net=10000, accrual_gross=11400,
		)
		# TC-02: a line-level Project overrides the header's
		self.assertAmounts(leaves[(w.cc_a, w.p2)], pi_net=20000, pi_gross=22800)
		# TC-03: shared Cost Center, no Project anywhere
		self.assertAmounts(leaves[(w.cc_a, UNASSIGNED)], pi_net=10000)
		# TC-04: unique Cost Center -> its Project
		self.assertAmounts(leaves[(w.cc_b, w.p3)], si_net=30000)
		# TC-05: overhead Cost Center
		self.assertAmounts(leaves[(w.cc_c, NO_PROJECT)], paid=5000, cash_balance=-5000)

	def test_tc06_no_cost_center(self):
		w = self.w
		pi = make_invoice("Purchase Invoice", w.cc_c, [(1234, None, None)])
		# Cost Center is mandatory today; the 194 historic PIs without one
		# predate that, so the state is reproduced directly.
		frappe.db.set_value("Purchase Invoice", pi.name, "cost_center", None)
		frappe.db.sql("UPDATE `tabPurchase Invoice Item` SET cost_center = NULL WHERE parent = %s", pi.name)

		_columns, data, _message = report.execute({"company": COMPANY, "to_date": TO_DATE})
		top_level = [r["label"] for r in data if r["indent"] == 0]
		self.assertEqual(top_level[-2:], [NO_COST_CENTER, "Total"])
		lines = report.get_drilldown(
			"pi_net", json.dumps({"company": COMPANY, "to_date": TO_DATE}), NO_COST_CENTER, NO_PROJECT
		)["rows"]
		self.assertIn(pi.name, [r["voucher_no"] for r in lines])

	def test_tc07_subtotals_reconcile(self):
		self.make_basic()
		_leaves, data, _f = run_report(self.w)
		cc_rows = [r for r in data if r["indent"] == 0 and not r.get("is_total_row")]
		total = data[-1]
		self.assertTrue(total.get("is_total_row"))
		for field in report.AMOUNT_FIELDS:
			for cc_row in cc_rows:
				leaves = [r for r in data if r.get("parent_row") == cc_row["row_id"]]
				self.assertAlmostEqual(cc_row[field], sum(r[field] for r in leaves), delta=0.01)
			self.assertAlmostEqual(total[field], sum(r[field] for r in cc_rows), delta=0.01)

	def test_tc08_draft_and_cancelled_ignored(self):
		w = self.w
		make_invoice("Sales Invoice", w.cc_b, [(10000, None, None)], submit=False)
		make_payment("Pay", 5000, w.cc_b).cancel()
		leaves, _data, _f = run_report(w)
		self.assertNotIn((w.cc_b, w.p3), leaves)

	def test_tc09_credit_note_not_subtracted_twice(self):
		w = self.w
		make_invoice("Sales Invoice", w.cc_b, [(100000, None, None)], project=w.p3)
		make_invoice("Sales Invoice", w.cc_b, [(20000, None, None)], project=w.p3, is_return=True)
		leaves, _data, _f = run_report(w)
		self.assertAmounts(leaves[(w.cc_b, w.p3)], si_net=80000, si_gross=91200)

	def test_tc10_internal_transfer_excluded(self):
		make_internal_transfer(7000, self.w.cc_a)
		leaves, _data, _f = run_report(self.w)
		for row in leaves.values():
			self.assertAmounts(row, paid=0, received=0)

	def make_cleared_cheque(self):
		"""TC-11 data: a 30,000 cheque payment on 10-10 to the Notes
		account, cleared by a Bank Clearance PE on 10-20."""
		w = self.w
		notes = account({"name": ["like", NOTES_PAYABLE_PREFIX]})
		payable = frappe.db.get_value("Company", COMPANY, "default_payable_account")
		original = make_payment("Pay", 30000, w.cc_a, project=w.p1, posting_date="2026-10-10",
			paid_from=notes, paid_to=payable)
		clearing = make_payment("Pay", 30000, w.cc_a, project=w.p1, posting_date="2026-10-20",
			mode_of_payment="Bank Clearance", paid_from=bank_gl(), paid_to=notes)
		make_cheque(original, "Collected", w.cc_a, w.p1, 30000, clearing.name)
		return original, clearing

	def test_tc11_tc12_tc22_cheque(self):
		w = self.w
		self.make_cleared_cheque()

		leaves, _data, _f = run_report(w)
		row = leaves[(w.cc_a, w.p1)]
		# TC-11: the clearing entry is not a second payment
		self.assertAmounts(row, paid=30000, uncleared_paid=0)
		# TC-22: cheque PE to Notes + clearing Notes -> Bank is neutral
		self.assertAmounts(row, gl_cash_difference=0)

		# TC-12: as at 10-15 the cheque is still uncleared
		leaves, _data, _f = run_report(w, to_date="2026-10-15")
		self.assertAmounts(leaves[(w.cc_a, w.p1)], paid=30000, uncleared_paid=30000, gl_cash_difference=0)

	def test_tc13_cancelled_vs_rejected_cheque(self):
		w = self.w
		cancelled_pe = make_payment("Pay", 1000, w.cc_b, project=w.p3)
		rejected_pe = make_payment("Pay", 2000, w.cc_b, project=w.p3)
		make_cheque(cancelled_pe, "Cancelled", w.cc_b, w.p3, 1000)
		make_cheque(rejected_pe, "Rejected", w.cc_b, w.p3, 2000)
		leaves, _data, _f = run_report(w)
		self.assertAmounts(leaves[(w.cc_b, w.p3)], uncleared_paid=2000)

	def make_sales_order(self, net, cost_center, project, currency="EGP", rate=1, tax=True):
		so = frappe.new_doc("Sales Order")
		so.update({
			"customer": CUSTOMER, "company": COMPANY, "transaction_date": TO_DATE, "delivery_date": TO_DATE,
			"order_type": "Sales", "currency": currency, "conversion_rate": rate,
			"cost_center": cost_center, "project": project, "ignore_pricing_rule": 1,
		})
		so.append("items", {"item_code": "_Test SC Subcontract", "qty": 1, "rate": net, "delivery_date": TO_DATE})
		if tax:
			so.append("taxes", {
				"charge_type": "On Net Total", "rate": 14, "description": "VAT", "cost_center": cost_center,
				"account_head": account({"account_type": "Tax", "name": ["like", "2350%"]}),
			})
		so.insert(ignore_permissions=True)
		so.submit()
		return so

	def test_tc14_sales_order_remaining(self):
		w = self.w
		so = self.make_sales_order(200000, w.cc_a, w.p1)
		# Billing an SO goes through a Sales Invoice; billed_amt is all the
		# report reads, so it is set directly.
		frappe.db.set_value("Sales Order Item", so.items[0].name, "billed_amt", 100000)
		closed = self.make_sales_order(5000, w.cc_a, w.p1)
		frappe.db.set_value("Sales Order", closed.name, "status", "Closed")

		leaves, _data, _f = run_report(w)
		self.assertAmounts(leaves[(w.cc_a, w.p1)], so_net=200000, so_gross=228000, so_remaining=100000)

	def test_tc15_sales_order_foreign_currency(self):
		w = self.w
		so = self.make_sales_order(1000, w.cc_b, w.p3, currency="USD", rate=50, tax=False)
		frappe.db.set_value("Sales Order Item", so.items[0].name, "billed_amt", 400)
		leaves, _data, _f = run_report(w)
		self.assertAmounts(leaves[(w.cc_b, w.p3)], so_net=50000, so_remaining=30000)

	def make_guarantee(self, amount, cost_center, posting_date):
		guarantee = account({"name": ["like", GUARANTEE_ACCOUNT_PREFIX]})
		return make_journal_entry([
			(guarantee, amount, 0, cost_center),
			(bank_gl(), 0, amount, cost_center),
		], posting_date=posting_date)

	def test_tc16_to_tc19_guarantees(self):
		w = self.w
		self.make_guarantee(40000, w.cc_a, "2026-08-01")
		self.make_guarantee(9999, w.cc_a, add_days(TO_DATE, 5))  # TC-18: after To Date
		self.make_sales_order(1000, w.cc_a, None, tax=False)
		make_payment("Receive", 1000, w.cc_a)

		# TC-19: nothing listed in the settings -> 0, no error
		leaves, _data, _f = run_report(w)
		self.assertAmounts(leaves[(w.cc_a, UNASSIGNED)], guarantees=0)

		set_guarantee_accounts([account({"name": ["like", GUARANTEE_ACCOUNT_PREFIX]})])
		leaves, _data, _f = run_report(w)
		# TC-16 (shared CC-A -> Unassigned), TC-18 excluded
		self.assertAmounts(leaves[(w.cc_a, UNASSIGNED)], guarantees=40000, cash_balance=1000,
			guarantee_adjusted_cash=-39000)
		# TC-17: the From Date does not apply to guarantees
		leaves, _data, _f = run_report(w, from_date="2026-10-01")
		self.assertAmounts(leaves[(w.cc_a, UNASSIGNED)], guarantees=40000)

	def test_tc20_tc21_gl_cash_difference(self):
		w = self.w
		self.make_sales_order(1000, w.cc_b, w.p3, tax=False)
		make_payment("Receive", 1000, w.cc_b)
		leaves, _data, filters = run_report(w)
		self.assertAmounts(leaves[(w.cc_b, w.p3)], cash_balance=1000, gl_cash_difference=0)  # TC-20

		je = make_journal_entry([
			(account({"root_type": "Expense"}), 8000, 0, w.cc_b),
			(bank_gl(), 0, 8000, w.cc_b),
		])
		leaves, _data, filters = run_report(w)
		self.assertAmounts(leaves[(w.cc_b, w.p3)], gl_cash_difference=8000)  # TC-21
		lines = report.get_drilldown("gl_cash_difference", json.dumps(filters), w.cc_b, w.p3)["rows"]
		self.assertEqual([r["voucher_no"] for r in lines], [je.name])

	def test_tc49_expenses_beyond_purchase_invoices(self):
		"""Total Expenses reads every expense posting, not only PIs: a
		Journal Entry expense (e.g. depreciation) counts and lowers Accrual."""
		w = self.w
		make_invoice("Sales Invoice", w.cc_b, [(50000, None, None)], project=w.p3)
		make_invoice("Purchase Invoice", w.cc_b, [(10000, None, None)], project=w.p3)
		make_journal_entry([(account({"root_type": "Expense"}), 4000, 0, w.cc_b), (bank_gl(), 0, 4000, w.cc_b)])
		leaves, _data, filters = run_report(w)
		self.assertAmounts(
			leaves[(w.cc_b, w.p3)],
			pi_net=10000, exp_purchase_invoice=10000, exp_journal_entry=4000,
			total_expenses=14000, accrual_net=36000,
		)
		lines = report.get_drilldown("total_expenses", json.dumps(filters), w.cc_b, w.p3)
		self.assertAlmostEqual(lines["total"], 14000, delta=0.01)
		self.assertEqual({r["voucher_type"] for r in lines["rows"]}, {"Purchase Invoice", "Journal Entry"})

	def test_tc50_stock_purchase_not_an_expense_until_issued(self):
		"""A PI line on a non-expense account is in PI Net but not in Total
		Expenses - it is expensed when the stock is issued."""
		w = self.w
		pi = make_invoice("Purchase Invoice", w.cc_b, [(7000, None, None)], project=w.p3, submit=False)
		pi.items[0].expense_account = account({"root_type": "Asset", "account_type": ["in", ["", None]]})
		pi.save(ignore_permissions=True)
		pi.submit()
		leaves, _data, _f = run_report(w)
		self.assertAmounts(leaves[(w.cc_b, w.p3)], pi_net=7000, total_expenses=0)

	def test_tc51_employee_advance_outstanding(self):
		"""Paid advance minus what Expense Claims settled as at To Date, keyed
		by the paying Payment Entry; not part of any balance."""
		from hrms.overrides.employee_payment_entry import get_payment_entry_for_employee

		w = self.w
		employee = frappe.db.get_value("Employee", {"status": "Active", "company": COMPANY}, "name")
		advance = frappe.get_doc({
			"doctype": "Employee Advance", "employee": employee, "company": COMPANY,
			"posting_date": "2026-10-01", "purpose": "PP test", "currency": "EGP", "exchange_rate": 1,
			"advance_amount": 6000,
			"advance_account": frappe.db.get_value("Company", COMPANY, "default_employee_advance_account"),
		}).insert(ignore_permissions=True)
		advance.submit()
		pe = get_payment_entry_for_employee("Employee Advance", advance.name)
		pe.update({
			"posting_date": "2026-10-02", "reference_date": "2026-10-02", "reference_no": w.tag,
			"paid_from": bank_gl(), "bank_account": frappe.db.get_value(
				"Bank Account", {"is_company_account": 1, "company": COMPANY, "account": bank_gl()}, "name"),
			"cost_center": w.cc_b, "project": None, "mode_of_payment": "Wire Transfer",
		})
		pe.insert(ignore_permissions=True)
		pe.submit()

		leaves, _data, filters = run_report(w)
		row = leaves[(w.cc_b, w.p3)]
		self.assertAmounts(row, advances_outstanding=6000, paid=6000, cash_balance=-6000, total_expenses=0)
		lines = report.get_drilldown("advances_outstanding", json.dumps(filters), w.cc_b, w.p3)["rows"]
		self.assertEqual([r["voucher_no"] for r in lines], [advance.name])

		# A claim settling 2,500 of it on 10-20 counts from that date on.
		claim = frappe.get_doc({
			"doctype": "Expense Claim", "name": "_PP-EC-" + w.tag, "employee": employee, "company": COMPANY,
			"posting_date": "2026-10-20", "docstatus": 1,
		})
		claim.db_insert()
		frappe.get_doc({
			"doctype": "Expense Claim Advance", "name": frappe.generate_hash(length=10),
			"parent": claim.name, "parenttype": "Expense Claim", "parentfield": "advances", "idx": 1,
			"docstatus": 1, "employee_advance": advance.name, "allocated_amount": 2500,
		}).db_insert()
		leaves, _data, _f = run_report(w)
		self.assertAmounts(leaves[(w.cc_b, w.p3)], advances_outstanding=3500)
		leaves, _data, _f = run_report(w, to_date="2026-10-15")
		self.assertAmounts(leaves[(w.cc_b, w.p3)], advances_outstanding=6000)

	def test_tc52_receipts_only_from_the_sales_order_client(self):
		"""A receipt is Payments Received only when the payer is a customer
		of a Sales Order on the same Cost Center/Project; any other receipt
		is Receipts from Others, outside the Cash Balance."""
		w = self.w
		self.make_sales_order(1000, w.cc_b, w.p3, tax=False)
		make_payment("Receive", 5000, w.cc_b, project=w.p3)  # the client
		make_payment("Receive", 700, w.cc_a, project=w.p1)  # P1 has no Sales Order
		refund = make_payment("Receive", 300, w.cc_b, project=w.p3, submit=False)
		refund.update({"party_type": "Supplier", "party": SUPPLIER,
			"paid_from": frappe.db.get_value("Company", COMPANY, "default_payable_account")})
		refund.save(ignore_permissions=True)
		refund.submit()

		leaves, _data, filters = run_report(w)
		self.assertAmounts(leaves[(w.cc_b, w.p3)], received=5000, other_receipts=300, cash_balance=5000)
		self.assertAmounts(leaves[(w.cc_a, w.p1)], received=0, other_receipts=700, cash_balance=0)
		# Still cash in the ledger, so the GL check shows it.
		self.assertAmounts(leaves[(w.cc_a, w.p1)], gl_cash_difference=-700)
		lines = report.get_drilldown("other_receipts", json.dumps(filters), w.cc_b, w.p3)["rows"]
		self.assertEqual([r["voucher_no"] for r in lines], [refund.name])

	def test_tc23_date_filters(self):
		w = self.w
		make_invoice("Purchase Invoice", w.cc_b, [(111, None, None)], posting_date="2026-09-15")
		make_invoice("Purchase Invoice", w.cc_b, [(222, None, None)], posting_date=add_days(TO_DATE, 1))
		make_invoice("Purchase Invoice", w.cc_b, [(333, None, None)], posting_date="2026-10-15")
		leaves, _data, _f = run_report(w, from_date="2026-10-01")
		self.assertAmounts(leaves[(w.cc_b, w.p3)], pi_net=333)

	def test_tc24_from_after_to(self):
		with self.assertRaises(frappe.ValidationError):
			run_report(self.w, from_date=add_days(TO_DATE, 1))

	def test_tc25_user_permissions(self):
		w = self.w
		make_invoice("Sales Invoice", w.cc_a, [(100, None, None)], project=w.p1)
		make_invoice("Sales Invoice", w.cc_b, [(300, None, None)])  # P3, derived
		make_invoice("Sales Invoice", w.cc_c, [(500, None, None)])  # no Project

		email = f"_pp_{w.tag}@example.com"
		user = frappe.get_doc({"doctype": "User", "email": email, "first_name": "PP", "send_welcome_email": 0})
		user.append("roles", {"role": "Accounts User"})
		user.insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Project", "for_value": w.p1}).insert(
			ignore_permissions=True
		)
		frappe.set_user(email)
		try:
			leaves, _data, filters = run_report(w)
			self.assertIn((w.cc_a, w.p1), leaves)
			self.assertIn((w.cc_c, NO_PROJECT), leaves)
			self.assertNotIn((w.cc_b, w.p3), leaves)
			self.assertEqual(report.get_drilldown("si_net", json.dumps(filters), w.cc_b, w.p3)["rows"], [])
		finally:
			frappe.set_user("Administrator")

	def test_tc26_drilldown_equals_cell(self):
		w = self.w
		self.make_basic()
		self.make_cleared_cheque()
		self.make_sales_order(7000, w.cc_b, w.p3)
		set_guarantee_accounts([account({"name": ["like", GUARANTEE_ACCOUNT_PREFIX]})])
		self.make_guarantee(4000, w.cc_c, "2026-08-01")
		make_journal_entry([(account({"root_type": "Expense"}), 800, 0, w.cc_b), (bank_gl(), 0, 800, w.cc_b)])

		_leaves, data, filters = run_report(w)
		checked = 0
		for row in data:
			for metric in report.AMOUNT_FIELDS:
				if not flt(row[metric]):
					continue
				result = report.get_drilldown(metric, json.dumps(filters), row["cost_center"], row["project"])
				self.assertAlmostEqual(result["total"], row[metric], delta=0.01, msg=f"{row['label']} {metric}")
				checked += 1
		self.assertGreater(checked, 40)

	def test_tc27_invalid_metric(self):
		with self.assertRaises(frappe.ValidationError):
			report.get_drilldown("x; DROP", json.dumps({"company": COMPANY, "to_date": TO_DATE}))

	def test_tc37_settings_rejects_duplicates(self):
		guarantee = account({"name": ["like", GUARANTEE_ACCOUNT_PREFIX]})
		with self.assertRaises(frappe.ValidationError):
			set_guarantee_accounts([guarantee, guarantee])

	def test_tc38_settings_is_single(self):
		self.assertEqual(frappe.get_meta("Project Position Settings").issingle, 1)

	def test_tc40_read_only(self):
		self.make_basic()
		tables = ["GL Entry", "Purchase Invoice", "Sales Invoice", "Payment Entry", "Sales Order", "Cheque"]
		before = {t: frappe.db.count(t) for t in tables}
		run_report(self.w)
		self.assertEqual(before, {t: frappe.db.count(t) for t in tables})

	def test_tc48_report_role(self):
		email = f"_pp_ceo_{self.w.tag}@example.com"
		user = frappe.get_doc({"doctype": "User", "email": email, "first_name": "PP CEO", "send_welcome_email": 0})
		user.append("roles", {"role": "Company CEO"})
		user.insert(ignore_permissions=True)
		frappe.set_user(email)
		try:
			self.assertTrue(frappe.get_doc("Report", report.REPORT_NAME).is_permitted())
			self.assertTrue(frappe.has_permission("Cost Center", "report"))
		finally:
			frappe.set_user("Administrator")
