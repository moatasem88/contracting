# Copyright (c) 2026, kazem and Contributors
# See license.txt

import frappe
from frappe.model.workflow import apply_workflow
from unittest import mock

from frappe.tests.utils import FrappeTestCase

from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	ensure_resource_item,
	make_draft_contract,
	add_tender_line,
	make_resource_tender_and_project,
	submit_contract,
)
from contracting.contracting.utils import allocation


# 2026-10-18 BRD: a Labor contract's price ceiling is the work item's labor
# cost per unit - 1 x 50 in make_resource_tender_and_project - so base
# contracts here are priced at it.
BASE_UNIT_PRICE = 50


def make_installing_contract(suffix, item_qty=10, labor_qty=8, unit_price=None):
	"""A submitted Labor contract (Install Only until 2026-10-18) with one
	Labor row - the base an Addendum (FR-34-46) amends.

	2026-10-20: a Total-priced line's amount is split over its components,
	so with fewer labor units than item units the labor rate is the line
	amount / labor_qty - the default price keeps it at the Tender rate."""
	if unit_price is None:
		unit_price = BASE_UNIT_PRICE * labor_qty / item_qty
	project, tender = make_resource_tender_and_project(suffix, qty=item_qty)
	boq_row = tender.boq_items[0]
	labor_row = tender.labor_items[0]

	contract = make_draft_contract(project, "Labor")
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
	# Contracts Manager -> Projects Director -> Cost Control Engineer (needs the
	# checklist) -> cost control manager -> Planning Director -> (CEO) -> Approved.
	for _step in range(8):
		if addendum.workflow_state == "Approved":
			break
		if addendum.workflow_state == "Pending Cost Control Engineer Review" and not addendum.checklist_attachment:
			addendum.checklist_attachment = "/files/test-checklist.pdf"
			addendum.save(ignore_permissions=True)
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
		self.assertEqual(addendum.type_subcontractor, "Labor")
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

		contract = make_draft_contract(project, "Labor")
		# qty 5: the base contract now really commits both labor lines (a new
		# Tender line takes all its lines, 2026-10-20), so leave room for +3.
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 5, "unit_price": BASE_UNIT_PRICE})
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


class TestAddendumTemplateLines(FrappeTestCase):
	def test_template_addendum_inserts_row_and_escalates(self):
		"""Follows Tender BRD FR-16 / OQ-3 / OQ-5 / TC-24: an addendum on a
		template line skips availability, escalates to the CEO on a Tender
		project, and on approval inserts a new Contracted Items row (marked
		with its addendum, so FR-06 doesn't reject it) plus its template
		resource rows, all red."""
		from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
			add_template_line,
			make_v2_template,
		)

		template = make_v2_template("_SC FT Addendum").name
		project, tender = make_resource_tender_and_project("addendum_tpl", qty=10)
		contract = make_draft_contract(project, "Labor")
		add_template_line(contract, template, 2)
		contract.insert(ignore_permissions=True)
		submit_contract(contract)

		addendum = frappe.get_doc({
			"doctype": "Contractor Contract Addendum",
			"naming_series": "Subcontractor-ADDENDUM-.YYYY.-",
			"subcontractor_contract": contract.name,
		})
		addendum.append("addendum_items", {"work_item_template": template, "qty_delta": 1000, "unit_price": 7})
		addendum.insert(ignore_permissions=True)
		self.assertEqual(addendum.requires_ceo_approval, 1)

		approve_addendum(addendum)
		self.assertEqual(addendum.workflow_state, "Approved")

		contract.reload()
		new_line = contract.contracted_items[-1]
		self.assertEqual((new_line.work_item_template, new_line.addendum, new_line.out_of_tender_scope),
			(template, addendum.name, 1))
		self.assertEqual(len(contract.contracted_items), 2)
		self.assertEqual(contract.contract_labor_items[-1].source, "Template")
		self.assertEqual(contract.has_out_of_tender_items, 1)


# ---------------------------------------------------------------------------
# Overhaul BRD (2026-10-18): Update / Delete / Add against the base contract
# ---------------------------------------------------------------------------

INVOICED = "contracting.contracting.doctype.contractor_contract_addendum.contractor_contract_addendum.get_invoiced_progress"


