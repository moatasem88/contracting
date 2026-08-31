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
