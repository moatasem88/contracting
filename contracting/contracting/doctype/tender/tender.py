# Copyright (c) 2026, kazem and contributors
# For license information, please see license.txt

import json

import erpnext
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


RESOURCE_TABLE_FIELDS = ("material_items", "labor_items", "equipment_items")


class Tender(Document):
	def validate(self):
		self.validate_not_complete_locked()
		self.validate_currency_table()
		self.calculate_resource_quantities()
		self.rollup_boq_costs()
		self.evaluate_auto_progress()
		self.calculate_row_sell_pricing()

	def on_update(self):
		from contracting.contracting.utils.cross_sync import sync_tender_to_project_tenders

		sync_tender_to_project_tenders(self)

	def validate_not_complete_locked(self):
		"""Once Complete, a Tender is locked outright - the award pipeline
		(Won/Lost/Project/Sales Order) now lives on Project Tender, so
		there's no sales_order to key the old lock off any more. Re-bidding
		goes through Create Revised Version instead, which clones the BOQ
		into a new Draft Tender; this one stays Complete."""
		if self.is_new():
			return

		before = self.get_doc_before_save()
		if not before or before.status != "Complete":
			return

		if self.as_dict() != before.as_dict():
			frappe.throw(
				_("This Tender is Complete and can no longer be edited. Use Create Revised Version to re-bid."),
				title=_("Tender Complete"),
			)

	def evaluate_auto_progress(self):
		"""Draft -> In Progress fires automatically once any real work item
		is priced (has an Item and a nonzero rolled-up amount). Never fires
		backward, and never advances past Draft on its own - Complete is a
		deliberate, Tender-Manager-only action via mark_tender_complete."""
		if self.status != "Draft":
			return

		has_priced_item = any(
			row.item_code and flt(row.total_amount) for row in self.boq_items if not row.is_group
		)
		if has_priced_item:
			self.status = "In Progress"

	def base_currency(self):
		return erpnext.get_company_currency(erpnext.get_default_company())

	def validate_currency_table(self):
		seen = set()
		for row in self.currency_table:
			if row.currency in seen:
				frappe.throw(
					_("Currency {0} is listed more than once in the Currencies table.").format(row.currency)
				)
			seen.add(row.currency)

	def resolve_exchange_rate(self, currency, row_label):
		base_currency = self.base_currency()
		if not currency or currency == base_currency:
			return 1.0

		for currency_row in self.currency_table:
			if currency_row.currency == currency:
				return flt(currency_row.exchange_rate) or 1.0

		frappe.throw(
			_("{0} is priced in {1}, which isn't listed in this Tender's Currencies table.").format(
				row_label, currency
			)
		)

	def calculate_resource_quantities(self):
		# Material/Labor/Equipment tabs are the real editable input now -
		# qty is derived from the work item's own quantity, not entered
		# directly (qty_per_unit is what was copied from the template).
		# work_item references the BOQ row by its number (idx), not its
		# name - a new BOQ row's name isn't final until save, but its
		# row number is stable from the moment it's added.
		boq_by_idx = {row.idx: row for row in self.boq_items}
		safety_multiplier = 1 + flt(self.safety_factor_percent) / 100.0
		indirect_multiplier = flt(self.propagated_addition_percent) / 100.0
		for fieldname in RESOURCE_TABLE_FIELDS:
			for row in self.get(fieldname):
				work_item = boq_by_idx.get(row.work_item)
				parent_qty = work_item.original_quantity if work_item else 0
				row.qty = (row.qty_per_unit or 0) * (parent_qty or 0)
				# exchange_rate is resolved server-side, never trusted from
				# the client, so a row's amount can't drift from a stale
				# rate a browser last saw - also what blocks the "currency
				# removed from currency_table while still referenced" edge
				# case, since the row simply fails this same lookup.
				row.exchange_rate = self.resolve_exchange_rate(
					row.currency, _("Row #{0}").format(row.idx)
				)
				# Cost layers, broken out for auditability - direct_cost_amount
				# is the raw pre-Safety-Factor cost; amount (Safety-Factor-
				# inclusive) is unchanged in value from before this split,
				# algebraically identical to qty*rate*exchange_rate*safety_multiplier.
				row.direct_cost_amount = row.qty * (row.rate or 0) * (row.exchange_rate or 1)
				row.safety_factor_amount = row.direct_cost_amount * (safety_multiplier - 1)
				pre_indirect_amount = row.direct_cost_amount + row.safety_factor_amount
				# Indirect Cost is applied after Safety Factor, against the
				# already-loaded amount, per the confirmed base - and, as of
				# the 08-28 BRD, folded into amount itself so the selling
				# price (sell_rate/sell_amount, downstream) is Direct +
				# Safety + Indirect, not just the first two layers.
				row.indirect_cost_amount = pre_indirect_amount * indirect_multiplier
				row.amount = pre_indirect_amount + row.indirect_cost_amount
				row.amount_currency = row.qty * (row.rate or 0)
				row.rate_egp = (row.rate or 0) * (row.exchange_rate or 1)
				# Pin the BOQ row's stable name alongside the idx pointer.
				# Contractor Contract reconciliation resolves through this,
				# so reordering or deleting BOQ rows can't silently
				# re-point an already-contracted resource line.
				row.boq_row_id = work_item.name if work_item else None

	def rollup_boq_costs(self):
		# Tender BOQ Item no longer carries its own rate - total_amount is
		# a pure rollup of whatever's linked to it across the three
		# resource tabs via `work_item`.
		totals_by_work_item = {}
		# Per-work-item cost layers, split by resource type - feeds the
		# material_cost/labor_cost/equipment_cost/boq_* rollup fields.
		layer_totals_by_work_item = {}
		table_layer_key = {
			"material_items": "material",
			"labor_items": "labor",
			"equipment_items": "equipment",
		}
		for fieldname in RESOURCE_TABLE_FIELDS:
			layer_key = table_layer_key[fieldname]
			for row in self.get(fieldname):
				totals_by_work_item[row.work_item] = (
					totals_by_work_item.get(row.work_item, 0.0) + (row.amount or 0)
				)
				layers = layer_totals_by_work_item.setdefault(
					row.work_item,
					{"material": 0.0, "labor": 0.0, "equipment": 0.0, "safety": 0.0, "indirect": 0.0},
				)
				layers[layer_key] += row.direct_cost_amount or 0
				layers["safety"] += row.safety_factor_amount or 0
				layers["indirect"] += row.indirect_cost_amount or 0

		# Header totals must be authoritative here, not only in tender.js -
		# tender.js's recompute on load previously diverged from whatever
		# was last stored (these 4 fields were client-only), which marked
		# the form dirty before the user touched anything.
		subtotal = 0.0
		total_vat = 0.0
		total_additions = 0.0

		for row in self.boq_items:
			if row.is_group:
				continue
			base_cost = totals_by_work_item.get(row.idx, 0.0)
			row.total_amount = base_cost
			vat = base_cost * (row.vat_percentage or 0) / 100.0
			other = base_cost * (row.other_additions_pct or 0) / 100.0
			fixed = row.fixed_additions or 0
			effective_total = base_cost + vat + other + fixed
			row.effective_unit_price = (
				effective_total / row.original_quantity if row.original_quantity else 0.0
			)
			subtotal += base_cost
			total_vat += vat
			total_additions += other + fixed

			layers = layer_totals_by_work_item.get(
				row.idx, {"material": 0.0, "labor": 0.0, "equipment": 0.0, "safety": 0.0, "indirect": 0.0}
			)
			row.material_cost = layers["material"]
			row.labor_cost = layers["labor"]
			row.equipment_cost = layers["equipment"]
			row.boq_direct_cost = layers["material"] + layers["labor"] + layers["equipment"]
			row.boq_safety_factor_amount = layers["safety"]
			row.boq_indirect_cost_amount = layers["indirect"]
			row.indirect_cost_percent = self.propagated_addition_percent
			if row.display_currency:
				rate = self.resolve_exchange_rate(
					row.display_currency, _("BOQ row #{0}").format(row.idx)
				)
				row.display_amount = base_cost / rate if rate else 0.0
			else:
				row.display_amount = 0.0

		self.subtotal = subtotal
		self.total_vat = total_vat
		self.total_additions = total_additions
		self.grand_total = subtotal + total_vat + total_additions

	def calculate_row_sell_pricing(self):
		"""Per-BOQ-row sell price - still consumed directly by
		create_sales_order_from_tender/carry_boq_to_project (now in
		project_tender.py) when the linked Project Tender goes Won.
		margin_percent has no blanket/default source any more since
		blanket_margin_percent moved off Tender - every row's margin is
		now always a deliberate manual entry."""
		for row in self.boq_items:
			if row.is_group:
				continue
			row.sell_rate = (row.effective_unit_price or 0) * (1 + (row.margin_percent or 0) / 100.0)
			row.sell_amount = row.sell_rate * (row.original_quantity or 0)


