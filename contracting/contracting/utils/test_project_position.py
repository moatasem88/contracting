# Copyright (c) 2026, kazem and Contributors
# See license.txt

"""Mandatory Project on submit and the Cost Center -> Project rule
(Project & Cost Center Position BRD, FR-02/FR-14/FR-15): TC-28 to TC-33,
TC-41 to TC-45.

The helpers here also build the documents for the report's own tests
(contracting/report/project_cost_center_position). Every test makes its
own fresh Cost Centers and Projects, so existing site data never leaks in:
P1 and P2 share CC-A, P3 is alone on CC-B, CC-C has no Project.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from contracting.contracting.utils.project_position import (
	NO_PROJECT,
	UNASSIGNED,
	cost_center_project_map,
	project_for_cost_center,
	resolve,
	validate_project_mandatory,
)

COMPANY = "Friends"
ROOT_COST_CENTER = "Friends - F"
SUPPLIER = "_Test Subcontractor Supplier"
CUSTOMER = "_Test Tender Customer"
ITEM = "_Test SC Subcontract"
VAT_RATE = 14
# cheques_management requires a company Bank Account on bank payments; CIB
# is the one bank with a Bank Account record on site1.local.
BANK_GL_PREFIX = "121021001%"


def company_bank_account():
	return frappe.db.get_value("Bank Account", {"is_company_account": 1, "company": COMPANY, "account": bank_gl()}, "name")


def bank_gl():
	return frappe.db.get_value("Account", {"company": COMPANY, "is_group": 0, "name": ["like", BANK_GL_PREFIX]}, "name")


def account(filters):
	return frappe.db.get_value("Account", {"company": COMPANY, "is_group": 0, **filters}, "name")


class World(frappe._dict):
	"""CC-A (P1, P2), CC-B (P3), CC-C (no Project), named per test."""

	@classmethod
	def make(cls):
		tag = frappe.generate_hash(length=6)
		world = cls(tag=tag)
		for key in ("a", "b", "c"):
			world[f"cc_{key}"] = frappe.get_doc({
				"doctype": "Cost Center",
				"cost_center_name": f"_PP {key.upper()} {tag}",
				"parent_cost_center": ROOT_COST_CENTER,
				"company": COMPANY,
				"is_group": 0,
			}).insert(ignore_permissions=True).name
		for key, cost_center in (("p1", world.cc_a), ("p2", world.cc_a), ("p3", world.cc_b)):
			world[key] = frappe.get_doc({
				"doctype": "Project",
				"project_name": f"_PP {key.upper()} {tag}",
				"company": COMPANY,
				"cost_center": cost_center,
			}).insert(ignore_permissions=True).name
		return world

	@property
	def cost_centers(self):
		return [self.cc_a, self.cc_b, self.cc_c]


def set_enforced(value):
	frappe.db.set_single_value("Project Position Settings", "enforce_mandatory_project", value)


def make_invoice(doctype, cost_center, lines, project=None, posting_date=None, is_return=False, tax=True, submit=True):
	"""lines: [(net amount, row cost_center or None, row project or None)].
	A row without its own Cost Center takes the header's, as on the form."""
	is_purchase = doctype == "Purchase Invoice"
	doc = frappe.new_doc(doctype)
	doc.update({
		"company": COMPANY,
		"posting_date": posting_date or today(),
		"set_posting_time": 1,
		"due_date": posting_date or today(),
		"cost_center": cost_center,
		"project": project,
		"is_return": 1 if is_return else 0,
	})
	if is_purchase:
		doc.supplier = SUPPLIER
		doc.custom_tax_status = "Taxable"
		doc.bill_no = frappe.generate_hash(length=8)
	else:
		doc.customer = CUSTOMER

	sign = -1 if is_return else 1
	for amount, row_cost_center, row_project in lines:
		row = {
			"item_code": ITEM,
			"qty": sign,
			"rate": amount,
			"cost_center": row_cost_center or cost_center,
			"project": row_project,
		}
		if is_purchase:
			row["expense_account"] = account({"root_type": "Expense"})
		else:
			row["income_account"] = account({"root_type": "Income"})
		doc.append("items", row)

	if tax:
		doc.append("taxes", {
			"charge_type": "On Net Total",
			"account_head": account({"account_type": "Tax", "name": ["like", "125010002%" if is_purchase else "2350%"]}),
			"description": "VAT",
			"rate": VAT_RATE,
			"cost_center": cost_center,
		})
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


def make_payment(payment_type, amount, cost_center, project=None, posting_date=None,
		mode_of_payment="Wire Transfer", paid_from=None, paid_to=None, submit=True):
	bank = bank_gl()
	pay = payment_type == "Pay"
	doc = frappe.get_doc({
		"doctype": "Payment Entry",
		"payment_type": payment_type,
		"company": COMPANY,
		"posting_date": posting_date or today(),
		"mode_of_payment": mode_of_payment,
		"party_type": "Supplier" if pay else "Customer",
		"party": SUPPLIER if pay else CUSTOMER,
		"paid_from": paid_from or (bank if pay else frappe.db.get_value("Company", COMPANY, "default_receivable_account")),
		"paid_to": paid_to or (frappe.db.get_value("Company", COMPANY, "default_payable_account") if pay else bank),
		"paid_amount": amount,
		"received_amount": amount,
		"source_exchange_rate": 1,
		"target_exchange_rate": 1,
		"bank_account": company_bank_account(),
		"reference_no": frappe.generate_hash(length=8),
		"reference_date": posting_date or today(),
		"cost_center": cost_center,
		"project": project,
	})
	doc.setup_party_account_field()
	doc.set_missing_values()
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


