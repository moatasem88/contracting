# Copyright (c) 2026, kazem and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

INDIRECT_SECTION = "Indirect Cost"
OVERHEAD_SECTION = "Overhead & Profit"
DEDUCTION_SECTION = "Deduction"


class ProjectTender(Document):
	def validate(self):
		self.validate_direct_cost_details()
		self.calculate_direct_cost()
		self.calculate_indirect_cost()
		self.calculate_overhead_and_deductions()
		self.solve_percentage_of_total_price()
		self.calculate_percent_of_total_price()
		self.calculate_total_addition_percent()
		self.calculate_section_percentages()
		self.validate_won_preconditions()

	def on_update(self):
		self.handle_won_automation()
		if not self.flags.get("skip_cross_sync"):
			from contracting.contracting.utils.cross_sync import sync_project_tender_to_tenders

			sync_project_tender_to_tenders(self)

	def validate_direct_cost_details(self):
		seen_tenders = set()
		for row in self.direct_cost_details:
			if not row.tender:
				continue

			if row.tender in seen_tenders:
				frappe.throw(
					_("Tender {0} is added more than once in Direct Cost Details.").format(row.tender)
				)
			seen_tenders.add(row.tender)

			tender_category, tender_project_tender = frappe.db.get_value(
				"Tender", row.tender, ["tender_category", "project_tender"]
			)
			if row.tender_category and tender_category and row.tender_category != tender_category:
				frappe.throw(
					_("Tender {0} belongs to category {1}, not {2}.").format(
						row.tender, tender_category, row.tender_category
					)
				)
			# Doesn't auto-set Tender.project_tender - FR-18 keeps that
			# field independently settable, not inferred from table
			# membership. This only backstops the picker filter (which
			# already excludes Tenders claimed elsewhere) against
			# API/bench-console inserts that bypass it.
			if tender_project_tender and tender_project_tender != self.name:
				frappe.throw(
					_("Tender {0} is already linked to Project Tender {1}.").format(
						row.tender, tender_project_tender
					)
				)

			tender_final_cost, tender_sell_amount = frappe.db.get_value(
				"Tender", row.tender, ["tender_final_cost", "sell_amount"]
			)
			row.tender_total = tender_final_cost or 0
			row.tender_sell_amount = tender_sell_amount or 0

	def calculate_direct_cost(self):
		self.total_direct_cost = sum(flt(row.tender_total) for row in self.direct_cost_details)
		for row in self.direct_cost_details:
			row.percent_of_direct_cost = (
				flt(row.tender_total) / self.total_direct_cost * 100 if self.total_direct_cost else 0
			)

	def cost_component_sections(self):
		"""Batched section lookup for every Cost Component referenced across
		both tables, instead of one query per row."""
		names = {
			row.cost_component
			for row in list(self.indirect_cost_details) + list(self.overhead_deduction_details)
			if row.cost_component
		}
		if not names:
			return {}
		return {
			d.name: d.section
			for d in frappe.get_all(
				"Tender Cost Component", filters={"name": ["in", list(names)]}, fields=["name", "section"]
			)
		}

	def resolve_row_defaults(self, row):
		"""Pre-fills calculation_type/rate from the Cost Component master,
		but only the moment calculation_type itself is still genuinely
		blank - i.e. a fresh, unconfigured row. Once calculation_type has
		any value (including a value this same method set on an earlier
		save), rate is left alone even if it's 0 - VAT at 0% is a valid,
		deliberate entry (BRD §10 edge case), not a sign the row needs
		re-defaulting."""
		if not row.cost_component or row.calculation_type:
			return
		component = frappe.db.get_value(
			"Tender Cost Component",
			row.cost_component,
			["default_calculation_type", "default_rate"],
			as_dict=True,
		)
		if not component:
			return
		row.calculation_type = component.default_calculation_type
		if not row.rate and component.default_calculation_type != "Fixed Amount":
			row.rate = component.default_rate

	def calculate_indirect_cost(self):
		sections = self.cost_component_sections()
		total = 0.0
		for row in self.indirect_cost_details:
			if row.cost_component and sections.get(row.cost_component) != INDIRECT_SECTION:
				frappe.throw(
					_("{0} is not an Indirect Cost component.").format(row.cost_component)
				)
			self.resolve_row_defaults(row)

			if row.calculation_type == "Percentage of Direct Cost":
				row.amount = flt(row.rate) * self.total_direct_cost / 100
			elif row.calculation_type == "Percentage of Total Price":
				# Solved document-wide in solve_percentage_of_total_price(),
				# once total_sell_price's non-PoTP contributions (K) are
				# fully known - placeholder here so this loop's `total`
				# (which becomes K's indirect-cost component) excludes it.
				row.amount = 0
			elif row.calculation_type == "Fixed Amount":
				# rate is back-computed for display only.
				row.rate = flt(row.amount) / self.total_direct_cost * 100 if self.total_direct_cost else 0

			total += flt(row.amount)

		self.total_indirect_cost = total
		self.total_direct_and_indirect_cost = self.total_direct_cost + self.total_indirect_cost

		for row in self.indirect_cost_details:
			row.percent_of_direct_cost = (
				flt(row.amount) / self.total_direct_cost * 100 if self.total_direct_cost else 0
			)

	def calculate_overhead_and_deductions(self):
		sections = self.cost_component_sections()

		# Overhead & Profit rows count toward the sub-total regardless of
		# where they sit in the table (FR-16) - unlike Deduction rows below.
		overhead_total = 0.0
		for row in self.overhead_deduction_details:
			section = sections.get(row.cost_component) if row.cost_component else None
			if section not in (OVERHEAD_SECTION, DEDUCTION_SECTION) and row.cost_component:
				frappe.throw(
					_("{0} is not an Overhead & Profit or Deduction component.").format(row.cost_component)
				)
			if section != OVERHEAD_SECTION:
				continue
			self.resolve_row_defaults(row)
			if row.calculation_type == "Percentage of Direct Cost":
				row.amount = flt(row.rate) * self.total_direct_cost / 100
			elif row.calculation_type == "Percentage of Total Price":
				# Placeholder, solved in solve_percentage_of_total_price() -
				# see the identical note in calculate_indirect_cost().
				row.amount = 0
			# Fixed Amount: row.amount was entered directly, left as-is.
			overhead_total += flt(row.amount)

		self.sub_total_overhead_and_profit = self.total_direct_and_indirect_cost + overhead_total

		# Deduction rows: Fixed Amount is entered directly; Percentage of
		# Total Price is placeholder-zeroed here too (solved below,
		# document-wide - no longer table-position-dependent).
		deduction_total = 0.0
		for row in self.overhead_deduction_details:
			section = sections.get(row.cost_component) if row.cost_component else None
			if section != DEDUCTION_SECTION:
				continue
			self.resolve_row_defaults(row)
			if row.calculation_type == "Percentage of Total Price":
				row.amount = 0
			# Fixed Amount: row.amount was entered directly, left as-is.
			deduction_total += flt(row.amount)

		self.total_sell_price = self.sub_total_overhead_and_profit + deduction_total

		for row in self.overhead_deduction_details:
			row.percent_of_direct_cost = (
				flt(row.amount) / self.total_direct_cost * 100 if self.total_direct_cost else 0
			)

	def solve_percentage_of_total_price(self):
		"""Replaces the old table-position-dependent "Percentage of Running
		Total" with a document-wide, self-referencing solve. With every
		"Percentage of Total Price" ("PoTP") row placeholder-zeroed by
		calculate_indirect_cost()/calculate_overhead_and_deductions() above,
		self.total_sell_price at this point already equals exactly K -
		every non-PoTP contribution (Direct Cost, every Fixed Amount row,
		every Percentage-of-Direct-Cost row) - for free. Solving
		total_sell_price = K / (1 - R) and writing each PoTP row's real
		amount is then a single, cheap, order-independent pass."""
		sections = self.cost_component_sections()
		potp_indirect = [
			r for r in self.indirect_cost_details if r.calculation_type == "Percentage of Total Price"
		]
		potp_overhead_deduction = [
			r for r in self.overhead_deduction_details if r.calculation_type == "Percentage of Total Price"
		]
		all_potp = potp_indirect + potp_overhead_deduction
		if not all_potp:
			return

		r_total = sum(flt(r.rate) for r in all_potp) / 100
		if r_total >= 1:
			names = [r.cost_component or _("Row #{0}").format(r.idx) for r in all_potp]
			frappe.throw(
				_("Combined 'Percentage of Total Price' rate is {0}%, which must be under 100%. "
					"Offending rows: {1}").format(round(r_total * 100, 2), ", ".join(names))
			)

		k = self.total_sell_price
		total_sell_price = k / (1 - r_total)

		indirect_delta = 0.0
		overhead_delta = 0.0
		for row in potp_indirect:
			row.amount = flt(row.rate) * total_sell_price / 100
			indirect_delta += row.amount
		for row in potp_overhead_deduction:
			row.amount = flt(row.rate) * total_sell_price / 100
			section = sections.get(row.cost_component) if row.cost_component else None
			if section != DEDUCTION_SECTION:
				overhead_delta += row.amount

		self.total_indirect_cost += indirect_delta
		self.total_direct_and_indirect_cost += indirect_delta
		self.sub_total_overhead_and_profit += indirect_delta + overhead_delta
		self.total_sell_price = total_sell_price

		for row in potp_indirect + potp_overhead_deduction:
			row.percent_of_direct_cost = (
				flt(row.amount) / self.total_direct_cost * 100 if self.total_direct_cost else 0
			)

	def calculate_percent_of_total_price(self):
		for row in self.direct_cost_details:
			row.percent_of_total_price = (
				flt(row.tender_total) / self.total_sell_price * 100 if self.total_sell_price else 0
			)
		for row in list(self.indirect_cost_details) + list(self.overhead_deduction_details):
			row.percent_of_total_price = (
				flt(row.amount) / self.total_sell_price * 100 if self.total_sell_price else 0
			)

	def calculate_total_addition_percent(self):
		self.total_addition_percent = (
			(self.total_sell_price - self.total_direct_cost) / self.total_direct_cost * 100
			if self.total_direct_cost else 0
		)

	def calculate_section_percentages(self):
		self.direct_cost_percent_of_total_price = (
			flt(self.total_direct_cost) / self.total_sell_price * 100 if self.total_sell_price else 0
		)
		self.indirect_cost_percent_of_direct_cost = (
			flt(self.total_indirect_cost) / self.total_direct_cost * 100 if self.total_direct_cost else 0
		)
		self.indirect_cost_percent_of_total_price = (
			flt(self.total_indirect_cost) / self.total_sell_price * 100 if self.total_sell_price else 0
		)
		overhead_deduction_total = flt(self.total_sell_price) - flt(self.total_direct_and_indirect_cost)
		self.overhead_deduction_percent_of_direct_cost = (
			overhead_deduction_total / self.total_direct_cost * 100 if self.total_direct_cost else 0
		)
		self.overhead_deduction_percent_of_total_price = (
			overhead_deduction_total / self.total_sell_price * 100 if self.total_sell_price else 0
		)

	def validate_won_preconditions(self):
		"""Everything each linked Tender's Sales Order will need, checked
		here rather than in the builder. handle_won_automation runs from
		on_update, inside this save's transaction - a throw from there
		rolls the save back and surfaces an opaque Sales Order error, after
		a Project has already been created. Checking up front means
		nothing is half-created and the message names the actual row and
		Tender at fault."""
		if self.status != "Won":
			return

		for row in self.direct_cost_details:
			if not row.tender or row.project:
				continue

			tender_doc = frappe.get_doc("Tender", row.tender)

			if tender_doc.status != "Complete":
				frappe.throw(
					_("Tender {0} (Direct Cost Detail row) is not Complete yet, so its pricing "
						"isn't final. Mark it Complete before this Project Tender can be Won.").format(
						row.tender
					)
				)

			if not tender_doc.client:
				frappe.throw(
					_("Tender {0}: Client is required before it can be part of a Won Project Tender - "
						"the Sales Order is raised against them.").format(row.tender)
				)

			for boq_row in tender_doc.boq_items:
				if boq_row.is_group:
					continue

				if not boq_row.item_code:
					frappe.throw(
						_("Tender {0}, BOQ row #{1} ({2}) has no Item. Pick a Work Item Template on "
							"that row - the template's Item is what the Sales Order line is raised "
							"against.").format(row.tender, boq_row.idx, boq_row.item_name or _("unnamed"))
					)

				if not flt(boq_row.original_quantity):
					frappe.throw(
						_("Tender {0}, BOQ row #{1} ({2}) has no quantity - a Sales Order line cannot "
							"be zero-quantity.").format(row.tender, boq_row.idx, boq_row.item_name or _("unnamed"))
					)

				# Sales Order runs validate_uom_is_integer, which Contract
				# Document never did. Catch it here, where the row is
				# visible, instead of letting it surface against the
				# generated order.
				uom = boq_row.uom or frappe.db.get_value("Item", boq_row.item_code, "stock_uom")
				if uom and frappe.db.get_value("UOM", uom, "must_be_whole_number"):
					qty = flt(boq_row.original_quantity)
					if qty != int(qty):
						frappe.throw(
							_("Tender {0}, BOQ row #{1} ({2}) has quantity {3}, but UOM {4} only "
								"allows whole numbers.").format(
								row.tender, boq_row.idx, boq_row.item_name or _("unnamed"), qty, uom
							)
						)

	def handle_won_automation(self):
		"""Won automation runs per linked Tender, not once for the whole
		Project Tender - a Project Tender can bundle several trade
		Tenders, and each one gets its own Project + Sales Order.
		Idempotent per row: row.project already set skips that row on a
		later save, so re-saving an already-Won Project Tender with a
		newly added row only processes the new row."""
		if self.status != "Won":
			return

		for row in self.direct_cost_details:
			if row.tender and not row.project:
				self.create_project_and_sales_order_for_row(row)

	def create_project_and_sales_order_for_row(self, row):
		"""One Project per Project Tender, not per Tender: reuses an
		existing Project already linked to this Project Tender (via the
		project_tender field) before creating another, so a multi-trade
		Project Tender (Civil/Electrical/Elevators) ends up with one
		Project and one Sales Order per Tender under it, instead of one of
		each per Tender."""
		from contracting.contracting.sales_order_from_tender import create_sales_order_from_tender

		tender_doc = frappe.get_doc("Tender", row.tender)

		existing_project = frappe.db.exists("Project", {"project_tender": self.name})
		if existing_project:
			project = frappe.get_doc("Project", existing_project)
		else:
			project = frappe.get_doc({
				"doctype": "Project",
				"project_name": self.project_name,
				"customer": self.client,
				"project_type": self.project_type,
				"project_tender": self.name,
			}).insert(ignore_permissions=True)

		# create_sales_order_from_tender reads tender.project - Tender no
		# longer owns that field, so it's set in-memory only, just for the
		# duration of this call.
		tender_doc.project = project.name
		sales_order = create_sales_order_from_tender(tender_doc)

		if not existing_project:
			# Project.sales_order is the native field - it couldn't hold a
			# Contract Document name, which is the only reason the custom
			# Project.contract_document field was ever added. Now that one
			# Project can have several Sales Orders (sales_order_details
			# below), this singular field is set once, to whichever Sales
			# Order is created first, purely for backward compatibility
			# with core reports that key off it - it's hidden on the form
			# since it's no longer authoritative.
			frappe.db.set_value("Project", project.name, "sales_order", sales_order.name)

		frappe.db.set_value(
			"Project Tender Direct Cost Detail",
			row.name,
			{"project": project.name, "sales_order": sales_order.name},
			update_modified=False,
		)
		# Also update the in-memory row, not just the DB - handle_won_automation's
		# `row.tender and not row.project` guard reads this same object, and a
		# second .save() on this same in-memory Project Tender (without an
		# intervening reload) must not re-process this row and create a
		# duplicate Sales Order.
		row.project = project.name
		row.sales_order = sales_order.name

		carry_boq_to_project(tender_doc, project.name)

		# Direct Cost Detail's project/sales_order links were only just
		# committed above - appending here and saving means the Cost &
		# Billing rollup (triggered by this save's own on_update) sees
		# this row's linkage immediately, instead of only after some later
		# unrelated save.
		project.reload()
		project.append("sales_order_details", {
			"tender": tender_doc.name,
			"tender_category": tender_doc.tender_category,
			"sales_order": sales_order.name,
		})
		project.save(ignore_permissions=True)


