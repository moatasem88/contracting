import base64

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.api.external import v1
from contracting.contracting.api.external.v1_test_utils import as_user, make_user
from contracting.contracting.doctype.contractor_contract_addendum.test_contractor_contract_addendum import (
	BASE_UNIT_PRICE,
	make_amendable_contract,
)
from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import make_v2_template


def rows_by_base(response):
	return {r["base_contract_item"]: r for r in response["data"]["addendum_items"] if r.get("base_contract_item")}


class TestV1Addendum(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cm = make_user("Contracts Manager")
		cls.ce = make_user("Contracts Engineer")

	def test_draft_options_and_update(self):
		contract, _boq_row = make_amendable_contract("v1_add_update")
		base = contract.contracted_items[0].name
		with as_user(self.cm):
			options = v1.get_addendum_draft_options(contract.name)
			self.assertEqual([r["base_contract_item"] for r in options["items"]], [base])
			response = v1.create_addendum_draft(contract.name, items=[
				{"base_contract_item": base, "action": "Update", "new_qty": 8, "new_unit_price": 120},
			])
		row = rows_by_base(response)[base]
		self.assertEqual((flt(row["old_qty"]), flt(row["qty_delta"])), (5, 3))
		self.assertAlmostEqual(flt(response["data"]["grand_total_delta"]), 8 * 120 - 5 * BASE_UNIT_PRICE)
		self.assertEqual(response["meta"]["workflow_state"], "Draft")
		# The contract itself is untouched until approval.
		self.assertEqual(flt(frappe.db.get_value("Contractor Contract Item", base, "qty")), 5)

	def test_delete_and_undo(self):
		contract, _boq_row = make_amendable_contract("v1_add_delete")
		base = contract.contracted_items[0].name
		response = v1.create_addendum_draft(contract.name, items=[{"base_contract_item": base, "action": "Delete"}])
		self.assertAlmostEqual(flt(response["data"]["grand_total_delta"]), -5 * BASE_UNIT_PRICE)
		with self.assertRaisesRegex(frappe.ValidationError, "at least one row"):
			# Undoing the only change leaves an addendum that changes nothing.
			v1.update_addendum_draft(response["data"]["name"], response["meta"]["modified"],
									 items=[{"base_contract_item": base, "action": ""}])

	def test_add_template_row_then_edit_and_remove(self):
		contract, _boq_row = make_amendable_contract("v1_add_add")
		template = make_v2_template("_v1 Addendum Template").name
		response = v1.create_addendum_draft(contract.name, items=[
			{"action": "Add", "work_item_template": template, "qty_delta": 2, "unit_price": 40},
		])
		added = [r for r in response["data"]["addendum_items"] if r["action"] == "Add"]
		self.assertEqual(len(added), 1)
		self.assertAlmostEqual(flt(added[0]["amount"]), 80)

		response = v1.update_addendum_draft(response["data"]["name"], response["meta"]["modified"],
											items=[{"row_name": added[0]["name"], "qty_delta": 3}])
		added = [r for r in response["data"]["addendum_items"] if r["action"] == "Add"]
		self.assertAlmostEqual(flt(added[0]["amount"]), 120)

		base = contract.contracted_items[0].name
		response = v1.update_addendum_draft(response["data"]["name"], response["meta"]["modified"], items=[
			{"row_name": added[0]["name"], "remove": True},
			{"base_contract_item": base, "action": "Update", "new_qty": 6, "new_unit_price": BASE_UNIT_PRICE},
		])
		self.assertFalse([r for r in response["data"]["addendum_items"] if r["action"] == "Add"])

	def test_add_tender_row_with_payment_conditions(self):
		contract, boq_row = make_amendable_contract("v1_add_cond", with_conditions=True)
		def create(*percents):
			return v1.create_addendum_draft(
				contract.name,
				items=[{"action": "Add", "work_item": boq_row.name, "qty_delta": 2, "unit_price": 150}],
				payment_conditions=[
					{"work_item": boq_row.name, "condition": "Stage %s" % i, "percent": p}
					for i, p in enumerate(percents, 1)
				],
			)
		self.assertRaises(frappe.ValidationError, create, 50, 40)
		response = create(50, 50)
		self.assertEqual(len(response["data"]["addendum_payment_conditions"]), 2)

	def test_bad_inputs_rejected(self):
		contract, _boq_row = make_amendable_contract("v1_add_bad")
		base = contract.contracted_items[0].name
		for items in (
			[{"base_contract_item": "nope", "action": "Delete"}],
			[{"base_contract_item": base, "action": "Add"}],
			[{"base_contract_item": base, "action": "Update", "new_qty": 6, "new_unit_price": 1, "amount": 0}],
			[{"action": "Add", "work_item_template": "x", "work_item": "y", "qty_delta": 1, "unit_price": 1}],
			[{"action": "Add", "work_item_template": "x", "qty_delta": 0, "unit_price": 1}],
			[{"qty_delta": 1}],
		):
			self.assertRaises(frappe.ValidationError, v1.create_addendum_draft, contract.name, items=items)

	def test_tc33_approval_from_the_app(self):
		contract, _boq_row = make_amendable_contract("v1_add_wf")
		base = contract.contracted_items[0].name
		with as_user(self.cm):
			response = v1.create_addendum_draft(contract.name, items=[
				{"base_contract_item": base, "action": "Update", "new_qty": 6, "new_unit_price": BASE_UNIT_PRICE},
			])
			name = response["data"]["name"]
			response = v1.apply_addendum_action(name, response["meta"]["modified"], "Submit for Approval")
			response = v1.apply_addendum_action(name, response["meta"]["modified"], "Approve")
			self.assertRaises(frappe.ValidationError, v1.update_addendum_draft, name, response["meta"]["modified"],
							  items=[{"base_contract_item": base, "action": "Delete"}])
		with as_user(make_user("Projects Director")):
			response = v1.apply_addendum_action(name, response["meta"]["modified"], "Approve")
		with as_user(make_user("Cost Control Engineer")):
			content = base64.b64encode(f"%PDF {name}".encode()).decode()
			response = v1.attach_addendum_file(name, "checklist_attachment", response["meta"]["modified"],
											   "_v1_api_test_" + name + ".pdf", content)
			response = v1.apply_addendum_action(name, response["meta"]["modified"], "Approve")
		with as_user(make_user("cost control manager")):
			response = v1.apply_addendum_action(name, response["meta"]["modified"], "Approve")
		with as_user(make_user("Planning and Cost Control Director")):
			response = v1.apply_addendum_action(name, response["meta"]["modified"], "Approve")
		if response["meta"]["workflow_state"] == "Pending CEO Approval":
			with as_user(make_user("Company CEO")):
				response = v1.apply_addendum_action(name, response["meta"]["modified"], "Approve")
		self.assertEqual(response["meta"]["workflow_state"], "Approved")
		self.assertEqual(flt(frappe.db.get_value("Contractor Contract Item", base, "qty")), 6)
		frappe.delete_doc("File", frappe.get_all("File", {"attached_to_name": name}, pluck="name"),
						  ignore_permissions=True, force=True)

	def test_contracts_engineer_cannot_author(self):
		contract, _boq_row = make_amendable_contract("v1_add_ce")
		with as_user(self.ce):
			self.assertRaises(frappe.PermissionError, v1.get_addendum_draft_options, contract.name)
			self.assertRaises(frappe.PermissionError, v1.create_addendum_draft, contract.name)

	def test_unapproved_contract_refused(self):
		contract, _boq_row = make_amendable_contract("v1_add_unappr")
		contract.db_set("workflow_state", "Pending CEO Approval")
		with self.assertRaisesRegex(frappe.ValidationError, "Approved contract"):
			v1.create_addendum_draft(contract.name)