@frappe.whitelist()
def preview_resource_totals(tender):
	"""Runs calculate_resource_quantities()/rollup_boq_costs() against an
	in-memory, unsaved Tender (posted as JSON from the form) and returns the
	computed cost-layer/rollup fields - so tender.js can preview them on
	child-row edits without a second, divergent JS re-implementation of the
	formula (NFR-02). Mirrors Project Tender's own preview_totals."""
	if isinstance(tender, str):
		tender = json.loads(tender)

	doc = frappe.get_doc(tender)
	doc.flags.ignore_permissions = True

	doc.calculate_resource_quantities()
	doc.rollup_boq_costs()

	return {
		"boq_items": [r.as_dict() for r in doc.boq_items],
		"material_items": [r.as_dict() for r in doc.material_items],
		"labor_items": [r.as_dict() for r in doc.labor_items],
		"equipment_items": [r.as_dict() for r in doc.equipment_items],
	}


LIVE_SYNC_FIELDS = {
	"Tender BOQ Item": (
		"vat_percentage", "other_additions_pct", "fixed_additions", "margin_percent",
		"original_quantity", "display_currency",
		"total_amount", "effective_unit_price", "sell_rate", "sell_amount",
		"material_cost", "labor_cost", "equipment_cost", "boq_direct_cost",
		"boq_safety_factor_amount", "boq_indirect_cost_amount",
		"indirect_cost_percent", "display_amount",
	),
	"Tender Material Item": (
		"work_item", "qty_per_unit", "currency", "rate",
		"qty", "exchange_rate", "direct_cost_amount", "safety_factor_amount",
		"amount", "indirect_cost_amount", "amount_currency", "rate_egp", "boq_row_id",
	),
}
LIVE_SYNC_FIELDS["Tender Labor Item"] = LIVE_SYNC_FIELDS["Tender Material Item"]
LIVE_SYNC_FIELDS["Tender Equipment Item"] = LIVE_SYNC_FIELDS["Tender Material Item"]

