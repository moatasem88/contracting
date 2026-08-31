# Copyright (c) 2026, kazem and Contributors
# See license.txt

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	ensure_resource_item,
	make_draft_contract,
	make_resource_tender_and_project,
	submit_contract,
)
from contracting.contracting.utils import allocation


def make_installing_contract(suffix, item_qty=10, labor_qty=8, unit_price=100):
	"""A submitted Install Only contract with one Labor row - the base an
	Addendum (FR-34-46) amends."""
	project, tender = make_resource_tender_and_project(suffix, qty=item_qty)
	boq_row = tender.boq_items[0]
	labor_row = tender.labor_items[0]

	contract = make_draft_contract(project, "Installing")
	contract.append("contracted_items", {"work_item": boq_row.name, "qty": item_qty, "unit_price": unit_price})
	contract.append("contract_labor_items", {
		"work_item": boq_row.name, "labor_item": labor_row.name, "qty": labor_qty,
	})
	contract.insert(ignore_permissions=True)
	submit_contract(contract)
	return contract, tender, boq_row, labor_row


def approve_addendum(addendum):
	"""Drives the addendum through its own workflow's actions (not a raw
	.submit() - Frappe auto-syncs workflow_state to whichever configured
	state matches the new docstatus on a direct submit(), which would jump
	straight to Approved and skip the approval chain entirely)."""
	apply_workflow(addendum, "Submit for Approval")
	apply_workflow(addendum, "Approve")  # Contracts Manager: -> Pending Cost Control
	apply_workflow(addendum, "Approve")  # Cost Control: -> Approved (or Pending CEO)
	if addendum.workflow_state == "Pending CEO Approval":
		apply_workflow(addendum, "Approve")
	return addendum


