# Copyright (c) 2026, kazem and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.doctype.tender.test_tender import ensure_customer, make_tender
from contracting.contracting.utils import allocation

TEST_SUBCONTRACTOR = "_Test Subcontractor Supplier"


def ensure_subcontractor():
	"""tax_id / custom_commercial_register / custom_bank_details_attachment
	are mandatory on Supplier site-wide (added outside this app) - not
	specific to this fixture, just unavoidable to create any Supplier here.
	"""
	if not frappe.db.exists("Supplier", TEST_SUBCONTRACTOR):
		frappe.get_doc({
			"doctype": "Supplier",
			"supplier_name": TEST_SUBCONTRACTOR,
			"supplier_group": frappe.db.get_value("Supplier Group", {}, "name"),
			"custom_is_subcontractor": 1,
			"tax_id": "TEST-TAX-ID",
			"custom_commercial_register": "TEST-CR-0001",
			"custom_bank_details_attachment": "/files/test-bank-details.pdf",
		}).insert(ignore_permissions=True)
	return TEST_SUBCONTRACTOR


def ensure_resource_item(item_code):
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc({
			"doctype": "Item", "item_code": item_code, "item_group": "Labor",
			"stock_uom": "Nos", "is_stock_item": 0,
		}).insert(ignore_permissions=True)
	return item_code


def make_resource_tender_and_project(suffix, qty=10):
	"""A Tender with one BOQ line plus a Labor/Equipment/Material row each
	against it, and a Project pointing straight at the Tender - the
	minimal shape a Subcontractor Contract needs (FR-11-19's material
	bottleneck extension needs a Material row, which
	test_project_cost_billing.make_won_project doesn't create). Skips the
	full Won-automation pipeline, which builds Sales Orders this doctype
	never reads.
	"""
	ensure_customer()
	tender = make_tender("_sc_" + suffix, qty=qty)
	boq_row = tender.boq_items[0]

	tender.append("labor_items", {
		"work_item": boq_row.idx, "item": ensure_resource_item("_Test SC Labor"),
		"qty_per_unit": 1, "rate": 50, "currency": "EGP",
	})
	tender.append("equipment_items", {
		"work_item": boq_row.idx, "item": ensure_resource_item("_Test SC Equipment"),
		"qty_per_unit": 1, "rate": 80, "currency": "EGP",
	})
	tender.append("material_items", {
		"work_item": boq_row.idx, "item": ensure_resource_item("_Test SC Material"),
		"qty_per_unit": 2, "rate": 30, "currency": "EGP",
	})
	tender.save(ignore_permissions=True)

	project = frappe.get_doc({
		"doctype": "Project",
		"project_name": "_SC Project " + suffix,
		"tender": tender.name,
	}).insert(ignore_permissions=True)

	return project, tender


def make_draft_contract(project, type_subcontractor):
	return frappe.get_doc({
		"doctype": "Subcontractor Contract",
		"naming_series": "Subcontractor-CONTRACT-.YYYY.-",
		"project": project.name,
		"contractor_name": ensure_subcontractor(),
		"type_subcontractor": type_subcontractor,
	})


def submit_contract(contract):
	"""allocation.py's _committed() only counts docstatus = 1 (FR-10) - a
	Draft's own rows never constrain anyone, including themselves."""
	contract.attached_contract = "/files/test-signed-contract.pdf"
	contract.submit()
	return contract