def carry_boq_to_project(tender_doc, project_name):
	for row in tender_doc.boq_items:
		if row.is_group:
			continue
		frappe.get_doc({
			"doctype": "Project BOQ Item",
			"boq_proj": project_name,
			"source_tender_boq_item": row.name,
			"parent_item": row.item_name,
			"proj_item_quantity": row.original_quantity,
			"project_item_rate": row.sell_rate,
		}).insert(ignore_permissions=True)


@frappe.whitelist()
def preview_totals(project_tender):
	"""Run the same validate()-time rollup against an in-memory, unsaved
	Project Tender (posted as JSON from the form) and return the computed
	totals - so project_tender.js can show live numbers on child-row edits
	without a second, divergent JS re-implementation of the FR-16 calculation.
	Mirrors the lesson CLAUDE.md already documents from tender.js."""
	if isinstance(project_tender, str):
		project_tender = json.loads(project_tender)

	doc = frappe.get_doc(project_tender)
	doc.flags.ignore_permissions = True

	try:
		doc.validate_direct_cost_details()
	except frappe.ValidationError:
		# A preview tolerates an incomplete/invalid in-progress row - only
		# the totals matter here. The real save() still enforces every
		# check via the normal validate() path.
		pass

	doc.calculate_direct_cost()
	doc.calculate_indirect_cost()
	doc.calculate_overhead_and_deductions()
	doc.solve_percentage_of_total_price()
	doc.calculate_percent_of_total_price()
	doc.calculate_total_addition_percent()
	doc.calculate_section_percentages()

	return {
		"total_direct_cost": doc.total_direct_cost,
		"total_indirect_cost": doc.total_indirect_cost,
		"total_direct_and_indirect_cost": doc.total_direct_and_indirect_cost,
		"sub_total_overhead_and_profit": doc.sub_total_overhead_and_profit,
		"total_addition_percent": doc.total_addition_percent,
		"direct_cost_percent_of_total_price": doc.direct_cost_percent_of_total_price,
		"indirect_cost_percent_of_direct_cost": doc.indirect_cost_percent_of_direct_cost,
		"indirect_cost_percent_of_total_price": doc.indirect_cost_percent_of_total_price,
		"overhead_deduction_percent_of_direct_cost": doc.overhead_deduction_percent_of_direct_cost,
		"overhead_deduction_percent_of_total_price": doc.overhead_deduction_percent_of_total_price,
		"total_sell_price": doc.total_sell_price,
		"direct_cost_details": [row.as_dict() for row in doc.direct_cost_details],
		"indirect_cost_details": [row.as_dict() for row in doc.indirect_cost_details],
		"overhead_deduction_details": [row.as_dict() for row in doc.overhead_deduction_details],
	}
