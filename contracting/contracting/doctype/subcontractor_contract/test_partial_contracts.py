# Copyright (c) 2026, kazem and Contributors
# See license.txt

"""2026-10-20: partial contracts - a contract claims exactly the Tender lines
it carries, so a work item's lines can be split across contracts, or added
to the original contract later through an Addendum.

Fixture work item W (qty 10), built from its template through the real
apply path:
    Subcontract A 1 x 30,  B 2 x 20,  C 1 x 10
    Labor 1 x 50,  Material 1 x 100,  Equipment 1 x 20
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.doctype.subcontractor_contract.subcontractor_contract import (
	get_unallocated_boq_items,
)
from contracting.contracting.doctype.subcontractor_contract.test_subcontract_resource import (
	SUBCONTRACT_GROUP,
	ensure_item,
)
from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	ensure_resource_item,
	ensure_stock_item,
	make_draft_contract,
	submit_contract,
)
from contracting.contracting.doctype.tender.test_tender import ensure_customer, make_tender
from contracting.contracting.doctype.tender.tender import add_boq_item_from_template
from contracting.contracting.utils import allocation

SUB_A, SUB_B, SUB_C = "_Test PC Sub A", "_Test PC Sub B", "_Test PC Sub C"


def make_split_template(name="_PC Template"):
	if frappe.db.exists("Work Item Template V2", name):
		return frappe.get_doc("Work Item Template V2", name)
	return frappe.get_doc({
		"doctype": "Work Item Template V2",
		"template_name": name,
		"uom": "Nos",
		"description": name + " description",
		"material_items": [{"item": ensure_stock_item("_Test SC Stock Material"), "qty_per_unit": 1, "rate": 100}],
		"labor_items": [{"item": ensure_resource_item("_Test SC Labor"), "qty_per_unit": 1, "rate": 50}],
		"subcontract_items": [
			{"item": ensure_item(SUB_A, SUBCONTRACT_GROUP), "qty_per_unit": 1, "rate": 30},
			{"item": ensure_item(SUB_B, SUBCONTRACT_GROUP), "qty_per_unit": 2, "rate": 20},
			{"item": ensure_item(SUB_C, SUBCONTRACT_GROUP), "qty_per_unit": 1, "rate": 10},
		],
		"equipment_items": [{"item": ensure_item("_Test SC Equipment Item", "Equipment"), "qty_per_unit": 1, "rate": 20}],
	}).insert(ignore_permissions=True)


def make_split_tender(suffix, qty=10):
	ensure_customer()
	make_split_template()
	tender = make_tender("_pc_" + suffix)
	add_boq_item_from_template(tender.name, "_PC Template", qty)
	tender.reload()
	project = frappe.get_doc({
		"doctype": "Project", "project_name": "_PC Project " + suffix, "tender": tender.name,
	}).insert(ignore_permissions=True)
	boq_row = tender.boq_items[-1]
	lines = {}
	for fieldname in ("material_items", "labor_items", "subcontract_items", "equipment_items"):
		for row in tender.get(fieldname):
			if row.work_item == boq_row.idx:
				lines[row.item] = row.name
	return project, tender, boq_row, lines


def add_line(contract, boq_row, qty, unit_price, lines):
	"""What the Select Work Item dialog does with only some lines ticked."""
	contract.append("contracted_items", {"work_item": boq_row.name, "qty": qty, "unit_price": unit_price})
	for fieldname, rows in allocation.get_propagated_resource_rows(
		boq_row.name, qty, contract.type_subcontractor, lines=json.dumps(lines)
	).items():
		for row in rows:
			contract.append(fieldname, row)


def addendum_for(contract, rows):
	doc = frappe.get_doc({
		"doctype": "Contractor Contract Addendum",
		"naming_series": "Subcontractor-ADDENDUM-.YYYY.-",
		"subcontractor_contract": contract.name,
	})
	for row in rows:
		doc.append("addendum_items", row)
	return doc


class TestPartialContracts(FrappeTestCase):
	def test_two_installing_contracts_split_subcontract_lines(self):
		project, _tender, boq_row, lines = make_split_tender("split")
		first = make_draft_contract(project, "Installing")
		add_line(first, boq_row, 10, 30, [lines[SUB_A]])
		first.insert(ignore_permissions=True)
		self.assertEqual([r.subcontract_item for r in first.contract_subcontract_items], [lines[SUB_A]])
		self.assertEqual(first.contract_labor_items, [])
		submit_contract(first)

		# The work item stays offered - only line A is used up.
		offered = {r["work_item"]: r for r in get_unallocated_boq_items([boq_row.parent], "Installing")}
		self.assertIn(boq_row.name, offered)
		free = {line.name: line.available_qty for line in offered[boq_row.name]["lines"]}
		self.assertAlmostEqual(free[lines[SUB_A]], 0.0, delta=1e-6)
		self.assertAlmostEqual(free[lines[SUB_B]], 20.0, delta=1e-6)

		second = make_draft_contract(project, "Installing")
		add_line(second, boq_row, 10, 40, [lines[SUB_B]])
		second.insert(ignore_permissions=True)
		submit_contract(second)

		third = make_draft_contract(project, "Installing")
		add_line(third, boq_row, 1, 30, [lines[SUB_A]])
		with self.assertRaisesRegex(frappe.ValidationError, "cannot allocate"):
			third.insert(ignore_permissions=True)

	def test_reference_covers_only_carried_lines(self):
		project, _tender, boq_row, lines = make_split_tender("ref")
		contract = make_draft_contract(project, "Installing")
		add_line(contract, boq_row, 4, 40, [lines[SUB_B]])
		contract.insert(ignore_permissions=True)
		# line B alone: 2 x 20 per unit
		self.assertAlmostEqual(flt(contract.contracted_items[0].tender_unit_cost), 40.0, delta=1e-6)

		over = make_draft_contract(project, "Installing")
		add_line(over, boq_row, 4, 41, [lines[SUB_B]])
		with self.assertRaisesRegex(frappe.ValidationError, "ceiling"):
			over.insert(ignore_permissions=True)

	def test_partial_supply_and_install_leaves_other_lines(self):
		project, _tender, boq_row, lines = make_split_tender("sai")
		materials_only = make_draft_contract(project, "Supplying and installing")
		add_line(materials_only, boq_row, 10, 100, [lines["_Test SC Stock Material"]])
		materials_only.insert(ignore_permissions=True)
		submit_contract(materials_only)

		equipment_only = make_draft_contract(project, "Supplying and installing")
		add_line(equipment_only, boq_row, 10, 20, [lines["_Test SC Equipment Item"]])
		equipment_only.insert(ignore_permissions=True)
		submit_contract(equipment_only)

		# Labor is untouched by both Supply-and-Install contracts.
		self.assertAlmostEqual(
			allocation.get_resource_availability(allocation.LABOR_DOCTYPE, lines["_Test SC Labor"]), 10.0, delta=1e-6,
		)
		labor = make_draft_contract(project, "Labor")
		add_line(labor, boq_row, 10, 50, [lines["_Test SC Labor"]])
		labor.insert(ignore_permissions=True)
		submit_contract(labor)

	def test_material_budget_nets_only_carried_material(self):
		"""Material Request budget: the company may procure a material line
		less what Supply-and-Install contracts actually carry of it."""
		project, _tender, boq_row, lines = make_split_tender("mrbudget")
		project_work_item = frappe.get_doc({
			"doctype": "Project BOQ Item", "boq_proj": project.name, "proj_item_quantity": 10,
			"source_tender_boq_item": boq_row.name,
		}).insert(ignore_permissions=True)
		self.assertAlmostEqual(
			allocation.get_material_budget(project_work_item.name)["_Test SC Stock Material"], 10.0, delta=1e-6,
		)

		# A Supply-and-Install contract that doesn't carry the material line
		# leaves the whole budget to the company...
		labor_only = make_draft_contract(project, "Supplying and installing")
		add_line(labor_only, boq_row, 10, 50, [lines["_Test SC Labor"]])
		labor_only.insert(ignore_permissions=True)
		submit_contract(labor_only)
		self.assertAlmostEqual(
			allocation.get_material_budget(project_work_item.name)["_Test SC Stock Material"], 10.0, delta=1e-6,
		)

		# ...one that carries 6 of it takes those 6 out.
		with_material = make_draft_contract(project, "Supplying and installing")
		add_line(with_material, boq_row, 6, 100, [lines["_Test SC Stock Material"]])
		with_material.insert(ignore_permissions=True)
		submit_contract(with_material)
		self.assertAlmostEqual(
			allocation.get_material_budget(project_work_item.name)["_Test SC Stock Material"], 4.0, delta=1e-6,
		)

	def test_closed_supply_and_install_releases_uninvoiced_lines(self):
		project, _tender, boq_row, lines = make_split_tender("closed")
		contract = make_draft_contract(project, "Supplying and installing")
		add_line(contract, boq_row, 10, 100, [lines["_Test SC Stock Material"]])
		contract.insert(ignore_permissions=True)
		submit_contract(contract)
		material = lines["_Test SC Stock Material"]
		self.assertAlmostEqual(allocation.get_resource_availability(allocation.MATERIAL_DOCTYPE, material), 0.0, delta=1e-6)

		frappe.db.set_value("Subcontractor Contract", contract.name, "is_closed", 1)
		# Nothing invoiced - a closed contract's line is fully released.
		self.assertAlmostEqual(allocation.get_resource_availability(allocation.MATERIAL_DOCTYPE, material), 10.0, delta=1e-6)

	def test_new_tender_line_without_lines_takes_all(self):
		"""An API insert that names no lines gets them all (the dialog's default)."""
		project, _tender, boq_row, _lines = make_split_tender("all")
		contract = make_draft_contract(project, "Installing")
		contract.append("contracted_items", {"work_item": boq_row.name, "qty": 2, "unit_price": 50})
		contract.insert(ignore_permissions=True)
		self.assertEqual(len(contract.contract_subcontract_items), 3)
		self.assertEqual(len(contract.contract_labor_items), 1)
		owner = contract.contracted_items[0].name
		self.assertEqual({r.contract_row for r in contract.contract_subcontract_items}, {owner})

		contract.set("contract_subcontract_items", [])
		contract.set("contract_labor_items", [])
		with self.assertRaisesRegex(frappe.ValidationError, "carries none of its Tender lines"):
			contract.save(ignore_permissions=True)

	def test_desk_save_relinks_contract_row(self):
		"""The client names a new row "new-contractor-contract-item-N";
		savedocs renames it, and contract_row must follow."""
		from frappe.desk.form.save import savedocs

		project, _tender, boq_row, lines = make_split_tender("relink")
		temp = "new-contractor-contract-item-1"
		propagated = allocation.get_propagated_resource_rows(boq_row.name, 3, "Installing", lines=json.dumps([lines[SUB_C]]))
		doc = make_draft_contract(project, "Installing").as_dict()
		doc.update({"__islocal": 1, "name": "new-subcontractor-contract-1"})
		doc["contracted_items"] = [{
			"doctype": "Contractor Contract Item", "name": temp, "__islocal": 1, "idx": 1,
			"parentfield": "contracted_items", "work_item": boq_row.name, "qty": 3, "unit_price": 10,
		}]
		doc["contract_subcontract_items"] = [
			{**row, "doctype": "Contractor Contract Subcontract Item", "name": f"new-contractor-contract-subcontract-item-{i}",
			 "__islocal": 1, "idx": i, "parentfield": "contract_subcontract_items", "contract_row": temp}
			for i, row in enumerate(propagated["contract_subcontract_items"], 1)
		]
		savedocs(frappe.as_json(doc), "Save")
		saved = frappe.get_doc("Subcontractor Contract", frappe.response.docs[0].name)
		self.assertEqual(saved.contract_subcontract_items[0].contract_row, saved.contracted_items[0].name)


class TestAddendumAddsLines(FrappeTestCase):
	def test_addendum_adds_another_line_to_the_original_contract(self):
		project, _tender, boq_row, lines = make_split_tender("add")
		contract = make_draft_contract(project, "Installing")
		add_line(contract, boq_row, 10, 30, [lines[SUB_A]])
		contract.insert(ignore_permissions=True)
		submit_contract(contract)
		base_row = contract.contracted_items[0].name

		addendum = addendum_for(contract, [{
			"action": "Add", "work_item": boq_row.name, "qty_delta": 10, "unit_price": 10,
			"resource_lines": json.dumps([lines[SUB_C]]),
		}])
		addendum.insert(ignore_permissions=True)
		self.assertEqual(addendum.addendum_items[-1].resource_item, SUB_C)
		addendum.submit()

		contract.reload()
		self.assertEqual(len(contract.contracted_items), 2)
		new_row = next(r for r in contract.contracted_items if r.name != base_row)
		by_line = {r.subcontract_item: r for r in contract.contract_subcontract_items}
		self.assertEqual(set(by_line), {lines[SUB_A], lines[SUB_C]})
		self.assertEqual(by_line[lines[SUB_C]].contract_row, new_row.name)
		self.assertAlmostEqual(by_line[lines[SUB_C]].qty, 10.0, delta=1e-6)
		self.assertEqual(contract.contract_labor_items, [])

		# A line already on the contract can't be added again.
		again = addendum_for(contract, [{
			"action": "Add", "work_item": boq_row.name, "qty_delta": 1, "unit_price": 10,
			"resource_lines": json.dumps([lines[SUB_C]]),
		}])
		with self.assertRaisesRegex(frappe.ValidationError, "already on contract"):
			again.insert(ignore_permissions=True)

	def test_addendum_line_over_allocation_and_own_rescale(self):
		project, _tender, boq_row, lines = make_split_tender("rescale")
		other = make_draft_contract(project, "Installing")
		add_line(other, boq_row, 6, 10, [lines[SUB_C]])
		other.insert(ignore_permissions=True)
		submit_contract(other)

		contract = make_draft_contract(project, "Installing")
		add_line(contract, boq_row, 10, 30, [lines[SUB_A]])
		contract.insert(ignore_permissions=True)
		submit_contract(contract)

		# 4 of line C left: 5 is over.
		over = addendum_for(contract, [{
			"action": "Add", "work_item": boq_row.name, "qty_delta": 5, "unit_price": 10,
			"resource_lines": json.dumps([lines[SUB_C]]),
		}])
		over.insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot allocate"):
			over.submit()

		ok = addendum_for(contract, [{
			"action": "Add", "work_item": boq_row.name, "qty_delta": 4, "unit_price": 10,
			"resource_lines": json.dumps([lines[SUB_C]]),
		}])
		ok.insert(ignore_permissions=True)
		ok.submit()
		contract.reload()
		base_row = contract.contracted_items[0]

		# Updating the base row rescales line A only - line C belongs to the
		# addendum's own row.
		update = addendum_for(contract, [{
			"base_contract_item": base_row.name, "action": "Update", "new_qty": 5, "new_unit_price": 30,
		}])
		update.insert(ignore_permissions=True)
		update.submit()
		contract.reload()
		qty = {r.subcontract_item: flt(r.qty) for r in contract.contract_subcontract_items}
		self.assertAlmostEqual(qty[lines[SUB_A]], 5.0, delta=1e-6)
		self.assertAlmostEqual(qty[lines[SUB_C]], 4.0, delta=1e-6)


class TestTemplateDescription(FrappeTestCase):
	def test_template_line_description_is_editable(self):
		make_split_template()
		project = frappe.get_doc({"doctype": "Project", "project_name": "_PC NoTender desc"}).insert(ignore_permissions=True)
		contract = make_draft_contract(project, "Installing")
		contract.append("contracted_items", {
			"work_item_template": "_PC Template", "qty": 1, "unit_price": 10, "description": "Custom scope wording",
		})
		contract.insert(ignore_permissions=True)
		self.assertEqual(contract.contracted_items[0].description, "Custom scope wording")

		contract.contracted_items[0].description = ""
		contract.save(ignore_permissions=True)
		self.assertEqual(contract.contracted_items[0].description, "_PC Template description")

	def test_tender_line_description_still_server_set(self):
		project, _tender, boq_row, lines = make_split_tender("desc")
		contract = make_draft_contract(project, "Installing")
		add_line(contract, boq_row, 1, 30, [lines[SUB_A]])
		contract.contracted_items[0].description = "typed by hand"
		contract.insert(ignore_permissions=True)
		self.assertNotEqual(contract.contracted_items[0].description, "typed by hand")
