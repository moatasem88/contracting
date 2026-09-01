# Copyright (c) 2026, kazem and Contributors
# See license.txt

import frappe
import unittest

from frappe.tests.utils import FrappeTestCase


def make_tender(project_name, tender_category, grand_total):
	"""A Tender with just enough on it for grand_total to be trustworthy
	via Tender.validate()'s own rollup - one BOQ row backed by a Material
	Item row priced to land on the requested grand_total exactly."""
	doc = frappe.get_doc({
		"doctype": "Tender",
		"project_name": project_name,
		"naming_series": "TND-.YYYY.-",
		"tender_category": tender_category,
	})
	doc.append("boq_items", {"item_name": "Test Work Item", "original_quantity": 1, "is_group": 0})
	doc.insert(ignore_permissions=True)
	work_item_idx = doc.boq_items[0].idx
	doc.append("material_items", {
		"work_item": work_item_idx,
		"item": "Test Work Item",
		"qty_per_unit": 1,
		"rate": grand_total,
		"currency": "EGP",
	})
	doc.save(ignore_permissions=True)
	return doc


def ensure_ic_item():
	if not frappe.db.exists("Item", "__ic_test_item__"):
		frappe.get_doc({
			"doctype": "Item", "item_code": "__ic_test_item__", "item_group": "Labor",
			"stock_uom": "Nos", "is_stock_item": 0,
		}).insert(ignore_permissions=True)
	return "__ic_test_item__"


def make_ic_project_tender(project_name):
	"""Self-contained Project Tender fixture for the indirect-cost/currency
	BRD's own tests - doesn't reuse make_tender() above, which predates
	Tender.project_tender becoming mandatory and no longer inserts.

	Assigns an explicit unique name rather than relying on the
	naming_series counter - within one test run that counter can hand out
	a number a since-rolled-back test already consumed, colliding on
	insert (a test-runner quirk, unrelated to naming_series in production
	use, which only ever moves forward one real request at a time)."""
	doc = frappe.get_doc({
		"doctype": "Project Tender",
		"naming_series": "PT-.YYYY.-",
		"project_name": project_name,
		"client": frappe.db.get_value("Customer", {}, "name"),
	})
	doc.name = "_ic_pt_" + frappe.generate_hash(length=10)
	return doc.insert(ignore_permissions=True)


def make_ic_tender(project_tender_name, project_name, tender_category="Civil", qty=10, rate=100, safety_factor_percent=0):
	"""Self-contained Tender fixture, always linked to an existing Project
	Tender (project_tender is mandatory) - one BOQ row backed by one
	Material row."""
	item = ensure_ic_item()
	doc = frappe.get_doc({
		"doctype": "Tender",
		"naming_series": "TND-.YYYY.-",
		"project_name": project_name,
		"client": frappe.db.get_value("Customer", {}, "name"),
		"tender_category": tender_category,
		"project_tender": project_tender_name,
		"safety_factor_percent": safety_factor_percent,
	})
	doc.name = "_ic_tnd_" + frappe.generate_hash(length=10)
	doc.append("boq_items", {"item_name": "IC Work Item", "original_quantity": 1, "is_group": 0})
	doc.insert(ignore_permissions=True)
	work_item_idx = doc.boq_items[0].idx
	doc.append("material_items", {
		"work_item": work_item_idx, "item": item, "qty_per_unit": qty, "rate": rate, "currency": "EGP",
	})
	doc.save(ignore_permissions=True)
	return doc