def make_amendable_contract(suffix, qty=5, with_conditions=False):
	"""An Approved Install Only contract whose single line (and its labor
	row) came through real propagation, so a rescale has something to
	rescale."""
	project, tender = make_resource_tender_and_project(suffix, qty=10)
	boq_row = tender.boq_items[0]
	contract = make_draft_contract(project, "Labor")
	add_tender_line(contract, boq_row, qty, BASE_UNIT_PRICE)
	if with_conditions:
		contract.append("payment_conditions", {"condition": "Advance", "percent": 100})
	contract.insert(ignore_permissions=True)
	submit_contract(contract)
	return contract, boq_row


def make_addendum(contract, rows, conditions=None):
	addendum = frappe.get_doc({
		"doctype": "Contractor Contract Addendum",
		"naming_series": "Subcontractor-ADDENDUM-.YYYY.-",
		"subcontractor_contract": contract.name,
	})
	for row in rows:
		addendum.append("addendum_items", row)
	for condition in conditions or []:
		addendum.append("addendum_payment_conditions", condition)
	return addendum


class TestAddendumAmendments(FrappeTestCase):
	def test_loader_returns_base_rows(self):
		from contracting.contracting.doctype.contractor_contract_addendum.contractor_contract_addendum import (
			get_contract_items_for_addendum,
		)
		contract, boq_row = make_amendable_contract("add_loader")
		rows = get_contract_items_for_addendum(contract.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["base_contract_item"], contract.contracted_items[0].name)
		self.assertEqual((rows[0]["old_qty"], rows[0]["old_unit_price"], rows[0]["invoiced_qty"]), (5, BASE_UNIT_PRICE, 0))
		self.assertEqual(get_contract_items_for_addendum(""), [])

	def test_update_changes_row_rescales_resources_and_stamps_effective_date(self):
		contract, boq_row = make_amendable_contract("add_update")
		base = contract.contracted_items[0].name
		self.assertEqual(contract.contract_labor_items[0].qty, 5)

		addendum = make_addendum(contract, [
			{"base_contract_item": base, "action": "Update", "new_qty": 8, "new_unit_price": 120},
		])
		addendum.insert(ignore_permissions=True)
		self.assertEqual((addendum.addendum_items[0].old_qty, addendum.addendum_items[0].qty_delta), (5, 3))
		self.assertAlmostEqual(addendum.grand_total_delta, 8 * 120 - 5 * BASE_UNIT_PRICE, places=6)

		contract.reload()
		self.assertEqual(contract.contracted_items[0].qty, 5)  # untouched until approval

		approve_addendum(addendum)
		contract.reload()
		self.assertEqual((contract.contracted_items[0].qty, contract.contracted_items[0].unit_price), (8, 120))
		self.assertEqual(contract.contract_labor_items[0].qty, 8)
		self.assertAlmostEqual(contract.grand_total, 960.0, places=6)
		self.assertEqual(str(addendum.effective_date), frappe.utils.today())

	def test_update_below_invoiced_blocked(self):
		contract, boq_row = make_amendable_contract("add_below_inv")
		addendum = make_addendum(contract, [
			{"base_contract_item": contract.contracted_items[0].name, "action": "Update", "new_qty": 3, "new_unit_price": 100},
		])
		with mock.patch(INVOICED, return_value={"qty": 4.0, "amount": 400.0, "draft_qty": 0.0}):
			self.assertRaises(frappe.ValidationError, addendum.insert, ignore_permissions=True)
		self.assertTrue(addendum.get("addendum_items")[0].invoiced_qty == 4.0)

	def test_update_without_change_blocked(self):
		contract, boq_row = make_amendable_contract("add_nochange")
		addendum = make_addendum(contract, [
			{"base_contract_item": contract.contracted_items[0].name, "action": "Update", "new_qty": 5,
			 "new_unit_price": BASE_UNIT_PRICE},
		])
		self.assertRaises(frappe.ValidationError, addendum.insert, ignore_permissions=True)

	def test_delete_removes_row_and_its_resources(self):
		contract, boq_row = make_amendable_contract("add_delete")
		addendum = make_addendum(contract, [
			{"base_contract_item": contract.contracted_items[0].name, "action": "Delete"},
		])
		addendum.insert(ignore_permissions=True)
		self.assertAlmostEqual(addendum.grand_total_delta, -5 * BASE_UNIT_PRICE, places=6)

		approve_addendum(addendum)
		contract.reload()
		self.assertEqual(len(contract.contracted_items), 0)
		self.assertEqual(len(contract.contract_labor_items), 0)
		self.assertEqual(contract.grand_total, 0)

	def test_delete_invoiced_row_blocked(self):
		contract, boq_row = make_amendable_contract("add_delete_inv")
		addendum = make_addendum(contract, [
			{"base_contract_item": contract.contracted_items[0].name, "action": "Delete"},
		])
		with mock.patch(INVOICED, return_value={"qty": 2.0, "amount": 200.0, "draft_qty": 0.0}):
			self.assertRaises(frappe.ValidationError, addendum.insert, ignore_permissions=True)

	def test_add_row_with_payment_conditions_must_total_100(self):
		contract, boq_row = make_amendable_contract("add_conditions", with_conditions=True)

		def with_conditions(*percents):
			return make_addendum(
				contract,
				[{"action": "Add", "work_item": boq_row.name, "qty_delta": 2, "unit_price": 150}],
				[{"work_item": boq_row.name, "condition": "Stage %s" % i, "percent": p} for i, p in enumerate(percents, 1)],
			)

		self.assertRaises(frappe.ValidationError, with_conditions(50, 40).insert, ignore_permissions=True)

		addendum = with_conditions(50, 50)
		addendum.insert(ignore_permissions=True)
		approve_addendum(addendum)
		contract.reload()
		self.assertEqual(len(contract.contracted_items), 2)
		self.assertEqual(contract.contracted_items[1].addendum, addendum.name)
		self.assertEqual(sorted(c.percent for c in contract.payment_conditions if c.work_item == boq_row.name), [50, 50])

	def test_conditions_rejected_when_contract_has_none(self):
		contract, boq_row = make_amendable_contract("add_no_conditions")
		addendum = make_addendum(
			contract, [{"action": "Add", "work_item": boq_row.name, "qty_delta": 1, "unit_price": 100}],
			[{"work_item": boq_row.name, "condition": "Stage", "percent": 100}],
		)
		self.assertRaises(frappe.ValidationError, addendum.insert, ignore_permissions=True)

	def test_second_addendum_on_same_row_blocked_while_first_in_flight(self):
		contract, boq_row = make_amendable_contract("add_concurrent")
		base = contract.contracted_items[0].name

		def update_to(qty):
			addendum = make_addendum(contract, [{"base_contract_item": base, "action": "Update", "new_qty": qty, "new_unit_price": 100}])
			addendum.insert(ignore_permissions=True)
			return addendum

		first, second = update_to(6), update_to(7)
		apply_workflow(first, "Submit for Approval")
		self.assertRaises(frappe.ValidationError, apply_workflow, second, "Submit for Approval")

	def test_closed_or_unapproved_contract_blocked(self):
		contract, boq_row = make_amendable_contract("add_closed")
		contract.db_set("is_closed", 1, update_modified=False)
		addendum = make_addendum(contract, [
			{"base_contract_item": contract.contracted_items[0].name, "action": "Update", "new_qty": 6, "new_unit_price": 100},
		])
		self.assertRaises(frappe.ValidationError, addendum.insert, ignore_permissions=True)

	def test_unchanged_loaded_rows_add_nothing_to_total_or_ceo_flag(self):
		contract, boq_row = make_amendable_contract("add_unchanged")
		base = contract.contracted_items[0].name
		addendum = make_addendum(contract, [
			{"base_contract_item": base},
			{"action": "Add", "work_item": boq_row.name, "qty_delta": 1, "unit_price": 50},
		])
		addendum.insert(ignore_permissions=True)
		self.assertAlmostEqual(addendum.grand_total_delta, 50.0, places=6)
		self.assertEqual(addendum.addendum_items[0].amount, 0)

	def test_checklist_and_effective_date_guards(self):
		contract, boq_row = make_amendable_contract("add_guards")
		addendum = make_addendum(contract, [
			{"base_contract_item": contract.contracted_items[0].name, "action": "Update", "new_qty": 6, "new_unit_price": 100},
		])
		addendum.insert(ignore_permissions=True)
		addendum.checklist_attachment = "/files/x.pdf"
		self.assertRaises(frappe.ValidationError, addendum.save)
