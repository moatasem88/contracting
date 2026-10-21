import base64
import os

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.api.external import v1
from contracting.contracting.api.external.v1_test_utils import as_user, make_user
from contracting.contracting.doctype.contractor_invoice.test_contractor_invoice import (
	make_charged_contract,
	make_condition_contract,
)
from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	make_draft_contract,
	make_resource_tender_and_project,
)

PDF = base64.b64encode(b"%PDF-1.4 v1 api test").decode()
FILE_PREFIX = "_v1_api_test_"


class TestV1ContractorInvoice(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.site_engineer = make_user("Site Engineer")
		cls.chain = [
			make_user("Project Manager"),
			make_user("Cost Control Engineer"),
			make_user("cost control manager"),
			make_user("Planning and Cost Control Director"),
			make_user("Accountant"),
			make_user("Accounts Manager"),
		]

	def setUp(self):
		self.addCleanup(self._remove_test_files)

	@staticmethod
	def _remove_test_files():
		folder = frappe.get_site_path("private", "files")
		for name in os.listdir(folder) if os.path.isdir(folder) else []:
			if name.startswith(FILE_PREFIX):
				os.remove(os.path.join(folder, name))

	def _attach(self, invoice, fieldname, modified):
		# Distinct bytes per file: File dedupes identical content by hash, and
		# create_purchase_invoice re-reads each attachment from disk.
		content = base64.b64encode(f"%PDF-1.4 {invoice} {fieldname}".encode()).decode()
		return v1.attach_contractor_invoice_file(invoice, fieldname, modified,
												 FILE_PREFIX + invoice + "_" + fieldname + ".pdf", content)

	# -- FR-26 drafts -----------------------------------------------------------------

	def test_tc29_unapproved_contract_refused(self):
		project, tender = make_resource_tender_and_project("v1_ci_draft", qty=10)
		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {"work_item": tender.boq_items[0].name, "qty": 10, "unit_price": 100})
		contract.insert(ignore_permissions=True)
		with as_user(self.site_engineer):
			with self.assertRaisesRegex(frappe.ValidationError, "must be Approved"):
				v1.create_contractor_invoice_draft(contract.name)

	def test_plain_contract_draft_starts_empty_then_lines_added(self):
		contract = make_charged_contract("v1_ci_plain")
		with as_user(self.site_engineer):
			self.assertIn(contract.name, [c["name"] for c in v1.list_invoiceable_contracts(contract.project)])
			draft = v1.create_contractor_invoice_draft(contract.name)
			self.assertEqual(draft["data"]["items"], [])
			self.assertEqual(draft["meta"]["entry_table"], "items")
			lines = v1.list_invoiceable_lines(contract.name)
			added = v1.add_contractor_invoice_lines(draft["data"]["name"], draft["meta"]["modified"],
													 [lines[0]["contract_item"]])
		self.assertEqual([r["contract_item"] for r in added["data"]["items"]], [contract.contracted_items[0].name])
		self.assertRaises(frappe.ValidationError, v1.add_contractor_invoice_lines, added["data"]["name"],
						  added["meta"]["modified"], ["not-a-line"])

	def test_condition_contract_draft_is_seeded(self):
		contract = make_condition_contract("v1_ci_cond")
		with as_user(self.site_engineer):
			draft = v1.create_contractor_invoice_draft(contract.name)
		self.assertEqual(draft["meta"]["entry_table"], "condition_progress")
		self.assertEqual(len(draft["data"]["condition_progress"]), 2)
		self.assertEqual(len(draft["data"]["items"]), 1)

	# -- FR-27 progress entry ----------------------------------------------------------

	def test_tc30_three_progress_inputs_convert_to_cumulative(self):
		contract = make_charged_contract("v1_ci_entry")
		draft = v1.create_contractor_invoice_draft(contract.name)
		name = draft["data"]["name"]
		draft = v1.add_contractor_invoice_lines(name, draft["meta"]["modified"], [contract.contracted_items[0].name])
		row = draft["data"]["items"][0]["name"]

		r = v1.update_contractor_invoice_progress(name, draft["meta"]["modified"],
												  [{"row_name": row, "percent_complete": 50}])
		self.assertAlmostEqual(flt(r["data"]["items"][0]["cumulative_qty_complete"]), 5)
		r = v1.update_contractor_invoice_progress(name, r["meta"]["modified"],
												  [{"row_name": row, "this_period_qty": 3}])
		self.assertAlmostEqual(flt(r["data"]["items"][0]["cumulative_qty_complete"]), 3)
		r = v1.update_contractor_invoice_progress(name, r["meta"]["modified"],
												  [{"row_name": row, "cumulative_qty_complete": 4}])
		item = r["data"]["items"][0]
		self.assertAlmostEqual(flt(item["this_period_amount"]), 400)
		self.assertAlmostEqual(flt(r["data"]["total_this_period"]), 400)
		self.assertTrue(r["data"]["additional_costs"])  # retention + VAT mirrored from the contract

	def test_tc31_over_progress_refused(self):
		contract = make_charged_contract("v1_ci_over")
		draft = v1.create_contractor_invoice_draft(contract.name)
		draft = v1.add_contractor_invoice_lines(draft["data"]["name"], draft["meta"]["modified"],
												[contract.contracted_items[0].name])
		with self.assertRaisesRegex(frappe.ValidationError, "cannot exceed"):
			v1.update_contractor_invoice_progress(
				draft["data"]["name"], draft["meta"]["modified"],
				[{"row_name": draft["data"]["items"][0]["name"], "cumulative_qty_complete": 11}],
			)

	def test_exactly_one_progress_field(self):
		contract = make_charged_contract("v1_ci_one")
		draft = v1.create_contractor_invoice_draft(contract.name)
		draft = v1.add_contractor_invoice_lines(draft["data"]["name"], draft["meta"]["modified"],
												[contract.contracted_items[0].name])
		self.assertRaises(
			frappe.ValidationError, v1.update_contractor_invoice_progress, draft["data"]["name"],
			draft["meta"]["modified"],
			[{"row_name": draft["data"]["items"][0]["name"], "percent_complete": 10, "this_period_qty": 1}],
		)

	# -- FR-28 workflow, attachments, billing accounts -----------------------------------

	def test_tc32_full_chain_from_the_app(self):
		contract = make_charged_contract("v1_ci_chain")
		with as_user(self.site_engineer):
			draft = v1.create_contractor_invoice_draft(contract.name)
			name = draft["data"]["name"]
			draft = v1.add_contractor_invoice_lines(name, draft["meta"]["modified"],
													[contract.contracted_items[0].name])
			view = v1.update_contractor_invoice_progress(
				name, draft["meta"]["modified"],
				[{"row_name": draft["data"]["items"][0]["name"], "cumulative_qty_complete": 4}],
			)
			# Submit for Review is hidden until the invoice is attached.
			self.assertEqual(view["meta"]["allowed_actions"], [])
			view = self._attach(name, "invoice_attachment", view["meta"]["modified"])
			self.assertEqual([a["action"] for a in view["meta"]["allowed_actions"]], ["Submit for Review"])
			view = v1.apply_contractor_invoice_action(name, view["meta"]["modified"], "Submit for Review")
		self.assertEqual(view["meta"]["workflow_state"], "Pending Project Manager Approval")
		self.assertEqual(frappe.db.get_value("Contractor Invoice", name, "modified_by"), self.site_engineer)

		pm, cce, ccm, director, accountant, accounts_manager = self.chain
		with as_user(pm):
			inbox = v1.list_my_pending_approvals()["data"]
			self.assertIn(name, [d["name"] for d in inbox])
			view = v1.apply_contractor_invoice_action(name, view["meta"]["modified"], "Approve")
		with as_user(cce):
			view = v1.get_contractor_invoice_view(name)
			self.assertEqual([a["action"] for a in view["meta"]["allowed_actions"]], ["Reject"])
			view = self._attach(name, "checklist_attachment", view["meta"]["modified"])
			view = v1.apply_contractor_invoice_action(name, view["meta"]["modified"], "Approve")
		for user in (ccm, director, accountant):
			with as_user(user):
				view = v1.apply_contractor_invoice_action(name, view["meta"]["modified"], "Approve")
		self.assertEqual(view["meta"]["workflow_state"], "Pending Accounts Manager Approval")

		with as_user(accounts_manager):
			preview = v1.preview_contractor_invoice_action(name, "Approve")
			self.assertTrue(preview["requires_billing_accounts"])
			rows = preview["billing_accounts"]
			self.assertTrue(rows)
			# Refused without the confirmation, and nothing changes.
			self.assertRaises(frappe.ValidationError, v1.apply_contractor_invoice_action, name,
							  view["meta"]["modified"], "Approve")
			company = frappe.db.get_value("Subcontractor Contract", contract.name, "company")
			account = frappe.db.get_value("Account", {"company": company, "root_type": "Expense", "is_group": 0})
			# A row that isn't one of this invoice's billed rows is refused too.
			self.assertRaises(frappe.ValidationError, v1.apply_contractor_invoice_action, name,
							  view["meta"]["modified"], "Approve", [{"name": "x", "account": account}])
			view = v1.apply_contractor_invoice_action(
				name, view["meta"]["modified"], "Approve", [{"name": r["name"], "account": account} for r in rows],
			)
		self.assertEqual(view["meta"]["workflow_state"], "Approved")
		self.assertEqual(view["meta"]["docstatus"], 1)
		self.assertTrue(view["data"]["purchase_invoice"])
		self.assertEqual(frappe.db.get_value("Contractor Invoice Item", rows[0]["name"], "account"), account)

	def test_attach_whitelist_and_window(self):
		contract = make_charged_contract("v1_ci_attach")
		draft = v1.create_contractor_invoice_draft(contract.name)
		name, modified = draft["data"]["name"], draft["meta"]["modified"]
		self.assertRaises(frappe.PermissionError, v1.attach_contractor_invoice_file, name, "naming_series", modified,
						  FILE_PREFIX + "x.pdf", PDF)
		# checklist_attachment may only change during Cost Control Engineer review.
		self.assertRaises(frappe.ValidationError, self._attach, name, "checklist_attachment", modified)

	def test_tc37_role_without_action_gets_error(self):
		contract = make_charged_contract("v1_ci_noact")
		draft = v1.create_contractor_invoice_draft(contract.name)
		with as_user(self.chain[0]):  # Project Manager can't submit a Draft
			with self.assertRaisesRegex(frappe.PermissionError, "Action not allowed"):
				v1.apply_contractor_invoice_action(draft["data"]["name"], draft["meta"]["modified"],
												   "Submit for Review")