def make_internal_transfer(amount, cost_center, submit=True):
	bank = bank_gl()
	cash = account({"account_type": "Cash", "name": ["like", "121010001%"]})
	return make_payment("Internal Transfer", amount, cost_center, paid_from=bank, paid_to=cash, submit=submit)


class TestProjectFromCostCenter(FrappeTestCase):
	def setUp(self):
		self.w = World.make()

	def test_map_and_resolve(self):
		w = self.w
		cc_map = cost_center_project_map(w.cost_centers)
		self.assertEqual(cc_map, {w.cc_a: UNASSIGNED, w.cc_b: w.p3})
		self.assertEqual(resolve(w.cc_a, None, cc_map), (w.cc_a, UNASSIGNED))
		self.assertEqual(resolve(w.cc_b, None, cc_map), (w.cc_b, w.p3))
		self.assertEqual(resolve(w.cc_c, None, cc_map), (w.cc_c, NO_PROJECT))
		# An explicit Project wins even on another Project's Cost Center.
		self.assertEqual(resolve(w.cc_b, w.p1, cc_map), (w.cc_b, w.p1))

	def test_form_default_lookup(self):
		"""TC-34/35 server side: unique -> that Project, shared/none -> nothing."""
		self.assertEqual(project_for_cost_center(self.w.cc_b), self.w.p3)
		self.assertIsNone(project_for_cost_center(self.w.cc_a))
		self.assertIsNone(project_for_cost_center(self.w.cc_c))


class TestMandatoryProject(FrappeTestCase):
	def setUp(self):
		self.w = World.make()
		set_enforced(1)

	def assert_refused(self, doc, *fragments):
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_project_mandatory(doc)
		for fragment in fragments:
			self.assertIn(fragment, str(ctx.exception))
		return str(ctx.exception)

	def test_tc28_header_refused_on_submit(self):
		pi = make_invoice("Purchase Invoice", self.w.cc_b, [(1000, None, None)], submit=False)
		with self.assertRaises(frappe.ValidationError) as ctx:
			pi.submit()
		self.assertIn(f"header (Cost Center {self.w.cc_b})", str(ctx.exception))

	def test_tc29_back_dated_still_checked(self):
		pi = make_invoice("Purchase Invoice", self.w.cc_b, [(1000, None, None)], posting_date="2025-06-01", submit=False)
		self.assert_refused(pi, "header")

	def test_tc30_overhead_cost_center_allowed(self):
		pe = make_payment("Pay", 5000, self.w.cc_c)
		self.assertEqual(pe.docstatus, 1)

	def test_tc31_exemptions(self):
		transfer = make_internal_transfer(7000, self.w.cc_a)
		self.assertEqual(transfer.docstatus, 1)
		clearing = make_payment("Pay", 7000, self.w.cc_a, mode_of_payment="Bank Clearance", submit=False)
		validate_project_mandatory(clearing)  # no error

	def test_tc32_switch_off(self):
		set_enforced(0)
		pi = make_invoice("Purchase Invoice", self.w.cc_b, [(1000, None, None)])
		self.assertEqual(pi.docstatus, 1)

	def test_tc33_only_before_submit(self):
		"""Already-submitted documents are never re-checked: the rule hangs
		on before_submit only, not on validate/on_update_after_submit."""
		for doctype in ("Purchase Invoice", "Sales Invoice", "Payment Entry", "Sales Order"):
			events = frappe.get_hooks("doc_events").get(doctype, {})
			hook = "contracting.contracting.utils.project_position.validate_project_mandatory"
			self.assertIn(hook, events.get("before_submit", []), doctype)
			for event, handlers in events.items():
				if event != "before_submit":
					self.assertNotIn(hook, handlers, f"{doctype}.{event}")

	def test_sales_order_and_payment_header(self):
		pe = make_payment("Receive", 1000, self.w.cc_b, submit=False)
		self.assert_refused(pe, "header")
		so = frappe._dict(doctype="Sales Order", cost_center=self.w.cc_a, project=None, items=[])
		self.assert_refused(so, "header")

	def test_tc41_row_check(self):
		pi = make_invoice("Purchase Invoice", self.w.cc_c, [(100, None, None), (200, self.w.cc_b, None)], submit=False)
		message = self.assert_refused(pi, f"row 2 (Cost Center {self.w.cc_b})")
		self.assertNotIn("header", message)

	def test_tc42_row_with_project_allowed(self):
		pi = make_invoice("Purchase Invoice", self.w.cc_c, [(100, None, None), (200, self.w.cc_b, self.w.p3)])
		self.assertEqual(pi.docstatus, 1)

	def test_tc43_row_header_mismatch(self):
		w = self.w
		pi = make_invoice(
			"Purchase Invoice", w.cc_a, [(100, None, None), (100, None, None), (200, w.cc_b, None)],
			project=w.p1, submit=False,
		)
		self.assert_refused(pi, f"row 3 (Cost Center {w.cc_b}, header Project {w.p1} belongs to {w.cc_a})")

	def test_tc44_rows_inherit_header(self):
		pi = make_invoice("Purchase Invoice", self.w.cc_b, [(100, None, None), (200, None, None)], project=self.w.p3)
		self.assertEqual(pi.docstatus, 1)

	def test_tc45_one_message_for_all(self):
		w = self.w
		si = make_invoice("Sales Invoice", w.cc_a, [(100, None, None), (200, w.cc_b, None), (300, w.cc_b, None)], submit=False)
		message = self.assert_refused(si, "header", "row 2", "row 3")
		self.assertEqual(message.count("Project is required"), 1)

	def tearDown(self):
		set_enforced(1)