class TestSubcontractorContract(FrappeTestCase):
	def test_material_bottleneck_extension(self):
		"""FR-16-18: a Supply-and-Install contract's Material row is now a
		bottleneck constraint on a *second* Supply-and-Install contract's
		availability on the same work item - the same cross-contract
		reconciliation Labor/Equipment rows already did before this BRD.

		Material rows are only ever populated by Supply-and-Install
		contracts themselves, unlike Labor/Equipment (populated by the
		*other* contract types) - so this deliberately doesn't reuse
		get_supply_install_committed's own contribution in the check below;
		see allocation.py's get_supply_install_availability for why counting
		both would double-count the same contract's usage.
		"""
		project, tender = make_resource_tender_and_project("mat_bottleneck", qty=10)
		boq_row = tender.boq_items[0]
		material_row = tender.material_items[0]  # qty_per_unit = 2

		contract = make_draft_contract(project, "Supplying and installing")
		# Claims 6 of the item's 10 units, but declares 16 units of
		# material - 4 more than its own 6 x 2 = 12 proportional share
		# (e.g. a waste/buffer allowance), eating into the shared budget.
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 6, "unit_price": 100})
		contract.append("contract_material_items", {
			"work_item": boq_row.name, "material_item": material_row.name, "qty": 16,
		})
		contract.insert(ignore_permissions=True)
		submit_contract(contract)

		# Item-quantity pool alone would suggest 10 - 6 = 4 remain. But the
		# material row's full budget is 2 x 10 = 20, of which 16 is already
		# used - only 4 material units (2 item-equivalent units) are left,
		# which is the tighter, correctly-binding constraint.
		available = allocation.get_supply_install_availability(boq_row.name)
		self.assertAlmostEqual(available, 2.0, delta=1e-6)

	def test_payment_conditions_default_and_override_groups(self):
		"""FR-22/23: the contract-wide default group and each work item's
		override group must independently sum to 100%."""
		project, tender = make_resource_tender_and_project("pay_cond", qty=10)
		boq_row = tender.boq_items[0]

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 10, "unit_price": 100})
		contract.append("payment_conditions", {"condition": "Foundation", "percent": 30})
		contract.append("payment_conditions", {"condition": "Structure", "percent": 40})
		contract.append("payment_conditions", {"condition": "Finishing", "percent": 20})  # sums to 90

		with self.assertRaises(frappe.ValidationError):
			contract.insert(ignore_permissions=True)

		contract.payment_conditions[-1].percent = 30  # now sums to 100
		contract.append("payment_conditions", {
			"work_item": boq_row.name, "condition": "Override A", "percent": 70,
		})  # a lone override row - only 70%, should fail independently of the valid default group
		with self.assertRaises(frappe.ValidationError):
			contract.insert(ignore_permissions=True)

		contract.payment_conditions[-1].percent = 100
		contract.insert(ignore_permissions=True)  # both groups now valid
		self.assertEqual(len(contract.payment_conditions), 4)

	def test_cascade_delete_prunes_orphaned_resource_rows(self):
		"""FR-20: a Material/Labor/Equipment row whose work item is no
		longer in Contracted Items gets dropped server-side, guaranteeing
		the rule under API/Data Import paths the client JS can't reach."""
		project, tender = make_resource_tender_and_project("cascade", qty=10)
		boq_row = tender.boq_items[0]
		material_row = tender.material_items[0]

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 5, "unit_price": 100})
		contract.append("contract_material_items", {
			"work_item": boq_row.name, "material_item": material_row.name, "qty": 5,
		})
		contract.insert(ignore_permissions=True)
		self.assertEqual(len(contract.contract_material_items), 1)

		contract.set("contracted_items", [])
		contract.save(ignore_permissions=True)
		self.assertEqual(len(contract.contract_material_items), 0)

	# -- resource propagation (2026-08-30 BRD) -----------------------------

	def test_propagated_rows_installing_only_labor(self):
		"""FR-04/FR-06: Installing only ever propagates into the Labor table."""
		project, tender = make_resource_tender_and_project("prop_installing", qty=10)
		boq_row = tender.boq_items[0]
		labor_row = tender.labor_items[0]  # qty_per_unit = 1

		work_item_row = allocation.get_work_item(boq_row.name)
		result = allocation._propagated_resource_rows(work_item_row, 6, "Installing")

		self.assertEqual(len(result.get("contract_labor_items", [])), 1)
		self.assertNotIn("contract_material_items", result)
		self.assertNotIn("contract_equipment_items", result)
		row = result["contract_labor_items"][0]
		self.assertEqual(row["work_item"], boq_row.name)
		self.assertEqual(row["labor_item"], labor_row.name)
		self.assertAlmostEqual(row["qty"], 6.0, delta=1e-6)  # qty_per_unit(1) x contracted qty(6)

	def test_propagated_rows_supply_and_install_all_three_tables(self):
		"""FR-04: Supply and Install populates all three tables from one call."""
		project, tender = make_resource_tender_and_project("prop_sai", qty=10)
		boq_row = tender.boq_items[0]

		work_item_row = allocation.get_work_item(boq_row.name)
		result = allocation._propagated_resource_rows(work_item_row, 5, "Supplying and installing")

		self.assertEqual(len(result["contract_material_items"]), 1)
		self.assertEqual(len(result["contract_labor_items"]), 1)
		self.assertEqual(len(result["contract_equipment_items"]), 1)
		self.assertAlmostEqual(result["contract_material_items"][0]["qty"], 10.0, delta=1e-6)  # 2 x 5
		self.assertAlmostEqual(result["contract_labor_items"][0]["qty"], 5.0, delta=1e-6)       # 1 x 5
		self.assertAlmostEqual(result["contract_equipment_items"][0]["qty"], 5.0, delta=1e-6)   # 1 x 5

	def test_propagated_rows_equipment_only(self):
		"""FR-04: an Equipment contract only ever propagates into the Equipment table."""
		project, tender = make_resource_tender_and_project("prop_equipment", qty=10)
		boq_row = tender.boq_items[0]
		equipment_row = tender.equipment_items[0]

		work_item_row = allocation.get_work_item(boq_row.name)
		result = allocation._propagated_resource_rows(work_item_row, 4, "Equipment")

		self.assertNotIn("contract_material_items", result)
		self.assertNotIn("contract_labor_items", result)
		self.assertEqual(len(result["contract_equipment_items"]), 1)
		self.assertEqual(result["contract_equipment_items"][0]["equipment_item"], equipment_row.name)
		self.assertAlmostEqual(result["contract_equipment_items"][0]["qty"], 4.0, delta=1e-6)  # 1 x 4

	def test_propagated_rows_skip_zero_ratio(self):
		"""FR-08: a linked resource row with qty_per_unit <= TOLERANCE produces no propagated row."""
		project, tender = make_resource_tender_and_project("prop_zero_ratio", qty=10)
		boq_row = tender.boq_items[0]
		tender.append("labor_items", {
			"work_item": boq_row.idx, "item": ensure_resource_item("_Test SC Labor Zero"),
			"qty_per_unit": 0, "rate": 10, "currency": "EGP",
		})
		tender.save(ignore_permissions=True)
		boq_row = tender.boq_items[0]

		work_item_row = allocation.get_work_item(boq_row.name)
		result = allocation._propagated_resource_rows(work_item_row, 5, "Installing")

		# Only the original qty_per_unit=1 labor row propagates - the
		# zero-ratio one is skipped, not included at qty=0.
		self.assertEqual(len(result["contract_labor_items"]), 1)

	def test_propagated_rows_no_linked_resources(self):
		"""A work item with nothing linked on the Tender yields empty lists, not an error."""
		ensure_customer()
		tender = make_tender("_sc_prop_empty")
		boq_row = tender.boq_items[0]

		work_item_row = allocation.get_work_item(boq_row.name)
		result = allocation._propagated_resource_rows(work_item_row, 5, "Supplying and installing")

		self.assertEqual(result["contract_material_items"], [])
		self.assertEqual(result["contract_labor_items"], [])
		self.assertEqual(result["contract_equipment_items"], [])

	def test_propagated_over_budget_row_created_then_save_throws(self):
		"""FR-11: propagation doesn't cap/block at over-budget qty; the
		existing over-allocation guard at save time is what catches it,
		same as it already does for manually entered rows."""
		project, tender = make_resource_tender_and_project("prop_over_budget", qty=10)
		boq_row = tender.boq_items[0]

		work_item_row = allocation.get_work_item(boq_row.name)
		# The labor row's own budget is qty_per_unit(1) x item qty(10) = 10;
		# ask for far more than that - propagation still returns it in full.
		result = allocation._propagated_resource_rows(work_item_row, 50, "Installing")
		self.assertAlmostEqual(result["contract_labor_items"][0]["qty"], 50.0, delta=1e-6)

		contract = make_draft_contract(project, "Installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 10, "unit_price": 100})
		contract.append("contract_labor_items", result["contract_labor_items"][0])
		with self.assertRaises(frappe.ValidationError):
			contract.insert(ignore_permissions=True)

	def test_item_level_availability_for_installing_without_named_resource(self):
		"""2026-08-30 fix: get_row_availability must not hard-return 0 for
		Installing/Equipment when no specific resource is named yet (the
		unified dialog, and an Addendum item post FR-16, both only know the
		work item at this point) - it falls back to the item's own
		remaining quantity instead."""
		project, tender = make_resource_tender_and_project("item_level_avail", qty=10)
		boq_row = tender.boq_items[0]

		self.assertAlmostEqual(
			allocation.get_row_availability("Installing", boq_row.name), 10.0, delta=1e-6,
		)
		self.assertAlmostEqual(
			allocation.get_row_availability("Equipment", boq_row.name), 10.0, delta=1e-6,
		)

		# Committing some of the item as Supply and Install draws down the
		# same pool this falls back to.
		sai_contract = make_draft_contract(project, "Supplying and installing")
		sai_contract.append("contracted_items", {"work_item": boq_row.name, "qty": 4, "unit_price": 100})
		sai_contract.insert(ignore_permissions=True)
		submit_contract(sai_contract)

		self.assertAlmostEqual(
			allocation.get_row_availability("Installing", boq_row.name), 6.0, delta=1e-6,
		)

	# -- price ceiling / approval-chain rebuild (2026-09-27 BRD) -----------

	def test_price_under_ceiling_saves_cleanly(self):
		"""TC-01: a Total-mode price below the work item's Direct Cost per
		unit needs no delegation and doesn't trigger CEO approval."""
		project, tender = make_resource_tender_and_project("ceiling_under", qty=10)
		boq_row = tender.boq_items[0]
		ceiling = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity
		self.assertGreater(ceiling, 100)  # sanity: fixture's rates put the ceiling well above 100

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 10, "unit_price": 100})
		contract.insert(ignore_permissions=True)

		self.assertEqual(contract.requires_ceo_approval, 0)

	def test_price_over_ceiling_blocks_without_delegation(self):
		"""TC-02: a price above the ceiling is blocked, naming the row but
		never the Tender's own figures."""
		project, tender = make_resource_tender_and_project("ceiling_over", qty=10)
		boq_row = tender.boq_items[0]
		ceiling = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {
			"work_item": boq_row.name, "qty": 10, "unit_price": ceiling * 2,
		})
		with self.assertRaises(frappe.ValidationError) as ctx:
			contract.insert(ignore_permissions=True)
		self.assertNotIn(str(round(ceiling, 2)), str(ctx.exception))

	def test_delegate_to_cost_control_allows_save_and_forces_ceo(self):
		"""TC-03: acknowledging the exception lets the over-ceiling price
		through and forces requires_ceo_approval regardless of amount."""
		project, tender = make_resource_tender_and_project("ceiling_delegate", qty=10)
		boq_row = tender.boq_items[0]
		ceiling = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {
			"work_item": boq_row.name, "qty": 10, "unit_price": ceiling * 2,
		})
		contract.price_exception_acknowledged = 1
		contract.insert(ignore_permissions=True)

		self.assertEqual(contract.requires_ceo_approval, 1)

	def test_components_mode_computes_unit_price_from_resource_rows(self):
		"""TC-04: Components mode's unit_price is a pure rollup of the
		matching Material/Labor/Equipment rows, never independently set."""
		project, tender = make_resource_tender_and_project("components", qty=10)
		boq_row = tender.boq_items[0]
		labor_row, equipment_row, material_row = (
			tender.labor_items[0], tender.equipment_items[0], tender.material_items[0]
		)

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {
			"work_item": boq_row.name, "qty": 10, "pricing_mode": "Components", "unit_price": 0,
		})
		contract.append("contract_labor_items", {
			"work_item": boq_row.name, "labor_item": labor_row.name, "qty": 10, "rate": 3,
		})
		contract.append("contract_equipment_items", {
			"work_item": boq_row.name, "equipment_item": equipment_row.name, "qty": 10, "rate": 2,
		})
		contract.append("contract_material_items", {
			"work_item": boq_row.name, "material_item": material_row.name, "qty": 10, "rate": 5,
		})
		contract.insert(ignore_permissions=True)

		# (10x3 + 10x2 + 10x5) / 10 = 10
		self.assertAlmostEqual(contract.contracted_items[0].unit_price, 10.0, delta=1e-6)

	def test_components_mode_zero_rows_blocked(self):
		"""TC-06: Components mode with no priced component rows is blocked
		rather than silently pricing the row at zero (FR-14)."""
		project, tender = make_resource_tender_and_project("components_empty", qty=10)
		boq_row = tender.boq_items[0]

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {
			"work_item": boq_row.name, "qty": 10, "pricing_mode": "Components", "unit_price": 0,
		})
		with self.assertRaises(frappe.ValidationError):
			contract.insert(ignore_permissions=True)

	def test_components_to_total_switch_retains_component_rates(self):
		"""TC-07: switching a row back to Total leaves the previously
		entered component rate/amount values in place, just inert."""
		project, tender = make_resource_tender_and_project("components_switch", qty=10)
		boq_row = tender.boq_items[0]
		labor_row = tender.labor_items[0]

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {
			"work_item": boq_row.name, "qty": 10, "pricing_mode": "Components", "unit_price": 0,
		})
		contract.append("contract_labor_items", {
			"work_item": boq_row.name, "labor_item": labor_row.name, "qty": 10, "rate": 3,
		})
		contract.insert(ignore_permissions=True)

		contract.contracted_items[0].pricing_mode = "Total"
		contract.contracted_items[0].unit_price = 5
		contract.save(ignore_permissions=True)

		self.assertEqual(contract.contract_labor_items[0].rate, 3)
		self.assertAlmostEqual(contract.contracted_items[0].unit_price, 5.0, delta=1e-6)

	def test_ceo_approval_fires_on_first_250k_work_item(self):
		"""TC-08: a work item reaching 250,000+ that has never been on
		another Approved contract requires CEO approval."""
		project, tender = make_resource_tender_and_project("ceo_first", qty=3000)
		boq_row = tender.boq_items[0]
		ceiling = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {
			"work_item": boq_row.name, "qty": 2000, "unit_price": ceiling * 0.9,
		})
		contract.insert(ignore_permissions=True)

		self.assertGreaterEqual(contract.contracted_items[0].amount, 250000)
		self.assertEqual(contract.requires_ceo_approval, 1)

	def test_ceo_approval_does_not_refire_for_already_approved_work_item(self):
		"""TC-09: once a work item has reached Approved on one contract, a
		later contract reusing it does not require CEO approval again."""
		project, tender = make_resource_tender_and_project("ceo_no_refire", qty=6000)
		boq_row = tender.boq_items[0]
		ceiling = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity
		rate = ceiling * 0.9

		first = make_draft_contract(project, "Supplying and installing")
		first.append("contracted_items", {"work_item": boq_row.name, "qty": 2000, "unit_price": rate})
		first.insert(ignore_permissions=True)
		first.db_set("workflow_state", "Approved", update_modified=False)

		second = make_draft_contract(project, "Supplying and installing")
		second.append("contracted_items", {"work_item": boq_row.name, "qty": 2000, "unit_price": rate})
		second.insert(ignore_permissions=True)

		self.assertEqual(second.requires_ceo_approval, 0)

	def test_ceo_approval_rejected_prior_contract_does_not_count(self):
		"""TC-10: a prior Rejected contract for the same work item doesn't
		count as 'already approved' - the rule still fires."""
		project, tender = make_resource_tender_and_project("ceo_rejected_prior", qty=6000)
		boq_row = tender.boq_items[0]
		ceiling = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity
		rate = ceiling * 0.9

		first = make_draft_contract(project, "Supplying and installing")
		first.append("contracted_items", {"work_item": boq_row.name, "qty": 2000, "unit_price": rate})
		first.insert(ignore_permissions=True)
		first.db_set("workflow_state", "Rejected", update_modified=False)

		second = make_draft_contract(project, "Supplying and installing")
		second.append("contracted_items", {"work_item": boq_row.name, "qty": 2000, "unit_price": rate})
		second.insert(ignore_permissions=True)

		self.assertEqual(second.requires_ceo_approval, 1)

	def test_ceo_approval_aggregates_rows_on_same_work_item(self):
		"""TC-11: two Contracted Items rows against the same work item are
		summed before comparing to the 250,000 threshold."""
		project, tender = make_resource_tender_and_project("ceo_aggregate", qty=3000)
		boq_row = tender.boq_items[0]
		ceiling = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity
		rate = ceiling * 0.9

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 1000, "unit_price": rate})
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 1000, "unit_price": rate})
		contract.insert(ignore_permissions=True)

		total = sum(flt(r.amount) for r in contract.contracted_items)
		self.assertGreaterEqual(total, 250000)
		self.assertEqual(contract.requires_ceo_approval, 1)

	def test_old_grand_total_threshold_no_longer_triggers_ceo(self):
		"""TC-12: crossing the old Contracting Settings.ceo_approval_value_
		threshold, on its own, no longer requires CEO approval - only the
		per-work-item rules (FR-07) do."""
		frappe.db.set_value("Contracting Settings", None, "ceo_approval_value_threshold", 500)

		project, tender = make_resource_tender_and_project("old_threshold_retired", qty=10)
		boq_row = tender.boq_items[0]
		ceiling = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity
		rate = min(100, ceiling * 0.5)

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 10, "unit_price": rate})
		contract.insert(ignore_permissions=True)

		self.assertGreater(contract.grand_total, 500)  # crosses the old threshold
		self.assertLess(contract.contracted_items[0].amount, 250000)  # but not the new one
		self.assertEqual(contract.requires_ceo_approval, 0)

	def test_checklist_gate_blocks_approve_without_attachment(self):
		"""TC-13: the Cost Control Engineer Review -> Approve transition is
		unavailable without checklist_attachment set, exercised through the
		real workflow engine (not just the field's client-side depends_on)."""
		from frappe.model.workflow import get_transitions

		project, tender = make_resource_tender_and_project("checklist_gate", qty=10)
		boq_row = tender.boq_items[0]

		contract = make_draft_contract(project, "Supplying and installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 10, "unit_price": 1})
		contract.insert(ignore_permissions=True)
		contract.db_set("workflow_state", "Pending Cost Control Engineer Review", update_modified=False)
		contract.reload()

		# get_transitions() matches on frappe.get_roles() directly, not on
		# a real logged-in user's assignments - patch it in place rather
		# than creating and assigning a throwaway test user (site1.local's
		# mandatory User/Employee fields make that fixture heavier than the
		# thing being tested here). check_permission("read") inside
		# get_transitions still passes: it's gated on frappe.session.user,
		# which stays Administrator and is unaffected by this patch.
		original_get_roles = frappe.get_roles
		frappe.get_roles = lambda *a, **kw: ["Cost Control Engineer"]
		try:
			transitions = get_transitions(contract)
			actions = {t["action"] for t in transitions}
			self.assertNotIn("Approve", actions)
			self.assertIn("Reject", actions)

			contract.checklist_attachment = "/files/test-checklist.pdf"
			transitions = get_transitions(contract)
			actions = {t["action"] for t in transitions}
			self.assertIn("Approve", actions)
		finally:
			frappe.get_roles = original_get_roles


# ---------------------------------------------------------------------------
# Follows Tender BRD (2026-10-13)
# ---------------------------------------------------------------------------

def ensure_stock_item(item_code):
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc({
			"doctype": "Item", "item_code": item_code,
			"item_group": frappe.db.get_value(
				"Item Group", {"is_group": 0, "name": ["not in", ["Labor", "Equipment"]]}, "name"
			),
			"stock_uom": "Nos", "is_stock_item": 1,
		}).insert(ignore_permissions=True)
	return item_code


def make_v2_template(name, labor_rate=50, material_qty=2, material_rate=30):
	"""BRD fixture T: labor 1 x 50 + material 2 x 30 per unit = 110."""
	if frappe.db.exists("Work Item Template V2", name):
		return frappe.get_doc("Work Item Template V2", name)
	return frappe.get_doc({
		"doctype": "Work Item Template V2",
		"template_name": name,
		"uom": "Nos",
		"description": name + " description",
		"labor_items": [{"item": ensure_resource_item("_Test SC Labor"), "qty_per_unit": 1, "rate": labor_rate}],
		"material_items": [{
			"item": ensure_stock_item("_Test SC Stock Material"), "qty_per_unit": material_qty, "rate": material_rate,
		}],
	}).insert(ignore_permissions=True)


def make_no_tender_project(suffix):
	return frappe.get_doc({"doctype": "Project", "project_name": "_SC NoTender " + suffix}).insert(
		ignore_permissions=True
	)


def add_template_line(contract, template, qty, unit_price=100, **extra):
	"""What the "Select Work Item Template" dialog does client-side."""
	contract.append("contracted_items", {
		"work_item_template": template, "qty": qty, "unit_price": unit_price, **extra,
	})
	for fieldname, rows in allocation._template_resource_rows(template, qty, contract.type_subcontractor).items():
		for row in rows:
			contract.append(fieldname, row)


def add_tender_line(contract, boq_row, qty, unit_price):
	"""What the "Select Work Item" dialog does client-side."""
	contract.append("contracted_items", {"work_item": boq_row.name, "qty": qty, "unit_price": unit_price})
	for fieldname, rows in allocation.get_propagated_resource_rows(
		boq_row.name, qty, contract.type_subcontractor
	).items():
		for row in rows:
			contract.append(fieldname, row)


class TestFollowsTender(FrappeTestCase):
	def setUp(self):
		self.template = make_v2_template("_SC FT Template").name

	def test_default_and_no_tender_project_forces_template_mode(self):
		"""TC-01/02: default 1; a project with no Tender saves (no throw)
		with follows_tender forced to 0."""
		self.assertEqual(frappe.get_meta("Subcontractor Contract").get_field("follows_tender").default, "1")
		contract = make_draft_contract(make_no_tender_project("tc02"), "Installing")
		contract.insert(ignore_permissions=True)
		self.assertEqual(contract.follows_tender, 0)
		self.assertEqual(contract.project_tenders, [])

	def test_template_line_supply_and_install(self):
		"""TC-03: template resources propagate into Material and Labor;
		nothing is red on a no-Tender project. Rates are *not* copied from
		the template (2026-10-13: the contracts department must not see
		template/Tender rates) - only the permlevel-1 tender_rate carries it."""
		contract = make_draft_contract(make_no_tender_project("tc03"), "Supplying and installing")
		add_template_line(contract, self.template, 5)
		contract.insert(ignore_permissions=True)

		line = contract.contracted_items[0]
		self.assertEqual((line.work_item_template, line.follows_tender), (self.template, 0))
		self.assertEqual(line.description, "_SC FT Template description")
		labor, material = contract.contract_labor_items[0], contract.contract_material_items[0]
		self.assertEqual((flt(labor.qty), flt(labor.rate), labor.source), (5.0, 0.0, "Template"))
		self.assertEqual((flt(material.qty), flt(material.rate), material.source), (10.0, 0.0, "Template"))
		self.assertEqual((flt(labor.tender_rate), flt(material.tender_rate)), (50.0, 30.0))
		self.assertEqual(contract.has_out_of_tender_items, 0)
		self.assertFalse(any(r.out_of_tender_scope for r in contract.contract_labor_items + contract.contract_material_items))

	def test_template_line_installing_propagates_labor_only(self):
		"""TC-04."""
		contract = make_draft_contract(make_no_tender_project("tc04"), "Installing")
		add_template_line(contract, self.template, 5)
		contract.insert(ignore_permissions=True)
		self.assertEqual(len(contract.contract_labor_items), 1)
		self.assertEqual(contract.contract_material_items, [])
		self.assertEqual(contract.contract_equipment_items, [])

	def test_duplicate_template_rejected(self):
		"""TC-06."""
		contract = make_draft_contract(make_no_tender_project("tc06"), "Installing")
		add_template_line(contract, self.template, 5)
		contract.insert(ignore_permissions=True)
		contract.append("contracted_items", {"work_item_template": self.template, "qty": 1, "unit_price": 1})
		with self.assertRaisesRegex(frappe.ValidationError, "already on row 1"):
			contract.save(ignore_permissions=True)

	def test_extra_row_and_item_filter(self):
		"""TC-07/08: an extra stock-item Material row saves as Extra (not red
		on a no-Tender project); a Labor-tab row with a non-Labor item is
		rejected server-side."""
		contract = make_draft_contract(make_no_tender_project("tc07"), "Supplying and installing")
		add_template_line(contract, self.template, 5)
		contract.insert(ignore_permissions=True)

		contract.append("contract_material_items", {
			"work_item_template": self.template, "item": ensure_stock_item("_Test SC Stock Material"),
			"qty": 1, "rate": 5,
		})
		contract.save(ignore_permissions=True)
		extra = contract.contract_material_items[-1]
		self.assertEqual((extra.source, extra.out_of_tender_scope), ("Extra", 0))

		contract.append("contract_labor_items", {
			"work_item_template": self.template, "item": ensure_stock_item("_Test SC Stock Material"), "qty": 1,
		})
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed on this tab"):
			contract.save(ignore_permissions=True)

	def test_extra_row_on_non_line_rejected_not_pruned(self):
		"""FR-08: an extra row pointing at a template that isn't a line is an
		error, not a silent prune."""
		contract = make_draft_contract(make_no_tender_project("nonline"), "Installing")
		add_template_line(contract, self.template, 1)
		contract.insert(ignore_permissions=True)
		other = make_v2_template("_SC FT Other").name
		contract.append("contract_labor_items", {
			"work_item_template": other, "item": ensure_resource_item("_Test SC Labor"), "qty": 1,
		})
		with self.assertRaisesRegex(frappe.ValidationError, "is not a line on Contracted Items"):
			contract.save(ignore_permissions=True)

	def test_untick_on_tender_project_template_line_is_red(self):
		"""TC-09: Tender project, switch unticked, template line -> red,
		headline flag, CEO."""
		project, tender = make_resource_tender_and_project("ft_tc09")
		contract = make_draft_contract(project, "Installing")
		contract.follows_tender = 0
		add_template_line(contract, self.template, 2)
		contract.insert(ignore_permissions=True)
		self.assertEqual(contract.follows_tender, 0)
		self.assertEqual(contract.contracted_items[0].out_of_tender_scope, 1)
		self.assertEqual(contract.contract_labor_items[0].out_of_tender_scope, 1)
		self.assertEqual((contract.has_out_of_tender_items, contract.requires_ceo_approval), (1, 1))

	def test_switch_locked_while_lines_exist(self):
		"""TC-10."""
		project, tender = make_resource_tender_and_project("ft_tc10")
		contract = make_draft_contract(project, "Installing")
		add_tender_line(contract, tender.boq_items[0], 1, 1)
		contract.insert(ignore_permissions=True)
		contract.follows_tender = 0
		with self.assertRaisesRegex(frappe.ValidationError, "Remove all Contracted Items"):
			contract.save(ignore_permissions=True)

	def test_per_line_override_extra_row_and_rate(self):
		"""TC-11/12/13/20: Tender line W in scope; template line and its rows
		red; an extra row on W red, W itself not; a Tender row's rate above
		the Tender rate red, cleared when lowered back. Tender reference
		figures filled for Tender and template rows, blank on extra rows."""
		project, tender = make_resource_tender_and_project("ft_tc11")
		boq_row = tender.boq_items[0]
		unit_cost = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity

		contract = make_draft_contract(project, "Installing")
		add_tender_line(contract, boq_row, 1, unit_cost)
		add_template_line(contract, self.template, 2)
		contract.insert(ignore_permissions=True)

		w, t = contract.contracted_items
		self.assertEqual((w.out_of_tender_scope, t.out_of_tender_scope), (0, 1))
		self.assertAlmostEqual(flt(w.tender_unit_cost), unit_cost, delta=1e-6)
		self.assertAlmostEqual(flt(t.tender_unit_cost), 110.0, delta=1e-6)
		tender_row = next(r for r in contract.contract_labor_items if r.source == "Tender")
		template_row = next(r for r in contract.contract_labor_items if r.source == "Template")
		self.assertEqual((tender_row.out_of_tender_scope, template_row.out_of_tender_scope), (0, 1))
		self.assertAlmostEqual(flt(tender_row.tender_rate), 50.0, delta=1e-6)
		self.assertAlmostEqual(flt(template_row.tender_rate), 50.0, delta=1e-6)

		contract.append("contract_labor_items", {
			"work_item": boq_row.name, "item": ensure_resource_item("_Test SC Labor"), "qty": 1, "rate": 1,
		})
		contract.save(ignore_permissions=True)
		extra = contract.contract_labor_items[-1]
		self.assertEqual((extra.source, extra.out_of_tender_scope, extra.tender_rate), ("Extra", 1, None))
		self.assertEqual(contract.contracted_items[0].out_of_tender_scope, 0)

		tender_row = next(r for r in contract.contract_labor_items if r.source == "Tender")
		tender_row.rate = 55
		contract.save(ignore_permissions=True)
		self.assertEqual(next(r for r in contract.contract_labor_items if r.source == "Tender").out_of_tender_scope, 1)
		next(r for r in contract.contract_labor_items if r.source == "Tender").rate = 50
		contract.save(ignore_permissions=True)
		self.assertEqual(next(r for r in contract.contract_labor_items if r.source == "Tender").out_of_tender_scope, 0)

	def test_delegated_price_over_ceiling_is_red(self):
		"""TC-14."""
		project, tender = make_resource_tender_and_project("ft_tc14")
		boq_row = tender.boq_items[0]
		unit_cost = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity
		contract = make_draft_contract(project, "Installing")
		add_tender_line(contract, boq_row, 1, unit_cost * 1.2)
		with self.assertRaisesRegex(frappe.ValidationError, "Delegate to Cost Control"):
			contract.insert(ignore_permissions=True)
		contract.price_exception_acknowledged = 1
		contract.insert(ignore_permissions=True)
		self.assertEqual(contract.contracted_items[0].out_of_tender_scope, 1)

	def test_tender_qty_overrun_still_blocked(self):
		"""TC-15 regression: availability on Tender lines unchanged."""
		project, tender = make_resource_tender_and_project("ft_tc15", qty=10)
		contract = make_draft_contract(project, "Supplying and installing")
		add_tender_line(contract, tender.boq_items[0], 11, 1)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot allocate"):
			contract.insert(ignore_permissions=True)

	def test_template_line_skips_ceiling_and_availability(self):
		"""TC-16."""
		project, tender = make_resource_tender_and_project("ft_tc16")
		contract = make_draft_contract(project, "Supplying and installing")
		add_template_line(contract, self.template, 1000, unit_price=10 ** 5)
		contract.insert(ignore_permissions=True)
		self.assertEqual(contract.contracted_items[0].out_of_tender_scope, 1)

	def test_template_threshold_applies_per_template(self):
		"""OQ-4: a template line >= 250,000 on a no-Tender project escalates
		through the per-line threshold even though nothing is red."""
		contract = make_draft_contract(make_no_tender_project("oq4"), "Installing")
		add_template_line(contract, self.template, 1000, unit_price=300)
		contract.insert(ignore_permissions=True)
		self.assertEqual(contract.has_out_of_tender_items, 0)
		self.assertEqual(contract.requires_ceo_approval, 1)

	def test_components_mode_keeps_template_lines_separate(self):
		"""Regression (None == None): two template lines in Components mode
		each price from their own rows only."""
		other = make_v2_template("_SC FT Components B", labor_rate=10, material_rate=1).name
		contract = make_draft_contract(make_no_tender_project("comp"), "Installing")
		add_template_line(contract, self.template, 1, pricing_mode="Components")
		add_template_line(contract, other, 1, pricing_mode="Components")
		for row in contract.contract_labor_items:
			row.rate = 50 if row.work_item_template == self.template else 10
		contract.insert(ignore_permissions=True)
		self.assertAlmostEqual(flt(contract.contracted_items[0].unit_price), 50.0, delta=1e-6)
		self.assertAlmostEqual(flt(contract.contracted_items[1].unit_price), 10.0, delta=1e-6)

	def test_deleting_one_template_line_prunes_only_its_rows(self):
		"""Regression (None == None) for FR-20 cascade-delete."""
		other = make_v2_template("_SC FT Prune B").name
		contract = make_draft_contract(make_no_tender_project("prune"), "Installing")
		add_template_line(contract, self.template, 1)
		add_template_line(contract, other, 1)
		contract.insert(ignore_permissions=True)
		self.assertEqual(len(contract.contract_labor_items), 2)

		contract.contracted_items = [r for r in contract.contracted_items if r.work_item_template == other]
		contract.save(ignore_permissions=True)
		self.assertEqual([r.work_item_template for r in contract.contract_labor_items], [other])

	def test_template_payment_condition_group_and_seed(self):
		"""Regression + TC-23: a template-scoped condition is its own 100%
		group and only seeds onto its own line."""
		from contracting.contracting.utils.progress_invoicing import get_condition_seed_rows

		project, tender = make_resource_tender_and_project("ft_pc")
		boq_row = tender.boq_items[0]
		unit_cost = allocation.get_work_item(boq_row.name).boq_direct_cost / boq_row.original_quantity
		contract = make_draft_contract(project, "Installing")
		add_tender_line(contract, boq_row, 1, unit_cost)
		add_template_line(contract, self.template, 1)
		contract.append("payment_conditions", {"condition": "Default", "percent": 100})
		contract.append("payment_conditions", {"work_item_template": self.template, "condition": "Tpl", "percent": 50})
		with self.assertRaisesRegex(frappe.ValidationError, "work item template"):
			contract.insert(ignore_permissions=True)

		contract.payment_conditions[-1].percent = 100
		contract.insert(ignore_permissions=True)
		w, t = contract.contracted_items
		default_pc, template_pc = contract.payment_conditions
		seeded = {(r.contract_item, r.payment_condition) for r in get_condition_seed_rows(contract.name)}
		self.assertEqual(seeded, {(w.name, default_pc.name), (t.name, template_pc.name)})

	def test_tender_cost_hidden_from_contracts_engineer(self):
		"""TC-21: permlevel-1 values never reach a Contracts Engineer through
		frappe.client.get. That path only strips permlevel fields when
		apply_perm_level_on_api_calls is on (off on site1.local), so this
		drives the request-scoped guard the way a real request would."""
		from contracting.contracting.api import permlevel_guard

		project, tender = make_resource_tender_and_project("ft_tc21")
		contract = make_draft_contract(project, "Installing")
		add_tender_line(contract, tender.boq_items[0], 1, 1)
		contract.insert(ignore_permissions=True)

		user = "_test_sc_contracts_engineer@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({
				"doctype": "User", "email": user, "first_name": "SC Engineer", "send_welcome_email": 0,
				"roles": [{"role": "Contracts Engineer"}],
			}).insert(ignore_permissions=True)

		self.assertIn(
			"contracting.contracting.api.permlevel_guard.apply_permlevel_on_guarded_reads",
			frappe.get_hooks("before_request"),
		)
		real_cost = flt(contract.contracted_items[0].tender_unit_cost)
		self.assertGreater(real_cost, 0)

		original_request = getattr(frappe.local, "request", None)
		original_form_dict = frappe.local.form_dict
		frappe.set_user(user)
		# After set_user: it resets frappe.local.form_dict. A real request
		# builds form_dict and resolves the session before before_request.
		frappe.local.request = frappe._dict(path="/api/method/frappe.client.get")
		frappe.local.form_dict = frappe._dict(doctype="Subcontractor Contract", name=contract.name)
		try:
			permlevel_guard.apply_permlevel_on_guarded_reads()
			payload = frappe.client.get("Subcontractor Contract", contract.name)
		finally:
			frappe.set_user("Administrator")
			frappe.local.request = original_request
			frappe.local.form_dict = original_form_dict
			if hasattr(frappe.local, "system_settings"):
				del frappe.local.system_settings
		self.assertNotEqual(flt(payload["contracted_items"][0].get("tender_unit_cost")), real_cost)
		self.assertFalse(flt(payload["contract_labor_items"][0].get("tender_rate")))

	def test_contracts_roles_cannot_read_templates_or_tender(self):
		"""2026-10-13: the contracts department may pick templates and Tender
		work items but never open them (their rates/costs live there)."""
		user = "_test_sc_contracts_manager@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({
				"doctype": "User", "email": user, "first_name": "SC Manager", "send_welcome_email": 0,
				"roles": [{"role": "Contracts Manager"}],
			}).insert(ignore_permissions=True)
		frappe.set_user(user)
		try:
			self.assertFalse(frappe.has_permission("Work Item Template V2", "read"))
			self.assertTrue(frappe.has_permission("Work Item Template V2", "select"))
			rows = allocation.get_template_resource_rows(self.template, 1, "Supplying and installing")
		finally:
			frappe.set_user("Administrator")
		for table_rows in rows.values():
			for row in table_rows:
				self.assertNotIn("rate", row)
				self.assertNotIn("tender_rate", row)

	def test_frozen_contract_not_recomputed(self):
		"""TC-26 / NFR-02: outside Draft, the flags and Tender figures stay put."""
		# Own template: this test edits it, and FrappeTestCase only rolls
		# back per class, not per test.
		template = make_v2_template("_SC FT Frozen").name
		project, tender = make_resource_tender_and_project("ft_tc26")
		contract = make_draft_contract(project, "Installing")
		add_template_line(contract, template, 1)
		contract.insert(ignore_permissions=True)
		frappe.db.set_value("Subcontractor Contract", contract.name, "workflow_state", "Pending Cost Control Approval")
		frappe.db.set_value("Work Item Template V2", template, "total_cost_per_unit", 999)
		contract.reload()
		contract.save(ignore_permissions=True)
		self.assertAlmostEqual(flt(contract.contracted_items[0].tender_unit_cost), 110.0, delta=1e-6)
		self.assertEqual(contract.has_out_of_tender_items, 1)

	def test_contractor_invoice_resolves_template_directly(self):
		"""FR-16: _resolve_work_item_template reads the line's own template."""
		from contracting.contracting.doctype.contractor_invoice.contractor_invoice import _resolve_work_item_template

		contract = make_draft_contract(make_no_tender_project("ci"), "Installing")
		add_template_line(contract, self.template, 1)
		contract.insert(ignore_permissions=True)
		self.assertEqual(_resolve_work_item_template(contract.contracted_items[0].name), self.template)