class TestContractorContractAddendum(FrappeTestCase):
	def test_approval_applies_delta_and_comments(self):
		"""FR-42-44/FR-16/FR-17: once Approved, an addendum's items become
		new rows on the base contract's Contracted Items, plus every
		resource row its work item has linked on the Tender - propagated
		automatically, with no labor_item hand-picked on the addendum item
		at all - and the contract's totals are recalculated with a comment
		left behind."""
		contract, tender, boq_row, labor_row = make_installing_contract("addendum_apply", item_qty=10, labor_qty=8)
		before_items = len(contract.contracted_items)
		before_labor = len(contract.contract_labor_items)
		before_grand_total = contract.grand_total

		addendum = frappe.get_doc({
			"doctype": "Contractor Contract Addendum",
			"naming_series": "Subcontractor-ADDENDUM-.YYYY.-",
			"subcontractor_contract": contract.name,
		})
		addendum.append("addendum_items", {
			"work_item": boq_row.name, "qty_delta": 1, "unit_price": 999,
		})
		addendum.insert(ignore_permissions=True)
		self.assertEqual([r.tender for r in addendum.project_tenders], [tender.name])
		self.assertEqual(addendum.type_subcontractor, "Installing")
		self.assertAlmostEqual(addendum.grand_total_delta, 999.0, delta=1e-6)

		approve_addendum(addendum)
		self.assertEqual(addendum.workflow_state, "Approved")
		self.assertEqual(addendum.docstatus, 1)

		contract.reload()
		self.assertEqual(len(contract.contracted_items), before_items + 1)
		self.assertEqual(len(contract.contract_labor_items), before_labor + 1)
		new_labor_row = contract.contract_labor_items[-1]
		self.assertEqual(new_labor_row.work_item, boq_row.name)
		self.assertEqual(new_labor_row.labor_item, labor_row.name)
		self.assertAlmostEqual(new_labor_row.qty, 1.0, delta=1e-6)  # qty_per_unit(1) x qty_delta(1)
		self.assertGreater(contract.grand_total, before_grand_total)

		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Subcontractor Contract", "reference_name": contract.name},
			fields=["content"], order_by="creation desc", limit=1,
		)
		self.assertTrue(comments and addendum.name in comments[0].content)

	def test_addendum_no_longer_requires_resource_pick(self):
		"""FR-16/TC-12: an addendum item needs no labor_item/equipment_item
		- resource selection is entirely automatic on approval now."""
		contract, tender, boq_row, labor_row = make_installing_contract(
			"addendum_no_pick", item_qty=10, labor_qty=2,
		)

		addendum = frappe.get_doc({
			"doctype": "Contractor Contract Addendum",
			"naming_series": "Subcontractor-ADDENDUM-.YYYY.-",
			"subcontractor_contract": contract.name,
		})
		addendum.append("addendum_items", {"work_item": boq_row.name, "qty_delta": 1, "unit_price": 500})
		addendum.insert(ignore_permissions=True)  # no labor_item set - must not throw
		addendum.submit()
		self.assertEqual(addendum.docstatus, 1)

	def test_addendum_approval_propagates_every_linked_resource_row(self):
		"""FR-17/TC-11: approval propagates every one of a work item's
		linked Tender Labor Item rows, not just one the addendum happens to
		name (it names none at all now)."""
		project, tender = make_resource_tender_and_project("addendum_multi_labor", qty=10)
		boq_row = tender.boq_items[0]
		first_labor = tender.labor_items[0]  # qty_per_unit = 1
		second_labor_item = ensure_resource_item("_Test SC Labor 2")
		tender.append("labor_items", {
			"work_item": boq_row.idx, "item": second_labor_item, "qty_per_unit": 2, "rate": 60, "currency": "EGP",
		})
		tender.save(ignore_permissions=True)
		boq_row = tender.boq_items[0]

		contract = make_draft_contract(project, "Installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 10, "unit_price": 100})
		contract.insert(ignore_permissions=True)
		submit_contract(contract)
		before_labor = len(contract.contract_labor_items)

		addendum = frappe.get_doc({
			"doctype": "Contractor Contract Addendum",
			"naming_series": "Subcontractor-ADDENDUM-.YYYY.-",
			"subcontractor_contract": contract.name,
		})
		addendum.append("addendum_items", {"work_item": boq_row.name, "qty_delta": 3, "unit_price": 500})
		addendum.insert(ignore_permissions=True)
		approve_addendum(addendum)

		contract.reload()
		self.assertEqual(len(contract.contract_labor_items), before_labor + 2)
		qty_by_labor_item = {r.labor_item: r.qty for r in contract.contract_labor_items}
		self.assertAlmostEqual(qty_by_labor_item[first_labor.name], 3.0, delta=1e-6)  # 1 x 3
		second_labor_name = frappe.db.get_value(
			"Tender Labor Item", {"parent": tender.name, "item": second_labor_item}, "name",
		)
		self.assertAlmostEqual(qty_by_labor_item[second_labor_name], 6.0, delta=1e-6)  # 2 x 3

	def test_addendum_approval_blocks_over_allocation(self):
		"""apply_on_approval writes resource rows directly (db_insert),
		bypassing the base contract's own validate() - this is the only
		place left to catch an over-allocation once FR-16 stopped requiring
		the addendum to name (and so pre-validate against) a specific
		resource row."""
		contract, tender, boq_row, labor_row = make_installing_contract(
			"addendum_over_alloc", item_qty=10, labor_qty=8,
		)
		# The labor row's own budget is qty_per_unit(1) x item qty(10) = 10;
		# 8 is already committed by the base contract - only 2 remain, but
		# validate_availability's own pre-submit check (item-level now, not
		# resource-specific) only sees the item's Supply-and-Install pool,
		# which nothing has touched, so it lets this addendum through to
		# submission/approval - apply_on_approval's own guard must still
		# catch it there.
		addendum = frappe.get_doc({
			"doctype": "Contractor Contract Addendum",
			"naming_series": "Subcontractor-ADDENDUM-.YYYY.-",
			"subcontractor_contract": contract.name,
		})
		addendum.append("addendum_items", {"work_item": boq_row.name, "qty_delta": 5, "unit_price": 500})
		addendum.insert(ignore_permissions=True)

		# A raw submit() (rather than approve_addendum's workflow actions)
		# jumps workflow_state straight to Approved, since it's the only
		# state with doc_status=1 - which is exactly what makes this a
		# useful test here: validate_availability's own pre-submit check
		# (before_submit) already ran and passed by this point, so any
		# ValidationError below can only be apply_on_approval's own guard.
		with self.assertRaises(frappe.ValidationError):
			addendum.submit()

	def test_availability_guard_respects_base_contract_commitment(self):
		"""FR-37: the base contract's own commitment is real and still
		stands - it isn't excluded from the addendum's own availability
		check, since the addendum only adds to it rather than replacing it,
		so a delta that would push the total over the row's budget is
		blocked the same as any other new claim would be."""
		contract, tender, boq_row, labor_row = make_installing_contract("addendum_guard", item_qty=10, labor_qty=8)

		# The labor row's own budget (qty_per_unit=1 x item qty=10) is 10;
		# 8 is already committed by the base contract itself, leaving only
		# 2 - asking the addendum for 3 more exceeds that.
		addendum = frappe.get_doc({
			"doctype": "Contractor Contract Addendum",
			"naming_series": "Subcontractor-ADDENDUM-.YYYY.-",
			"subcontractor_contract": contract.name,
		})
		addendum.append("addendum_items", {
			"work_item": boq_row.name, "labor_item": labor_row.name, "qty_delta": 3, "unit_price": 50,
		})
		addendum.insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			addendum.submit()
