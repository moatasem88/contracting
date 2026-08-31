# Copyright (c) 2026, kazem and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from contracting.contracting.api import WORK_ITEM_GROUP, create_work_item_group

TEST_CUSTOMER = "_Test Tender Customer"


def ensure_customer():
	if not frappe.db.exists("Customer", TEST_CUSTOMER):
		frappe.get_doc({
			"doctype": "Customer",
			"customer_name": TEST_CUSTOMER,
			"customer_type": "Company",
		}).insert(ignore_permissions=True)
	return TEST_CUSTOMER


def make_template(name, uom="Nos", description="template reference text"):
	if frappe.db.exists("Work Item Template V2", name):
		frappe.delete_doc("Work Item Template V2", name, force=True, ignore_permissions=True)
	if frappe.db.exists("Item", "WI-" + name):
		frappe.delete_doc("Item", "WI-" + name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "Work Item Template V2",
		"template_name": name,
		"uom": uom,
		"description": description,
	}).insert(ignore_permissions=True)


def make_project_tender(project_name, client=TEST_CUSTOMER):
	"""Tender.project_tender is mandatory - every Tender needs one of these
	to link to, even tests that don't care about Project Tender's own
	behaviour (see the Won Project Tender BRD, 2026-08-28)."""
	return frappe.get_doc({
		"doctype": "Project Tender",
		"naming_series": "PT-.YYYY.-",
		"project_name": project_name,
		"client": client,
	}).insert(ignore_permissions=True)


def make_tender(project_name, template=None, qty=10, description="per-tender scope", client=TEST_CUSTOMER):
	project_tender = make_project_tender(project_name, client=client)
	tender = frappe.get_doc({
		"doctype": "Tender",
		"naming_series": "TND-.YYYY.-",
		"project_tender": project_tender.name,
		"status": "Draft",
		"blanket_margin_percent": 20,
	})
	row = {"item_name": "Work item", "original_quantity": qty, "description": description}
	if template:
		row["work_item_template"] = template
	tender.append("boq_items", row)
	tender.insert(ignore_permissions=True)
	return tender


class TestTender(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		create_work_item_group()
		ensure_customer()
		if not frappe.db.exists("Item", "_Test Tender Labor"):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": "_Test Tender Labor",
				"item_group": "Labor",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)

	# ---------- Work Item Template V2 -> Item ----------

	def test_template_creates_item(self):
		template = make_template("_Test WIT Create", uom="Cubic Meter", description="slab desc")

		self.assertEqual(template.item, "WI-_Test WIT Create")
		item = frappe.get_doc("Item", template.item)
		self.assertEqual(item.item_group, WORK_ITEM_GROUP)
		self.assertEqual(item.stock_uom, "Cubic Meter")
		self.assertEqual(item.is_stock_item, 0)
		self.assertEqual(item.is_sales_item, 1)
		self.assertEqual(item.description, "slab desc")

	def test_template_edit_syncs_item(self):
		template = make_template("_Test WIT Sync")

		template.description = "revised"
		template.uom = "Cubic Meter"
		template.save(ignore_permissions=True)

		item = frappe.get_doc("Item", template.item)
		self.assertEqual(item.description, "revised")
		self.assertEqual(item.stock_uom, "Cubic Meter")

	def test_template_rename_renames_item(self):
		template = make_template("_Test WIT Rename")
		if frappe.db.exists("Item", "WI-_Test WIT Renamed"):
			frappe.delete_doc("Item", "WI-_Test WIT Renamed", force=True, ignore_permissions=True)

		frappe.rename_doc("Work Item Template V2", template.name, "_Test WIT Renamed")

		self.assertFalse(frappe.db.exists("Item", "WI-_Test WIT Rename"))
		self.assertTrue(frappe.db.exists("Item", "WI-_Test WIT Renamed"))
		self.assertEqual(
			frappe.db.get_value("Work Item Template V2", "_Test WIT Renamed", "item"),
			"WI-_Test WIT Renamed",
		)

	def test_template_blocked_when_item_code_taken(self):
		"""An Item under our code that isn't ours must never be adopted onto
		a template - it would end up on a customer's Sales Order."""
		if not frappe.db.exists("Item", "WI-_Test WIT Squatted"):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": "WI-_Test WIT Squatted",
				"item_group": "Civil",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Work Item Template V2",
				"template_name": "_Test WIT Squatted",
				"uom": "Nos",
			}).insert(ignore_permissions=True)

	# ---------- Tender BOQ row ----------

	def test_boq_fetches_item_and_description(self):
		template = make_template("_Test WIT Fetch", description="TEMPLATE TEXT")
		tender = make_tender("_Test Fetch", template=template.name, description=None)

		row = tender.boq_items[0]
		self.assertEqual(row.item_code, template.item)
		self.assertEqual(row.description, "TEMPLATE TEXT")

	def test_boq_description_is_not_clobbered(self):
		"""fetch_if_empty: the template seeds a starting point, but the
		per-tender wording must survive every later save."""
		template = make_template("_Test WIT Keep", description="TEMPLATE TEXT")
		tender = make_tender("_Test Keep", template=template.name)

		tender.boq_items[0].description = "PER-TENDER TEXT"
		tender.save(ignore_permissions=True)
		tender.reload()

		self.assertEqual(tender.boq_items[0].description, "PER-TENDER TEXT")

	# ---------- Resource row amount (Direct + Safety Factor + Indirect Cost) ----------

	def test_amount_includes_indirect_cost(self):
		"""2026-08-28 BRD: a resource row's amount is Direct + Safety +
		Indirect, all three layers - not just the first two."""
		tender = make_tender("_Test Indirect Included", qty=1)
		tender.safety_factor_percent = 10
		tender.propagated_addition_percent = 20
		tender.append("labor_items", {
			"work_item": tender.boq_items[0].idx,
			"item": "_Test Tender Labor",
			"qty_per_unit": 1,
			"rate": 100,
		})
		tender.save(ignore_permissions=True)

		row = tender.labor_items[0]
		self.assertAlmostEqual(row.direct_cost_amount, 100)
		self.assertAlmostEqual(row.safety_factor_amount, 10)
		self.assertAlmostEqual(row.indirect_cost_amount, 22)
		self.assertAlmostEqual(row.amount, 132)

	def test_zero_indirect_cost_amount_unchanged(self):
		"""No propagated_addition_percent (no linked Project Tender
		addition) -> amount is Direct + Safety only, same as before this
		BRD - indirect cost is legitimately zero, not silently dropped."""
		tender = make_tender("_Test Indirect Zero", qty=1)
		tender.safety_factor_percent = 10
		tender.append("labor_items", {
			"work_item": tender.boq_items[0].idx,
			"item": "_Test Tender Labor",
			"qty_per_unit": 1,
			"rate": 100,
		})
		tender.save(ignore_permissions=True)

		row = tender.labor_items[0]
		self.assertAlmostEqual(row.indirect_cost_amount, 0)
		self.assertAlmostEqual(row.amount, 110)