LIVE_SYNC_CHILD_TABLES = {
	"boq_items": "Tender BOQ Item",
	"material_items": "Tender Material Item",
	"labor_items": "Tender Labor Item",
	"equipment_items": "Tender Equipment Item",
}


@frappe.whitelist()
def sync_live_edits(tender):
	"""Background counterpart to a real save, used by tender_expand_view.js's
	throttled autosync. Persists only the fields in LIVE_SYNC_FIELDS - raw
	user input plus what calculate_resource_quantities()/rollup_boq_costs()/
	calculate_row_sell_pricing() derive from it - and re-broadcasts doc_update
	the same way a real save's on_update -> notify_update() would.

	Deliberately skips validate()'s other side effects (evaluate_auto_progress,
	validate_currency_table, cross_sync, handle_won_automation) and any row
	insert/delete - those stay on the checkpoint frm.save() path in
	tender_expand_view.js so this can run every ~2s without freezing the UI or
	re-running business logic that shouldn't fire on every keystroke.
	"""
	if isinstance(tender, str):
		tender = json.loads(tender)
	tender_name = tender.get("name")

	# Tender's permissions live in tabCustom DocPerm on this site, which
	# shadows tender.json's permissions block outright once any Custom DocPerm
	# row exists (it does here) - has_permission resolves through whichever
	# table is actually authoritative, so this is correct as-is; editing
	# tender.json's permissions would have no effect.
	if not tender_name or not frappe.has_permission("Tender", "write", doc=tender_name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	if frappe.db.get_value("Tender", tender_name, "status") == "Complete":
		return {}

	doc = frappe.get_doc(tender)
	doc.calculate_resource_quantities()
	doc.rollup_boq_costs()
	doc.calculate_row_sell_pricing()

	_persist_live_edit_fields(doc, tender_name)

	saved = frappe.get_doc("Tender", tender_name)
	saved.notify_update()
	return {
		"modified": saved.modified,
		"subtotal": doc.subtotal, "total_vat": doc.total_vat,
		"total_additions": doc.total_additions, "grand_total": doc.grand_total,
	}


def _persist_live_edit_fields(doc, tender_name):
	for fieldname, child_doctype in LIVE_SYNC_CHILD_TABLES.items():
		valid_names = set(frappe.get_all(
			child_doctype, filters={"parent": tender_name}, pluck="name",
		))
		allowed = LIVE_SYNC_FIELDS[child_doctype]
		for row in doc.get(fieldname):
			# Excludes unsaved new rows (local names, not in valid_names) and
			# any row a crafted payload claims but that doesn't actually
			# belong to this Tender - child tables have no permission model
			# of their own (see api/link.py), so this check is load-bearing,
			# not defensive dead code.
			if row.name not in valid_names:
				continue
			frappe.db.set_value(
				child_doctype, row.name,
				{f: row.get(f) for f in allowed}, update_modified=False,
			)

	frappe.db.set_value(
		"Tender", tender_name,
		{"subtotal": doc.subtotal, "total_vat": doc.total_vat,
			"total_additions": doc.total_additions, "grand_total": doc.grand_total},
	)


@frappe.whitelist()
def add_boq_item_from_template(tender_name, template_name, original_quantity=1):
	"""Atomically append one BOQ item (+ its template-derived resource
	rows) to a live Tender, bypassing the client's held document
	snapshot entirely - this is what makes two users adding rows
	around the same time not collide. for_update=True row-locks the
	Tender for the duration of this request, so a second concurrent
	call blocks until this one's save commits, then reads this row
	before appending its own - idx is assigned by append() itself
	(synchronous, before save), so both rows land with correct,
	non-colliding idx values."""
	from contracting.contracting.api.work_item_template_v2 import get_v2_template_rows

	original_quantity = flt(original_quantity) or 1
	payload = get_v2_template_rows(template_name)
	parent, children = payload["parent"], payload["children"]

	doc = frappe.get_doc("Tender", tender_name, for_update=True)

	row = doc.append("boq_items", {
		"work_item_template": template_name,
		"item_name": parent["item_name"],
		"item_type": parent["item_type"],
		"uom": parent["uom"],
		"is_group": 0,
		"original_quantity": original_quantity,
		"vat_percentage": parent["vat_percentage"],
		"other_additions_pct": parent["other_additions_pct"],
		"fixed_additions": parent["fixed_additions"],
	})

	table_by_type = {"material": "material_items", "labor": "labor_items", "equipment": "equipment_items"}
	for child in children:
		fieldname = table_by_type[child["resource_type"]]
		qty = (child["qty_per_unit"] or 0) * original_quantity
		doc.append(fieldname, {
			"work_item": row.idx,
			"item": child["item"],
			"qty_per_unit": child["qty_per_unit"],
			"rate": child["rate"],
			"qty": qty,
			# Template rows start priced in the base currency - the
			# estimator picks a foreign currency afterwards if needed.
			"currency": doc.base_currency(),
			"exchange_rate": 1,
			"amount": qty * (child["rate"] or 0),
		})

	doc.save()
	return {"boq_row_idx": row.idx}


@frappe.whitelist()
def mark_tender_complete(tender_name):
	"""Manual Tender Manager checkpoint: In Progress -> Complete. Once
	Complete, validate_not_complete_locked() rejects any further edit
	except through Create Revised Version."""
	frappe.only_for("Tender Manager")
	doc = frappe.get_doc("Tender", tender_name)
	if doc.status != "In Progress":
		frappe.throw(_("Only a Tender that is In Progress can be marked Complete."))
	doc.status = "Complete"
	doc.save()


@frappe.whitelist()
def create_revised_version(tender_name):
	"""Only escape hatch for a Complete (locked) Tender: clone it into a
	new, numbered Draft so the same opportunity can be re-bid without
	losing the prior bid's history. The source stays Complete - re-linking
	the new version into the relevant Project Tender's Direct Cost Detail
	row is a manual step, not automatic."""
	source = frappe.get_doc("Tender", tender_name)
	if source.status != "Complete":
		frappe.throw(_("Create Revised Version is only available once a Tender is Complete."))

	new_tender = frappe.copy_doc(source)
	new_tender.tender_version = (source.tender_version or 1) + 1
	new_tender.previous_tender = source.name
	new_tender.status = "Draft"
	new_tender.insert(ignore_permissions=True)

	return new_tender.name
