# Copyright (c) 2026, kazem and Contributors
# See license.txt

"""2026-10-20: how a contract line's price and its cost components relate.

Total mode - the entered price is split over the line's components in the
proportions of their Tender budget; the split rates are editable
placeholders that must still add up to the line amount.
Components mode - the price is the rollup of the components.
Either way, each component is held to its own Tender rate, as well as the
line to its total ceiling.

Fixture work item W (qty 10), Installing pools per unit:
    Labor 1 x 50,  Subcontract 2 x 30   -> ceiling 110 per unit
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.doctype.contractor_contract_addendum.test_contractor_contract_addendum import (
	approve_addendum,
	make_addendum,
	make_amendable_contract,
)
from contracting.contracting.doctype.subcontractor_contract.subcontractor_contract import (
	get_component_rates,
)
from contracting.contracting.doctype.subcontractor_contract.test_subcontract_resource import (
	make_four_resource_tender,
)
from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	add_tender_line,
	make_draft_contract,
)


def make_installing_line(suffix, unit_price=55, **line):
	project, _tender, boq_row = make_four_resource_tender("cp_" + suffix)
	contract = make_draft_contract(project, "Installing")
	add_tender_line(contract, boq_row, 4, unit_price)
	contract.contracted_items[0].update(line)
	return contract


def reset_modified(contract):
	# a failed save bumps the in-memory timestamp
	contract.modified = frappe.db.get_value(contract.doctype, contract.name, "modified")


class TestComponentPricing(FrappeTestCase):
	def test_total_price_split_in_tender_proportions(self):
		"""4 x 55 = 220, split 50 : 60 -> labor 100 over 4 units, subcontract
		120 over 8 units."""
		contract = make_installing_line("split")
		contract.insert(ignore_permissions=True)
		labor, sub = contract.contract_labor_items[0], contract.contract_subcontract_items[0]
		self.assertAlmostEqual(flt(labor.rate), 25.0, places=6)
		self.assertAlmostEqual(flt(sub.rate), 15.0, places=6)
		self.assertAlmostEqual(flt(labor.amount) + flt(sub.amount), 220.0, places=6)

	def test_client_split_returns_rates_only(self):
		"""What the form calls: the same rates, and nothing else - no Tender
		rates or proportions."""
		contract = make_installing_line("client")
		rows = [
			{"table": "contract_labor_items", "link": contract.contract_labor_items[0].labor_item,
			 "source": "Tender", "qty": 4},
			{"table": "contract_subcontract_items", "link": contract.contract_subcontract_items[0].subcontract_item,
			 "source": "Tender", "qty": 8},
		]
		rates = get_component_rates(json.dumps(rows), 220)
		self.assertEqual(len(rates), 2)
		self.assertAlmostEqual(rates[0], 25.0, places=6)
		self.assertAlmostEqual(rates[1], 15.0, places=6)

	def test_placeholders_editable_but_must_add_up(self):
		contract = make_installing_line("edit")
		contract.insert(ignore_permissions=True)
		labor, sub = contract.contract_labor_items[0], contract.contract_subcontract_items[0]

		labor.rate, sub.rate = 30, 12.5  # 120 + 100 = 220
		contract.save(ignore_permissions=True)
		self.assertEqual((flt(labor.rate), flt(sub.rate)), (30.0, 12.5))

		labor.rate = 31
		with self.assertRaisesRegex(frappe.ValidationError, "add up to the line total"):
			contract.save(ignore_permissions=True)

	def test_component_above_tender_rate_allowed_within_total(self):
		"""Labor at 51 is over its own Tender rate of 50, but the line (55)
		stays under its 110 ceiling - the saving on subcontract covers it, so
		it saves (the component is still flagged red)."""
		contract = make_installing_line("component_within")
		contract.insert(ignore_permissions=True)
		contract.contract_labor_items[0].rate = 51  # 204
		contract.contract_subcontract_items[0].rate = 2  # 16 -> 220
		contract.save(ignore_permissions=True)
		self.assertEqual(contract.contract_labor_items[0].out_of_tender_scope, 1)
		self.assertEqual(contract.contracted_items[0].out_of_tender_scope, 0)

	def test_component_above_tender_rate_and_total_over_blocked(self):
		"""Labor raised to 70 lifts the line to 4 x 115 = 460, over the 110
		ceiling - refused, naming labor, until delegated to Cost Control."""
		contract = make_installing_line("component_over")
		contract.insert(ignore_permissions=True)
		contract.contracted_items[0].unit_price = 115
		contract.contract_labor_items[0].rate = 70  # 280
		contract.contract_subcontract_items[0].rate = 22.5  # 180 -> 460
		with self.assertRaisesRegex(frappe.ValidationError, "_Test SC Labor.*priced above its Tender rate"):
			contract.save(ignore_permissions=True)

		reset_modified(contract)
		contract.price_exception_acknowledged = 1
		contract.save(ignore_permissions=True)
		self.assertEqual(contract.contracted_items[0].out_of_tender_scope, 1)

	def test_line_above_total_ceiling_still_blocked(self):
		contract = make_installing_line("total_ceiling", unit_price=111)
		with self.assertRaisesRegex(frappe.ValidationError, "approved ceiling for that work item"):
			contract.insert(ignore_permissions=True)

	def test_components_mode_rolls_up(self):
		contract = make_installing_line("components", pricing_mode="Components")
		contract.contract_labor_items[0].rate = 40
		contract.contract_subcontract_items[0].rate = 20
		contract.insert(ignore_permissions=True)
		# (4 x 40 + 8 x 20) / 4
		self.assertAlmostEqual(flt(contract.contracted_items[0].unit_price), 80.0, places=6)


class TestAddendumComponentPricing(FrappeTestCase):
	def test_update_scales_components_with_the_new_price(self):
		"""A Labor line 5 @ 50 (labor 1 x 5 @ 50) updated to 8 @ 40: the
		labor row keeps adding up to the line - 8 @ 40."""
		contract, _boq_row = make_amendable_contract("cp_update")
		self.assertAlmostEqual(flt(contract.contract_labor_items[0].rate), 50.0, places=6)
		addendum = make_addendum(contract, [{
			"base_contract_item": contract.contracted_items[0].name, "action": "Update",
			"new_qty": 8, "new_unit_price": 40,
		}])
		addendum.insert(ignore_permissions=True)
		approve_addendum(addendum)

		contract.reload()
		labor = contract.contract_labor_items[0]
		self.assertEqual(flt(labor.qty), 8.0)
		self.assertAlmostEqual(flt(labor.rate), 40.0, places=6)
		self.assertAlmostEqual(flt(labor.amount), 320.0, places=6)

	def test_added_line_is_split(self):
		contract, boq_row = make_amendable_contract("cp_add", qty=5)
		addendum = make_addendum(contract, [{"work_item": boq_row.name, "qty_delta": 2, "unit_price": 45}])
		addendum.insert(ignore_permissions=True)
		approve_addendum(addendum)

		contract.reload()
		new_row = contract.contracted_items[-1]
		owned = [r for r in contract.contract_labor_items if r.contract_row == new_row.name]
		self.assertTrue(owned)
		self.assertAlmostEqual(sum(flt(r.qty) * flt(r.rate) for r in owned), 90.0, places=6)


class TestComponentQtyCap(FrappeTestCase):
	"""A component's qty may be lowered by hand, never raised above
	qty_per_unit x the line qty (labor 1 x 4, subcontract 2 x 4 here)."""

	def test_lower_qty_saves(self):
		contract = make_installing_line("qty_lower")
		contract.insert(ignore_permissions=True)
		labor, sub = contract.contract_labor_items[0], contract.contract_subcontract_items[0]
		labor.qty, labor.auto_propagated = 3, 0
		sub.rate = (220 - 3 * flt(labor.rate)) / flt(sub.qty)
		contract.save(ignore_permissions=True)
		self.assertEqual(flt(contract.contract_labor_items[0].qty), 3.0)

	def test_qty_above_line_share_blocked(self):
		contract = make_installing_line("qty_over")
		contract.insert(ignore_permissions=True)
		sub = contract.contract_subcontract_items[0]
		self.assertEqual(flt(sub.qty), 8.0)
		sub.qty, sub.auto_propagated = 9, 0
		with self.assertRaisesRegex(frappe.ValidationError, "never raised above"):
			contract.save(ignore_permissions=True)

	def test_lowering_line_qty_catches_hand_edited_component(self):
		"""A hand-edited row isn't rescaled, so lowering the line under it is
		caught too."""
		contract = make_installing_line("qty_line")
		contract.insert(ignore_permissions=True)
		contract.contract_labor_items[0].auto_propagated = 0
		contract.save(ignore_permissions=True)
		contract.contracted_items[0].qty = 2  # labor still 4 > 1 x 2
		with self.assertRaisesRegex(frappe.ValidationError, "never raised above"):
			contract.save(ignore_permissions=True)

	def test_client_max_qty(self):
		from contracting.contracting.doctype.subcontractor_contract.subcontractor_contract import get_component_max_qty
		contract = make_installing_line("qty_client")
		row = json.dumps({"subcontract_item": contract.contract_subcontract_items[0].subcontract_item})
		self.assertAlmostEqual(flt(get_component_max_qty("contract_subcontract_items", row, 4)), 8.0, places=6)