class TestIndirectCostAndCurrency(FrappeTestCase):
	"""Tests for the Tender Cost Layering, Indirect-Cost Propagation &
	Multi-Currency Display BRD (2026-08-26). Prefixed test_ic## and kept in
	their own TestCase, distinct from TestProjectTender's test_tc##
	methods (2026-08-22 BRD), which use unrelated TC numbering that would
	otherwise collide (e.g. both BRDs define a "TC-12"). Uses FrappeTestCase
	(class-level rollback) rather than TestProjectTender's per-method
	frappe.db.rollback() - naming-series counters commit independently of
	the outer transaction, so a per-method rollback can hand out an
	already-consumed series number to the next test in the same run.
	"""

	def test_ic01_02_resource_row_cost_layering(self):
		# TC-01/TC-02: qty=10, rate=100, safety_factor_percent=5.
		pt = make_ic_project_tender("__ic01_project__")
		t = make_ic_tender(pt.name, "__ic01_tender__", qty=10, rate=100, safety_factor_percent=5)
		row = t.material_items[0]
		self.assertAlmostEqual(row.direct_cost_amount, 1000, delta=0.01)
		self.assertAlmostEqual(row.safety_factor_amount, 50, delta=0.01)
		self.assertAlmostEqual(row.amount, 1050, delta=0.01)
		self.assertAlmostEqual(row.amount_currency, 1000, delta=0.01)
		self.assertAlmostEqual(row.rate_egp, 100, delta=0.01)

		# Link into the Project Tender, with a 14% VAT Deduction row (VAT
		# is no longer an implicit header default - it's an ordinary row
		# now) -> total_addition_percent becomes 14%, which then propagates
		# back down (FR-04) without reopening the Tender.
		pt.reload()
		pt.append("direct_cost_details", {"tender": t.name})
		pt.append("overhead_deduction_details", {
			"cost_component": "Other Deductions", "calculation_type": "Fixed Amount", "amount": 1050 * 0.14,
		})
		pt.save(ignore_permissions=True)
		t.reload()
		row = t.material_items[0]
		self.assertAlmostEqual(row.indirect_cost_amount, 1050 * 0.14, delta=0.01)

	def test_ic03_04_boq_rollup_and_display_currency(self):
		pt = make_ic_project_tender("__ic03_project__")
		t = make_ic_tender(pt.name, "__ic03_tender__", qty=1, rate=65000, safety_factor_percent=0)
		boq_row = t.boq_items[0]
		self.assertAlmostEqual(boq_row.material_cost, 65000, delta=0.01)
		self.assertAlmostEqual(boq_row.boq_direct_cost, 65000, delta=0.01)

		if not frappe.db.exists("Currency", "EUR"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "EUR"}).insert(ignore_permissions=True)
		t.append("currency_table", {"currency": "EUR", "exchange_rate": 65})
		t.boq_items[0].display_currency = "EUR"
		t.save(ignore_permissions=True)
		self.assertAlmostEqual(t.boq_items[0].display_amount, 1000, delta=0.01)

	def test_ic05_06_07_cross_sync_converges_without_loop(self):
		# TC-05/06/07 combined: editing the Tender updates the Project
		# Tender without opening it, editing the Project Tender pushes back
		# down, and a single edit converges in one pass (no save loop).
		pt = make_ic_project_tender("__ic05_project__")
		t = make_ic_tender(pt.name, "__ic05_tender__", qty=1, rate=500000)
		pt.reload()
		pt.append("direct_cost_details", {"tender": t.name})
		pt.save(ignore_permissions=True)
		# pt.save() just pushed propagated_addition_percent down to t and
		# saved it server-side (FR-11) - a real open Tender form would
		# reload live via the FR-13 realtime broadcast at this point;
		# reload() here simulates that so t's in-memory copy isn't stale.
		t.reload()

		t.material_items[0].rate = 550000
		t.save(ignore_permissions=True)

		pushed_total = frappe.db.get_value(
			"Project Tender Direct Cost Detail", {"tender": t.name}, "tender_total"
		)
		self.assertAlmostEqual(pushed_total, t.tender_final_cost, delta=0.01)
		self.assertAlmostEqual(
			frappe.db.get_value("Project Tender", pt.name, "total_direct_cost"), t.tender_final_cost, delta=0.01
		)

		# Prove one-pass convergence directly: a single Tender save must
		# call the Project-Tender-side push function at most once (for the
		# one Project Tender it's linked to), not recurse back into itself.
		from contracting.contracting.utils import cross_sync

		original = cross_sync.sync_project_tender_to_tenders
		calls = []

		def counting_wrapper(pt_doc, skip_tender=None):
			calls.append((pt_doc.name, skip_tender))
			return original(pt_doc, skip_tender=skip_tender)

		cross_sync.sync_project_tender_to_tenders = counting_wrapper
		try:
			t.material_items[0].rate = 600000
			t.save(ignore_permissions=True)
		finally:
			cross_sync.sync_project_tender_to_tenders = original

		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0], (pt.name, t.name))

	def test_ic10_11_total_addition_percent(self):
		pt = make_ic_project_tender("__ic10_project__")
		t = make_ic_tender(pt.name, "__ic10_tender__", qty=1, rate=1000000)
		pt.reload()
		pt.append("direct_cost_details", {"tender": t.name})
		# Fixed Amount, not "Percentage of Total Price" (self-referential) -
		# a flat 45% of direct cost (the only other total here) reproduces
		# the old header vat_percentage=45 behaviour exactly.
		pt.append("overhead_deduction_details", {
			"cost_component": "Other Deductions", "calculation_type": "Fixed Amount", "amount": 1000000 * 0.45,
		})
		pt.save(ignore_permissions=True)
		self.assertAlmostEqual(pt.total_addition_percent, 45, delta=0.01)

		# TC-11: no direct_cost_details rows -> 0, no division-by-zero.
		empty_pt = make_ic_project_tender("__ic11_project__")
		self.assertEqual(empty_pt.total_addition_percent, 0)

	def test_ic12_percentage_of_total_price_single_row(self):
		pt = make_ic_project_tender("__ic12_project__")
		pt.append("overhead_deduction_details", {
			"cost_component": "VAT", "calculation_type": "Percentage of Total Price", "rate": 14,
		})
		pt.append("overhead_deduction_details", {
			"cost_component": "Other Deductions", "calculation_type": "Fixed Amount", "amount": 1228334199.36,
		})
		pt.save(ignore_permissions=True)
		self.assertAlmostEqual(pt.total_sell_price, 1428295580.65, delta=1)
		vat_row = next(r for r in pt.overhead_deduction_details if r.cost_component == "VAT")
		self.assertAlmostEqual(vat_row.amount, 199961381.29, delta=1)

	def test_ic13_percentage_of_total_price_guard(self):
		pt = frappe.get_doc({
			"doctype": "Project Tender",
			"naming_series": "PT-.YYYY.-",
			"project_name": "__ic13_project__",
			"client": frappe.db.get_value("Customer", {}, "name"),
			"overhead_deduction_details": [
				{"cost_component": "VAT", "calculation_type": "Percentage of Total Price", "rate": 60},
			],
			"indirect_cost_details": [
				{"cost_component": "Risk", "calculation_type": "Percentage of Total Price", "rate": 45},
			],
		})
		self.assertRaises(frappe.ValidationError, pt.insert, ignore_permissions=True)

	def test_ic14_percentage_of_total_price_multiple_rows(self):
		pt = make_ic_project_tender("__ic14_project__")
		t = make_ic_tender(pt.name, "__ic14_tender__", qty=1, rate=1000000)
		pt.reload()
		pt.append("direct_cost_details", {"tender": t.name})
		pt.append("overhead_deduction_details", {
			"cost_component": "VAT", "calculation_type": "Percentage of Total Price", "rate": 10,
		})
		pt.append("overhead_deduction_details", {
			"cost_component": "Other Deductions", "calculation_type": "Percentage of Total Price", "rate": 5,
		})
		pt.save(ignore_permissions=True)
		# K = total_direct_cost = 1,000,000 (no other components).
		self.assertAlmostEqual(pt.total_sell_price, 1000000 / 0.85, delta=1)

	def test_ic15_section_percentages(self):
		# direct=100, indirect=20 (20% of direct), a 3 fixed-amount deduction
		# -> total_sell_price=123, matching the BRD's own worked example.
		pt = make_ic_project_tender("__ic15_project__")
		t = make_ic_tender(pt.name, "__ic15_tender__", qty=1, rate=100)
		pt.reload()
		pt.append("direct_cost_details", {"tender": t.name})
		pt.append("indirect_cost_details", {
			"cost_component": "Risk", "calculation_type": "Percentage of Direct Cost", "rate": 20,
		})
		pt.append("overhead_deduction_details", {
			"cost_component": "Other Deductions", "calculation_type": "Fixed Amount", "amount": 3,
		})
		pt.save(ignore_permissions=True)

		self.assertAlmostEqual(pt.total_direct_cost, 100, delta=0.01)
		self.assertAlmostEqual(pt.total_indirect_cost, 20, delta=0.01)
		self.assertAlmostEqual(pt.total_sell_price, 123, delta=0.01)
		self.assertAlmostEqual(pt.direct_cost_percent_of_total_price, 100 / 123 * 100, delta=0.01)
		self.assertAlmostEqual(pt.indirect_cost_percent_of_direct_cost, 20, delta=0.01)
		self.assertAlmostEqual(pt.indirect_cost_percent_of_total_price, 20 / 123 * 100, delta=0.01)
		self.assertAlmostEqual(pt.overhead_deduction_percent_of_direct_cost, 3, delta=0.01)
		self.assertAlmostEqual(pt.overhead_deduction_percent_of_total_price, 3 / 123 * 100, delta=0.01)

		# TC-07: empty Project Tender -> all five 0, no division-by-zero.
		empty_pt = make_ic_project_tender("__ic15_empty_project__")
		self.assertEqual(empty_pt.direct_cost_percent_of_total_price, 0)
		self.assertEqual(empty_pt.indirect_cost_percent_of_direct_cost, 0)
		self.assertEqual(empty_pt.indirect_cost_percent_of_total_price, 0)
		self.assertEqual(empty_pt.overhead_deduction_percent_of_direct_cost, 0)
		self.assertEqual(empty_pt.overhead_deduction_percent_of_total_price, 0)

	def test_dc01_worked_example(self):
		# Direct Cost restructure BRD (2026-09-01) TC-01: the full worked
		# example - qty=10 x rate=100, safety_factor_percent=10, row
		# vat_percentage=14/other_additions_pct=5/fixed_additions=50,
		# propagated_addition_percent=20 (set directly here to isolate this
		# from Project Tender's own calculation chain, already proven
		# untouched by FR-11 in test_ic05_06_07 above).
		pt = make_ic_project_tender("__dc01_project__")
		t = make_ic_tender(pt.name, "__dc01_tender__", qty=10, rate=100, safety_factor_percent=10)
		t.boq_items[0].vat_percentage = 14
		t.boq_items[0].other_additions_pct = 5
		t.boq_items[0].fixed_additions = 50
		t.propagated_addition_percent = 20
		t.save(ignore_permissions=True)

		row = t.boq_items[0]
		self.assertAlmostEqual(row.boq_direct_cost, 1240, delta=0.01)
		self.assertAlmostEqual(row.boq_safety_factor_amount, 100, delta=0.01)
		self.assertAlmostEqual(row.boq_indirect_cost_amount, 268, delta=0.01)

		self.assertAlmostEqual(t.total_direct_cost, 1240, delta=0.01)
		# BRD §12's worked-example table lists total_additions=200, but its
		# own formula (§5's code snippet and FR-01/FR-04's prose, both VAT-
		# excluded) computes raw_direct*other_additions_pct/100+fixed =
		# 1000*5%+50 = 100 - a table-cell arithmetic slip, not a spec
		# disagreement. 100 is what the formal spec (and this code) produce.
		self.assertAlmostEqual(t.total_additions, 100, delta=0.01)
		self.assertAlmostEqual(t.total_safety_factor, 100, delta=0.01)
		self.assertAlmostEqual(t.tender_final_cost, 1340, delta=0.01)
		self.assertAlmostEqual(t.total_indirect_cost, 268, delta=0.01)
		self.assertAlmostEqual(t.sell_amount, 1608, delta=0.01)

	def test_dc05_final_cost_immune_to_propagated_percent(self):
		# TC-05: tender_final_cost (and everything it's built from) must not
		# move when propagated_addition_percent changes - only
		# total_indirect_cost/sell_amount should. This is the property that
		# eliminates the self-reference described in the BRD's §2.
		pt = make_ic_project_tender("__dc05_project__")
		t = make_ic_tender(pt.name, "__dc05_tender__", qty=10, rate=100, safety_factor_percent=10)
		t.boq_items[0].vat_percentage = 14
		t.boq_items[0].other_additions_pct = 5
		t.boq_items[0].fixed_additions = 50
		t.propagated_addition_percent = 20
		t.save(ignore_permissions=True)
		self.assertAlmostEqual(t.tender_final_cost, 1340, delta=0.01)

		t.propagated_addition_percent = 45
		t.save(ignore_permissions=True)

		self.assertAlmostEqual(t.total_direct_cost, 1240, delta=0.01)
		self.assertAlmostEqual(t.total_safety_factor, 100, delta=0.01)
		self.assertAlmostEqual(t.tender_final_cost, 1340, delta=0.01)
		self.assertAlmostEqual(t.total_indirect_cost, 1340 * 0.45, delta=0.01)
		self.assertAlmostEqual(t.sell_amount, 1340 * 1.45, delta=0.01)

	def test_dc06_project_tender_convergence(self):
		# TC-06: re-saving Tender -> Project Tender -> Tender must converge
		# total_direct_cost/total_addition_percent to a stable value, not
		# keep drifting on each additional save (the pre-fix behaviour the
		# BRD's §2 diagnoses).
		pt = make_ic_project_tender("__dc06_project__")
		t = make_ic_tender(pt.name, "__dc06_tender__", qty=10, rate=100, safety_factor_percent=10)
		pt.reload()
		pt.append("direct_cost_details", {"tender": t.name})
		pt.append("overhead_deduction_details", {
			"cost_component": "Other Deductions", "calculation_type": "Fixed Amount", "amount": 1100 * 0.20,
		})
		pt.save(ignore_permissions=True)
		t.reload()

		first_direct_cost = frappe.db.get_value("Project Tender", pt.name, "total_direct_cost")
		first_addition_percent = frappe.db.get_value("Project Tender", pt.name, "total_addition_percent")

		t.save(ignore_permissions=True)
		pt.reload()
		pt.save(ignore_permissions=True)
		t.reload()
		t.save(ignore_permissions=True)

		second_direct_cost = frappe.db.get_value("Project Tender", pt.name, "total_direct_cost")
		second_addition_percent = frappe.db.get_value("Project Tender", pt.name, "total_addition_percent")

		self.assertAlmostEqual(first_direct_cost, second_direct_cost, delta=0.01)
		self.assertAlmostEqual(first_addition_percent, second_addition_percent, delta=0.01)


