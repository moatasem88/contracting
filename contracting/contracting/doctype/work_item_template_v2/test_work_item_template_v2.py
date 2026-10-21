# Copyright (c) 2026, kazem and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from contracting.contracting.api import create_work_item_group
from contracting.contracting.api.work_item_template_v2 import get_v2_template_rows

TEST_LABOR_ITEM = "_Test Tender Labor"


def ensure_labor_item():
	if not frappe.db.exists("Item", TEST_LABOR_ITEM):
		frappe.get_doc({
			"doctype": "Item",
			"item_code": TEST_LABOR_ITEM,
			"item_group": "Labor",
			"stock_uom": "Nos",
			"is_stock_item": 0,
		}).insert(ignore_permissions=True)


def make_priced_template(name, additions):
	"""A template whose resources cost exactly 100 per unit, plus the given
	additions - the fixture from the 2026-10-13 per-unit addition BRD."""
	if frappe.db.exists("Work Item Template V2", name):
		frappe.delete_doc("Work Item Template V2", name, force=True, ignore_permissions=True)
	if frappe.db.exists("Item", "WI-" + name):
		frappe.delete_doc("Item", "WI-" + name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "Work Item Template V2",
		"template_name": name,
		"uom": "Nos",
		"labor_items": [{"item": TEST_LABOR_ITEM, "qty_per_unit": 1, "rate": 100}],
		"additions": additions,
	}).insert(ignore_permissions=True)


def brd_additions():
	"""A1 Percentage 10, A2 Fixed 5 per unit, A3 Fixed 50 lump sum."""
	return [
		{"addition_name": "A1", "calculation_type": "Percentage", "value": 10},
		{"addition_name": "A2", "calculation_type": "Fixed Amount", "value": 5, "per_unit_item": 1},
		{"addition_name": "A3", "calculation_type": "Fixed Amount", "value": 50, "per_unit_item": 0},
	]


class TestWorkItemTemplateV2(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		create_work_item_group()
		ensure_labor_item()

	def assert_totals(self, template, work_item_cost, additions, cost_per_unit, lump_sum):
		self.assertAlmostEqual(template.total_work_item_cost, work_item_cost)
		self.assertAlmostEqual(template.total_additions, additions)
		self.assertAlmostEqual(template.total_cost_per_unit, cost_per_unit)
		self.assertAlmostEqual(template.total_fixed_lump_sum, lump_sum)

	def test_split_totals(self):
		"""TC-02: per-unit fixed amounts are in the per-unit cost, lump
		sums are reported separately."""
		template = make_priced_template("_Test WIT Per Unit", brd_additions())
		self.assert_totals(template, 100, 15, 115, 50)

	def test_untick_lump_sum_moves_into_per_unit(self):
		"""TC-03 (server half): ticking A3 makes it per unit."""
		template = make_priced_template("_Test WIT Per Unit Toggle", brd_additions())
		template.additions[2].per_unit_item = 1
		template.save(ignore_permissions=True)
		self.assert_totals(template, 100, 65, 165, 0)

	def test_flag_ignored_on_percentage_row(self):
		"""TC-11: per_unit_item on a Percentage row changes nothing."""
		additions = brd_additions()
		additions[0]["per_unit_item"] = 1
		template = make_priced_template("_Test WIT Per Unit Pct", additions)
		self.assert_totals(template, 100, 15, 115, 50)

	def test_lump_sum_only_without_resources(self):
		"""Edge case: no resources, only a lump sum."""
		name = "_Test WIT Lump Only"
		if frappe.db.exists("Work Item Template V2", name):
			frappe.delete_doc("Work Item Template V2", name, force=True, ignore_permissions=True)
		template = frappe.get_doc({
			"doctype": "Work Item Template V2",
			"template_name": name,
			"uom": "Nos",
			"additions": [
				{"addition_name": "L", "calculation_type": "Fixed Amount", "value": 50, "per_unit_item": 0},
			],
		}).insert(ignore_permissions=True)
		self.assert_totals(template, 0, 0, 0, 50)

	def test_pull_payload_splits_fixed_additions(self):
		"""FR-04: the Tender pull payload carries both fixed sums apart."""
		template = make_priced_template("_Test WIT Per Unit Pull", brd_additions())
		parent = get_v2_template_rows(template.name)["parent"]
		self.assertAlmostEqual(parent["other_additions_pct"], 10)
		self.assertAlmostEqual(parent["fixed_additions_per_unit"], 5)
		self.assertAlmostEqual(parent["fixed_additions"], 50)