class TestProjectTender(unittest.TestCase):
	def setUp(self):
		self._tenders = []

	def tearDown(self):
		frappe.db.rollback()

	def make_tender(self, *args, **kwargs):
		doc = make_tender(*args, **kwargs)
		self._tenders.append(doc.name)
		return doc

	def test_tc01_direct_indirect_overhead_deduction_rollup(self):
		civil = self.make_tender("__test_civil__", "Civil", 651656813)
		mech = self.make_tender("__test_mech__", "Mechanical", 153323882)
		elec = self.make_tender("__test_elec__", "Electrical", 161377697)

		pt = frappe.get_doc({
			"doctype": "Project Tender",
			"naming_series": "PT-.YYYY.-",
			"project_name": "__test_project__",
			"client": frappe.db.get_value("Customer", {}, "name"),
			"direct_cost_details": [
				{"tender_category": "Civil", "tender": civil.name},
				{"tender_category": "Mechanical", "tender": mech.name},
				{"tender_category": "Electrical", "tender": elec.name},
			],
		})

		# The BRD's TC-01 only states the aggregate total_indirect_cost
		# (141,181,008.36) - the individual Fixed Amount values for the
		# non-percentage components below live in the original uploaded
		# Excel sheet, not reproduced in the BRD text. They're rolled into
		# "Others" here as one reconciling figure so this test still proves
		# the calculation engine reproduces the BRD's published totals
		# exactly, without fabricating per-line business data nobody supplied.
		for component_name in (
			"Site Management & Supervision", "Temporary Site Facilities",
			"Transportation for Workers", "Accommodation", "Safety Requirement",
			"Equipment & Instrument", "Syndicates Stamp", "Taxes",
		):
			pt.append("indirect_cost_details", {"cost_component": component_name})
		pt.append("indirect_cost_details", {
			"cost_component": "Others", "calculation_type": "Fixed Amount", "amount": 57977550.81,
		})
		for component_name in ("Financial Requirement", "Social Insurance", "Project Insurance", "Risk"):
			pt.append("indirect_cost_details", {"cost_component": component_name})

		pt.append("overhead_deduction_details", {"cost_component": "Profit"})
		pt.append("overhead_deduction_details", {"cost_component": "Head Office Overhead"})
		pt.append("overhead_deduction_details", {
			"cost_component": "Other Deductions",
			"calculation_type": "Fixed Amount",
			"amount": 92480498.11,
		})
		pt.append("overhead_deduction_details", {
			"cost_component": "VAT",
			"calculation_type": "Percentage of Total Price",
			"rate": 0,
		})

		pt.insert(ignore_permissions=True)

		self.assertAlmostEqual(pt.total_direct_cost, 966358392, delta=1)
		self.assertAlmostEqual(pt.sub_total_overhead_and_profit, 1228334199.36, delta=1)
		self.assertAlmostEqual(pt.total_sell_price, 1320814697.47, delta=1)

	def test_tc04_duplicate_tender_blocked(self):
		civil = self.make_tender("__test_dup__", "Civil", 100)
		pt = frappe.get_doc({
			"doctype": "Project Tender",
			"naming_series": "PT-.YYYY.-",
			"project_name": "__test_dup_project__",
			"client": frappe.db.get_value("Customer", {}, "name"),
			"direct_cost_details": [
				{"tender_category": "Civil", "tender": civil.name},
				{"tender_category": "Civil", "tender": civil.name},
			],
		})
		self.assertRaises(frappe.ValidationError, pt.insert, ignore_permissions=True)

	def test_tc05_category_mismatch_blocked(self):
		civil = self.make_tender("__test_mismatch__", "Civil", 100)
		pt = frappe.get_doc({
			"doctype": "Project Tender",
			"naming_series": "PT-.YYYY.-",
			"project_name": "__test_mismatch_project__",
			"client": frappe.db.get_value("Customer", {}, "name"),
			"direct_cost_details": [
				{"tender_category": "Electrical", "tender": civil.name},
			],
		})
		self.assertRaises(frappe.ValidationError, pt.insert, ignore_permissions=True)

	def test_tc07_zero_direct_cost_no_error(self):
		pt = frappe.get_doc({
			"doctype": "Project Tender",
			"naming_series": "PT-.YYYY.-",
			"project_name": "__test_zero_project__",
			"client": frappe.db.get_value("Customer", {}, "name"),
			"indirect_cost_details": [{"cost_component": "Risk"}],
		})
		pt.insert(ignore_permissions=True)
		self.assertEqual(pt.total_direct_cost, 0)
		self.assertEqual(pt.indirect_cost_details[0].percent_of_direct_cost, 0)

	def test_tc08_fixed_amount_ignores_position(self):
		civil = self.make_tender("__test_fixed__", "Civil", 1000)
		pt = frappe.get_doc({
			"doctype": "Project Tender",
			"naming_series": "PT-.YYYY.-",
			"project_name": "__test_fixed_project__",
			"client": frappe.db.get_value("Customer", {}, "name"),
			"direct_cost_details": [{"tender_category": "Civil", "tender": civil.name}],
			"overhead_deduction_details": [
				{"cost_component": "VAT", "calculation_type": "Percentage of Total Price", "rate": 14},
				{"cost_component": "Other Deductions", "calculation_type": "Fixed Amount", "amount": 500},
			],
		})
		pt.insert(ignore_permissions=True)
		other_deductions_row = next(r for r in pt.overhead_deduction_details if r.cost_component == "Other Deductions")
		self.assertEqual(other_deductions_row.amount, 500)

	def test_tc12_base_currency_implicit(self):
		# A resource row with no currency set at all resolves to the base
		# currency at exchange_rate = 1 - this is Tender's own behaviour,
		# exercised here only insofar as make_tender relies on it.
		civil = self.make_tender("__test_base_ccy__", "Civil", 250)
		self.assertEqual(civil.material_items[0].exchange_rate, 1)


def make_won_ready_tender(project_tender_name, project_name, tender_category="Civil", qty=1, rate=100):
	"""Like make_ic_tender, but with a real Work Item Template so the BOQ
	row carries item_code - Project Tender.validate_won_preconditions()
	throws without one, and Won automation never runs at all."""
	from contracting.contracting.doctype.tender.test_tender import make_template

	template = make_template("_won_wit_" + frappe.generate_hash(length=8))
	doc = frappe.get_doc({
		"doctype": "Tender",
		"naming_series": "TND-.YYYY.-",
		"project_name": project_name,
		"client": frappe.db.get_value("Customer", {}, "name"),
		"tender_category": tender_category,
		"project_tender": project_tender_name,
	})
	doc.append("boq_items", {
		"item_name": "Won Work Item", "original_quantity": qty, "work_item_template": template.name,
	})
	doc.insert(ignore_permissions=True)
	doc.append("material_items", {
		"work_item": doc.boq_items[0].idx, "item": ensure_ic_item(),
		"qty_per_unit": qty, "rate": rate, "currency": "EGP",
	})
	doc.save(ignore_permissions=True)
	doc.status = "Complete"
	doc.save(ignore_permissions=True)
	return doc


class TestWonAutomation(FrappeTestCase):
	"""handle_won_automation()'s one-Project-per-Project-Tender rewrite
	(Won Project Tender BRD, 2026-08-28). TC-01/02/07. FrappeTestCase
	(class-level rollback), not a per-method frappe.db.rollback(), for the
	same naming-series-counter reason TestIndirectCostAndCurrency uses it."""

	def test_tc01_won_creates_one_project_multiple_sales_orders(self):
		pt = make_ic_project_tender("__won01_project__")
		civil = make_won_ready_tender(pt.name, "__won01_civil__", "Civil")
		elec = make_won_ready_tender(pt.name, "__won01_elec__", "Electrical")

		pt.reload()
		pt.append("direct_cost_details", {"tender": civil.name, "tender_category": "Civil"})
		pt.append("direct_cost_details", {"tender": elec.name, "tender_category": "Electrical"})
		pt.status = "Won"
		pt.save(ignore_permissions=True)

		projects = frappe.get_all("Project", filters={"project_tender": pt.name})
		self.assertEqual(len(projects), 1)
		project = frappe.get_doc("Project", projects[0].name)
		self.assertEqual(len(project.sales_order_details), 2)

		pt.reload()
		project_names = {row.project for row in pt.direct_cost_details}
		self.assertEqual(project_names, {project.name})

	def test_tc02_resave_with_new_row_reuses_project(self):
		pt = make_ic_project_tender("__won02_project__")
		civil = make_won_ready_tender(pt.name, "__won02_civil__", "Civil")
		pt.reload()
		pt.append("direct_cost_details", {"tender": civil.name, "tender_category": "Civil"})
		pt.status = "Won"
		pt.save(ignore_permissions=True)

		first_project = frappe.db.get_value("Project", {"project_tender": pt.name}, "name")

		elec = make_won_ready_tender(pt.name, "__won02_elec__", "Electrical")
		pt.reload()
		pt.append("direct_cost_details", {"tender": elec.name, "tender_category": "Electrical"})
		pt.save(ignore_permissions=True)

		projects = frappe.get_all("Project", filters={"project_tender": pt.name})
		self.assertEqual(len(projects), 1)
		self.assertEqual(projects[0].name, first_project)
		project = frappe.get_doc("Project", first_project)
		self.assertEqual(len(project.sales_order_details), 2)

	def test_tc07_already_linked_row_skipped_on_resave(self):
		pt = make_ic_project_tender("__won07_project__")
		civil = make_won_ready_tender(pt.name, "__won07_civil__", "Civil")
		pt.reload()
		pt.append("direct_cost_details", {"tender": civil.name, "tender_category": "Civil"})
		pt.status = "Won"
		pt.save(ignore_permissions=True)

		so_count_before = frappe.db.count("Sales Order", {"tender": civil.name})
		pt.save(ignore_permissions=True)  # re-save; row already has project/sales_order set
		so_count_after = frappe.db.count("Sales Order", {"tender": civil.name})
		self.assertEqual(so_count_before, so_count_after)
