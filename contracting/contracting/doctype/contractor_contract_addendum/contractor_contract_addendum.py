import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from contracting.contracting.utils import allocation, approval_guards
from contracting.contracting.utils.allocation import (
	TEMPLATE_DOCTYPE,
	TOLERANCE,
	line_key,
)

# 2026-10-18 BRD: an addendum amends an Approved contract - per row Update,
# Delete or Add - and nothing touches the contract until final approval.
# Rows without an action are either the unchanged base rows loaded from the
# contract (they carry base_contract_item) or, with neither, a legacy delta
# row from before this rebuild (add-only; kept working as it always did).
UPDATE, DELETE, ADD = "Update", "Delete", "Add"

SUBMITTED_STATE = "Pending Contracts Manager Approval"
EDITABLE_STATES = ("", "Draft")
# States an addendum sits in while it is awaiting approval (not Draft, not
# finished): another addendum touching the same contract row is blocked while
# one of these holds it.
IN_FLIGHT_STATES = (
	"Pending Contracts Manager Approval",
	"Pending Projects Director Approval",
	"Pending Cost Control Engineer Review",
	"Pending Cost Control Approval",
	"Pending Planning and Cost Control Director Approval",
	"Pending CEO Approval",
)

CONTRACT_RESOURCE_TABLES = (
	("contract_material_items", "Contractor Contract Material Item"),
	("contract_labor_items", "Contractor Contract Labor Item"),
	("contract_subcontract_items", "Contractor Contract Subcontract Item"),
	("contract_equipment_items", "Contractor Contract Equipment Item"),
)


class ContractorContractAddendum(Document):
	def validate(self):
		approval_guards.enforce_checklist_window(self)
		approval_guards.protect_effective_date(self)
		self.set_project()
		self.set_project_tenders()
		self.validate_items()
		self.validate_payment_conditions()
		self.validate_no_conflicting_addendum()
		self.calculate_grand_total_delta()
		self.set_ceo_approval_flag()

	def before_submit(self):
		approval_guards.stamp_effective_date(self)
		self.validate_availability(lock=True)

	def set_project(self):
		"""The addendum's project is always its contract's - the field exists
		to filter the contract picker, not to be set independently."""
		if self.subcontractor_contract:
			self.project = frappe.db.get_value("Subcontractor Contract", self.subcontractor_contract, "project")

	def set_project_tenders(self):
		"""Mirrors the base contract's own project_tenders (FR-01, extended
		2026-08-30) - copied rather than fetched, since Table fields can't
		use fetch_from.
		"""
		self.set("project_tenders", [])
		if not self.subcontractor_contract:
			return

		tenders = frappe.get_all(
			"Subcontractor Contract Tender",
			filters={"parent": self.subcontractor_contract},
			order_by="idx",
			pluck="tender",
		)
		self.set("project_tenders", [{"tender": t} for t in tenders])

	def uses_actions(self):
		"""True once any row is an Update/Delete/Add or a loaded base row -
		the 2026-10-18 model. A document of only legacy delta rows keeps the
		original rules untouched."""
		return any(row.action or row.base_contract_item for row in self.addendum_items)

	def validate_items(self):
		"""Legacy rows (and Add rows): an item only needs work_item +
		qty_delta + unit_price - resource selection is no longer hand-picked
		(2026-08-30), the base contract's resource rows come entirely from
		propagation on approval. Update/Delete/unchanged rows reference a
		base Contracted Items row instead (_validate_base_row).
		"""
		if not self.addendum_items:
			frappe.throw(_("Add at least one Addendum Item."))

		actions = self.uses_actions()
		if actions and self.docstatus == 0:
			self.validate_base_contract()

		project_tenders = {row.tender for row in self.project_tenders}
		actioned = 0
		for row in self.addendum_items:
			if self.is_new() and not row.action and not row.base_contract_item:
				# A new addendum has no legacy rows: a row that is neither a
				# loaded contract row nor marked is a new work item.
				row.action = ADD
			if not row.action and not row.base_contract_item:
				self._validate_added_row(row, project_tenders)
			elif row.action == ADD:
				if row.base_contract_item:
					frappe.throw(_("Row {0}: an Add row cannot reference an existing contract row.").format(row.idx))
				self._validate_added_row(row, project_tenders)
				row.new_qty, row.new_unit_price = row.qty_delta, row.unit_price
				actioned += 1
			else:
				self._validate_base_row(row)
				actioned += 1 if row.action else 0

		if actions and not actioned:
			frappe.throw(_("Update, Delete or Add at least one row."))

	def _validate_added_row(self, row, project_tenders):
		if bool(row.work_item) == bool(row.work_item_template):
			frappe.throw(
				_("Row {0}: set exactly one of Work Item (Tender line) or Work Item Template.").format(row.idx)
			)
		if flt(row.qty_delta) <= 0:
			frappe.throw(_("Row {0}: Qty must be greater than zero.").format(row.idx))
		if flt(row.unit_price) < 0:
			frappe.throw(_("Row {0}: Unit Price cannot be negative.").format(row.idx))

		if row.work_item_template:
			# Follows Tender BRD FR-16: a template line - no Tender
			# membership or availability to check (FR-09).
			if not frappe.db.exists(TEMPLATE_DOCTYPE, row.work_item_template):
				frappe.throw(_("Row {0}: Work Item Template {1} does not exist.").format(
					row.idx, frappe.bold(row.work_item_template)))
			row.amount = flt(row.qty_delta) * flt(row.unit_price)
			return

		work_item_row = allocation.get_work_item(row.work_item)
		if work_item_row.parent not in project_tenders:
			frappe.throw(
				_("Row {0}: work item {1} does not belong to any of the base contract's Project "
				  "Tenders ({2}).").format(
					row.idx, frappe.bold(row.work_item), frappe.bold(", ".join(project_tenders) or "")
				)
			)

		self._validate_resource_lines(row, work_item_row)
		row.amount = flt(row.qty_delta) * flt(row.unit_price)

	def _validate_resource_lines(self, row, work_item_row):
		"""2026-10-20 (partial contracts): the lines an Add row takes must be
		Tender lines of its own work item, in the contract type's pools, and
		not already on the base contract (that one is an Update instead)."""
		lines = parse_resource_lines(row.resource_lines)
		if not lines:
			row.resource_lines = None
			return
		allowed = {line.name: line for line in allocation.get_line_availability(self.type_subcontractor, work_item_row)}
		on_contract = contract_line_names(self.subcontractor_contract, row.work_item)
		labels = []
		for name in lines:
			if name not in allowed:
				frappe.throw(_("Row {0}: line {1} is not one of work item {2}'s lines for a {3} contract.").format(
					row.idx, frappe.bold(name), frappe.bold(row.work_item), self.type_subcontractor))
			if name in on_contract:
				frappe.throw(_("Row {0}: line {1} is already on contract {2} - change its qty with an Update "
							   "on that row instead.").format(row.idx, frappe.bold(allowed[name].item),
															   frappe.bold(self.subcontractor_contract)))
			labels.append(allowed[name].item)
		row.resource_lines = frappe.as_json(lines, indent=None)
		row.resource_item = ", ".join(labels)

	def validate_base_contract(self):
		contract = frappe.db.get_value(
			"Subcontractor Contract", self.subcontractor_contract,
			["docstatus", "workflow_state", "is_closed"], as_dict=True,
		)
		if not contract or contract.docstatus != 1 or contract.workflow_state != "Approved":
			frappe.throw(_("An addendum can only amend an Approved contract."))
		if contract.is_closed:
			frappe.throw(_("Contract {0} is closed and cannot be amended.").format(
				frappe.bold(self.subcontractor_contract)))

	def _validate_base_row(self, row):
		"""Update / Delete / unchanged: a row that points at one of the base
		contract's Contracted Items. Everything about the base row is read
		from the database on every validate - the client only supplies the
		reference and, for an Update, the new qty / price - so the old
		values and invoiced qty can never be forged.
		"""
		base = frappe.db.get_value(
			"Contractor Contract Item", row.base_contract_item,
			["parent", "description", "qty", "unit_price", "work_item", "work_item_template",
			 "labor_item", "equipment_item"],
			as_dict=True,
		)
		if not base or base.parent != self.subcontractor_contract:
			frappe.throw(_("Row {0}: this is not a row of contract {1}.").format(
				row.idx, frappe.bold(self.subcontractor_contract)))

		row.description = base.description
		row.work_item, row.work_item_template = base.work_item, base.work_item_template
		row.labor_item, row.equipment_item = base.labor_item, base.equipment_item
		row.old_qty, row.old_unit_price = flt(base.qty), flt(base.unit_price)
		invoiced = get_invoiced_progress(row.base_contract_item)
		row.invoiced_qty = invoiced["qty"]

		old_total = flt(base.qty) * flt(base.unit_price)
		if row.action == UPDATE:
			new_qty, new_price = flt(row.new_qty), flt(row.new_unit_price)
			if new_qty < 0 or new_price < 0:
				frappe.throw(_("Row {0}: New Qty and New Unit Price cannot be negative.").format(row.idx))
			if new_qty + TOLERANCE < invoiced["qty"]:
				frappe.throw(_("Row {0}: New Qty {1} is below the {2} already invoiced on this row.").format(
					row.idx, flt(new_qty), flt(invoiced["qty"])))
			if abs(new_qty - flt(base.qty)) <= TOLERANCE and abs(new_price - flt(base.unit_price)) <= TOLERANCE:
				frappe.throw(_("Row {0}: Update changes nothing - set a different Qty or Unit Price.").format(row.idx))
			row.qty_delta, row.unit_price = new_qty - flt(base.qty), new_price
			row.amount = new_qty * new_price - old_total
		elif row.action == DELETE:
			if invoiced["qty"] > TOLERANCE or invoiced["amount"] > TOLERANCE or invoiced["draft_qty"] > TOLERANCE:
				frappe.throw(_("Row {0}: cannot delete a row that already has invoiced progress.").format(row.idx))
			row.new_qty, row.new_unit_price = 0, 0
			row.qty_delta, row.unit_price, row.amount = -flt(base.qty), flt(base.unit_price), -old_total
		elif not row.action:
			row.new_qty, row.new_unit_price = flt(base.qty), flt(base.unit_price)
			row.qty_delta, row.unit_price, row.amount = 0, flt(base.unit_price), 0
		else:
			frappe.throw(_("Row {0}: a row of the contract can only be Updated or Deleted.").format(row.idx))

	def validate_no_conflicting_addendum(self):
		"""Two addenda cannot hold the same contract row at once. Checked when
		this one enters approval, under a lock on the contract, so the second
		of two simultaneous submissions is the one that is blocked."""
		before = self.get_doc_before_save()
		if self.workflow_state != SUBMITTED_STATE:
			return
		if before and (before.workflow_state or "") not in EDITABLE_STATES:
			return
		touched = [row.base_contract_item for row in self.addendum_items if row.action in (UPDATE, DELETE)]
		if not touched:
			return

		frappe.db.sql("select name from `tabSubcontractor Contract` where name=%s for update", (self.subcontractor_contract,))
		rivals = frappe.db.sql(
			"""select a.name, i.base_contract_item
			from `tabContractor Contract Addendum` a
			join `tabContractor Contract Addendum Item` i on i.parent = a.name
			where a.subcontractor_contract = %(contract)s and a.name != %(me)s and a.docstatus = 0
			  and a.workflow_state in %(states)s and i.action in ('Update', 'Delete')
			  and i.base_contract_item in %(rows)s""",
			{"contract": self.subcontractor_contract, "me": self.name or "", "states": IN_FLIGHT_STATES,
			 "rows": touched},
			as_dict=True,
		)
		if rivals:
			frappe.throw(
				_("Another addendum already holds a row you are changing: {0}.").format(
					"; ".join(sorted({"{0} ({1})".format(frappe.bold(r.name), r.base_contract_item) for r in rivals}))
				),
				title=_("Conflicting Addendum"),
			)

	def validate_payment_conditions(self):
		"""FR-24: the addendum's conditions are checked as they will stand
		once applied - the contract's current conditions, minus those of
		deleted lines, with replacements (base_condition) swapped in and new
		rows added - and only for the groups the addendum touched.
		"""
		conditions = self.addendum_payment_conditions
		add_rows = [row for row in self.addendum_items if row.action == ADD]
		if not conditions and not add_rows:
			return

		from contracting.contracting.doctype.subcontractor_contract.subcontractor_contract import CATEGORY_TYPE_GATE

		current = frappe.get_all(
			"Contractor Contract Payment Condition", filters={"parent": self.subcontractor_contract},
			fields=["name", "work_item", "work_item_template", "condition", "percent", "category"],
			order_by="idx",
		)
		if conditions and not current:
			frappe.throw(_("Contract {0} has no Payment Conditions, so invoices bill per line. Remove the "
				"addendum's Payment Conditions, or add them on the contract itself.").format(
				frappe.bold(self.subcontractor_contract)))

		base_by_name = {c.name: c for c in current}
		deleted_keys = {line_key(row) for row in self.addendum_items if row.action == DELETE}
		added_keys = {line_key(row) for row in add_rows}
		live_keys = {
			line_key(frappe._dict(r)) for r in frappe.get_all(
				"Contractor Contract Item", filters={"parent": self.subcontractor_contract},
				fields=["work_item", "work_item_template"],
			)
		} - deleted_keys

		replaced, touched = {}, set()
		new_rows = []
		for row in conditions:
			if row.work_item and row.work_item_template:
				frappe.throw(_("Payment Conditions row {0}: set Work Item or Work Item Template, not both.").format(row.idx))
			key = line_key(row)
			touched.add(key)
			if key is not None and key in deleted_keys:
				frappe.throw(_("Payment Conditions row {0}: that work item is being deleted - its conditions are "
					"removed with it.").format(row.idx))
			if key is not None and key not in live_keys and key not in added_keys:
				frappe.throw(_("Payment Conditions row {0}: that work item is not a line of this contract or this "
					"addendum.").format(row.idx))
			if row.category and self._contract_type() not in CATEGORY_TYPE_GATE.get(row.category, ()):
				frappe.throw(_("Payment Conditions row {0}: Category {1} is not applicable to a {2} contract.").format(
					row.idx, row.category, self._contract_type()))
			if row.base_condition:
				base = base_by_name.get(row.base_condition)
				if not base:
					frappe.throw(_("Payment Conditions row {0}: the condition it replaces no longer exists.").format(row.idx))
				if line_key(base) != key:
					frappe.throw(_("Payment Conditions row {0}: a replacement must stay on the same work item.").format(row.idx))
				if row.base_condition in replaced:
					frappe.throw(_("Payment Conditions row {0}: the same condition is replaced twice.").format(row.idx))
				replaced[row.base_condition] = row
			else:
				new_rows.append(row)

		merged = [
			frappe._dict(replaced[c.name].as_dict() if c.name in replaced else c)
			for c in current if line_key(c) not in deleted_keys
		] + [frappe._dict(r.as_dict()) for r in new_rows]
		merged_keys = {line_key(r) for r in merged}

		if current:
			for row in add_rows:
				key = line_key(row)
				# Without its own conditions a new line falls back to the
				# contract-wide default group, if there is one.
				if key not in merged_keys and None not in merged_keys:
					frappe.throw(_("Row {0}: the new work item needs Payment Conditions (the contract has no "
						"contract-wide default to fall back on).").format(row.idx))

		allocation.validate_condition_groups(merged, only_keys=touched)

	def _contract_type(self):
		return self.type_subcontractor or frappe.db.get_value(
			"Subcontractor Contract", self.subcontractor_contract, "type_subcontractor")

	def calculate_grand_total_delta(self):
		self.grand_total_delta = sum(flt(row.amount) for row in self.addendum_items)

	def set_ceo_approval_flag(self):
		"""Mirrors Subcontractor Contract's own set_ceo_approval_flag,
		against this addendum's own Grand Total Delta rather than the base
		contract's Grand Total - workflow conditions run through safe_eval,
		which cannot read a Single doctype, so this has to be precomputed.
		"""
		threshold = flt(frappe.db.get_single_value("Contracting Settings", "ceo_approval_value_threshold"))
		over_threshold = bool(threshold and flt(self.grand_total_delta) >= threshold)

		# Follows Tender BRD OQ-5: adding a template line to a contract
		# whose project has a Tender goes beyond the original Tender, so it
		# escalates the same way the base contract does (FR-13).
		out_of_tender = bool(self.project_tenders) and any(
			row.work_item_template for row in self.addendum_items
			if not row.base_contract_item  # loaded base rows are not additions
		)

		self.requires_ceo_approval = 1 if (over_threshold or out_of_tender) else 0

	def validate_availability(self, lock=False):
		"""FR-37: re-check availability inside the submitting transaction.

		Deliberately does *not* exclude the base contract itself: the base
		contract's own commitment is real and still stands (the addendum
		only adds new rows to it on approval - see apply_on_approval - it
		never removes or replaces the base contract's existing rows), so an
		addendum's delta has to compete for whatever is left after that
		commitment, the same as any other new claim would. exclude_contract
		exists to stop a document from being blocked by its own
		not-yet-committed rows, which is irrelevant here: an Addendum's
		rows live in their own table that _committed() never queries, so
		there is nothing of this document's own to exclude.
		"""
		tender_rows = [row for row in self.addendum_items if row.work_item]
		if lock:
			allocation.lock_work_items([row.work_item for row in tender_rows])

		# 2026-10-20: Add rows that picked their lines are checked per line -
		# each line needs ratio x qty of what's still free on it.
		per_line = {}
		for row in tender_rows:
			lines = parse_resource_lines(row.resource_lines) if not row.base_contract_item else []
			if not lines:
				continue
			free = {line.name: line for line in allocation.get_line_availability(
				self.type_subcontractor, allocation.get_work_item(row.work_item))}
			for name in lines:
				line = free.get(name)
				if not line:
					continue  # _validate_resource_lines already rejected it
				entry = per_line.setdefault(name, {"qty": 0.0, "idx": row.idx, "line": line})
				entry["qty"] += line.ratio * flt(row.qty_delta)
		for entry in per_line.values():
			if entry["qty"] > entry["line"].available_qty + TOLERANCE:
				frappe.throw(_("Row {0}: cannot allocate {1} of {2} - only {3} remains available.").format(
					entry["idx"], flt(entry["qty"]), frappe.bold(entry["line"].item), flt(entry["line"].available_qty)))

		own = {}
		for row in tender_rows:
			if not row.base_contract_item and parse_resource_lines(row.resource_lines):
				continue  # checked per line above
			key = self._allocation_key(row)
			entry = own.setdefault(key, {"qty": 0.0, "idx": row.idx})
			entry["qty"] += flt(row.qty_delta)

		for key, entry in own.items():
			available = allocation.get_row_availability(
				self.type_subcontractor,
				key[1],
				labor_item=key[2],
				equipment_item=key[3],
			)
			if entry["qty"] > available + TOLERANCE:
				frappe.throw(
					_("Row {0}: cannot allocate {1} - only {2} remains available for {3}.").format(
						entry["idx"], flt(entry["qty"]), flt(available), frappe.bold(key[1])
					)
				)

	def _allocation_key(self, row):
		# Resource-claiming types (Labor / Installing / Equipment) dispatch on
		# their own pools inside get_row_availability (allocation.type_resources);
		# the legacy labor_item / equipment_item picks pass straight through
		# and only one matching a pool the type claims is read.
		if allocation.type_resources(self.type_subcontractor):
			return ("resource", row.work_item, row.labor_item, row.equipment_item)
		return ("item", row.work_item, None, None)


@frappe.whitelist()
def get_project_tenders_for_contract(subcontractor_contract):
	"""Client-side counterpart to set_project_tenders - lets the JS populate
	Project Tenders (and so the work_item picker) as soon as a base
	contract is chosen, rather than waiting for the first save. A plain
	frappe.client.get_list on this child doctype would hit the same
	permission gap Tender BOQ/Labor/Equipment Item links have (see
	contracting.contracting.api.link's module docstring) - this sidesteps
	it the same way those do, with its own whitelisted read.
	"""
	if not subcontractor_contract:
		return []
	return frappe.get_all(
		"Subcontractor Contract Tender",
		filters={"parent": subcontractor_contract},
		order_by="idx",
		pluck="tender",
	)


def get_invoiced_progress(contract_item):
	"""What invoices already hold against one Contracted Items row: qty and
	amount on submitted Contractor Invoices (what an Update may not go below
	and a Delete may not discard), plus qty entered on still-open drafts
	(which also blocks a Delete - the draft would be left pointing at
	nothing). Contract lines billed per payment condition roll up into the
	same Contractor Invoice Item rows, so one query covers both modes."""
	row = frappe.db.sql(
		"""select
			coalesce(sum(case when ci.docstatus = 1 then cii.this_period_qty end), 0) as qty,
			coalesce(sum(case when ci.docstatus = 1 then cii.this_period_amount end), 0) as amount,
			coalesce(sum(case when ci.docstatus = 0 then abs(cii.this_period_qty) end), 0) as draft_qty
		from `tabContractor Invoice Item` cii
		join `tabContractor Invoice` ci on ci.name = cii.parent
		where cii.contract_item = %s""",
		(contract_item,),
		as_dict=True,
	)[0]
	return {"qty": flt(row.qty), "amount": flt(row.amount), "draft_qty": flt(row.draft_qty)}


def apply_amendments(doc, contract, resource_tables):
	"""FR-28: Update and Delete rows, then payment conditions, applied to the
	base contract inside the approval's own transaction. Like the Add path
	this writes with db_* calls and never runs the contract's validate(), so
	the rules that matter are re-checked here against the database as it is
	now - an invoice may have been submitted since the addendum was drafted.
	"""
	summary = {"updated": 0, "deleted": 0}
	rows = [row for row in doc.addendum_items if row.base_contract_item and row.action in (UPDATE, DELETE)]

	deleted_names, deleted_keys = [], set()
	for row in rows:
		item = frappe.db.get_value(
			"Contractor Contract Item", row.base_contract_item,
			["name", "parent", "qty", "unit_price", "work_item", "work_item_template"], as_dict=True,
		)
		if not item or item.parent != contract.name:
			frappe.throw(_("Row {0}: contract row {1} no longer exists.").format(row.idx, row.base_contract_item))
		invoiced = get_invoiced_progress(row.base_contract_item)

		if row.action == UPDATE:
			if flt(row.new_qty) + TOLERANCE < invoiced["qty"]:
				frappe.throw(_("Row {0}: New Qty {1} is now below the {2} invoiced on this row.").format(
					row.idx, flt(row.new_qty), flt(invoiced["qty"])))
			frappe.db.set_value(
				"Contractor Contract Item", row.base_contract_item,
				{"qty": row.new_qty, "unit_price": row.new_unit_price, "amount": flt(row.new_qty) * flt(row.new_unit_price)},
				update_modified=False,
			)
			rescale_resources(contract, item, flt(row.new_qty), resource_tables, doc.type_subcontractor)
			reprice_components(contract, item, flt(row.new_qty), flt(row.new_unit_price), flt(item.unit_price))
			summary["updated"] += 1
		else:
			if invoiced["qty"] > TOLERANCE or invoiced["amount"] > TOLERANCE:
				frappe.throw(_("Row {0}: this row has been invoiced since the addendum was drafted.").format(row.idx))
			deleted_names.append(row.base_contract_item)
			deleted_keys.add(line_key(item))
			summary["deleted"] += 1

	if deleted_names:
		delete_contract_rows(contract, deleted_names, deleted_keys)

	apply_payment_conditions(doc, contract, deleted_keys)
	return summary


def parse_resource_lines(value):
	if not value:
		return []
	lines = frappe.parse_json(value) if isinstance(value, str) else value
	return [line for line in (lines or []) if line]


def contract_line_names(subcontractor_contract, work_item):
	"""Tender resource-row names already carried on a contract for a work item."""
	names = set()
	for _dt, child_doctype, fieldname, link_fieldname in allocation.RESOURCE_PROPAGATION[allocation.SUPPLY_AND_INSTALL]:
		names.update(frappe.get_all(
			child_doctype,
			filters={"parent": subcontractor_contract, "parentfield": fieldname, "work_item": work_item},
			pluck=link_fieldname,
		))
	return {name for name in names if name}


@frappe.whitelist()
def get_addable_lines(subcontractor_contract, work_item):
	"""The Addendum line picker: the work item's lines this contract's type
	can claim, minus those the contract already carries."""
	frappe.has_permission("Subcontractor Contract", "read", doc=subcontractor_contract, throw=True)
	contract_type = frappe.db.get_value("Subcontractor Contract", subcontractor_contract, "type_subcontractor")
	on_contract = contract_line_names(subcontractor_contract, work_item)
	return [
		line for line in allocation.get_line_availability(contract_type, allocation.get_work_item(work_item))
		if line.name not in on_contract
	]


def rescale_resources(contract, item, new_qty, resource_tables, contract_type):
	"""Server-side twin of subcontractor_contract.js propagate_resources'
	rescale branch: qty (and available_qty / amount) is updated in place on
	rows still marked auto_propagated; hand-added or hand-edited rows are
	left alone, nothing is created or deleted."""
	if item.work_item:
		propagated = allocation._propagated_resource_rows(
			allocation.get_work_item(item.work_item), new_qty, contract_type, subcontractor_contract=contract.name
		)
		line_filter, match_by = {"work_item": item.work_item}, None
	else:
		propagated = allocation._template_resource_rows(item.work_item_template, new_qty, contract_type)
		line_filter, match_by = {"work_item_template": item.work_item_template}, "item"

	for _dt, child_doctype, fieldname, link_fieldname in resource_tables:
		match_field = match_by or link_fieldname
		existing = [
			r for r in frappe.get_all(
				child_doctype, filters={"parent": contract.name, "parentfield": fieldname, **line_filter},
				fields=["name", match_field, "auto_propagated", "contract_row"] + (["rate"] if match_by else []),
			)
			# 2026-10-20: only this row's own lines (older rows have no owner).
			if not r.contract_row or r.contract_row == item.name
		]
		for new_row in propagated.get(fieldname, []):
			match = next((r for r in existing if r.get(match_field) == new_row.get(match_field)), None)
			if not match or not match.auto_propagated:
				continue
			if match_by is None and flt(new_row["qty"]) > flt(new_row["available_qty"]) + TOLERANCE:
				frappe.throw(
					_("Work item {0}: cannot allocate {1} - only {2} remains available for {3}.").format(
						frappe.bold(item.work_item), flt(new_row["qty"]), flt(new_row["available_qty"]),
						frappe.bold(new_row.get("resource_item") or fieldname),
					)
				)
			values = {"qty": new_row["qty"]}
			if match_by:
				values["amount"] = flt(new_row["qty"]) * flt(match.rate)
			else:
				values["available_qty"] = new_row["available_qty"]
			frappe.db.set_value(child_doctype, match.name, values, update_modified=False)


def reprice_components(contract, item, qty, unit_price, old_unit_price=None):
	"""2026-10-20: keep a Total-priced line's cost components adding up to
	its amount when the addendum sets it. A repriced row keeps its own
	breakdown, scaled to the new price; a new row (or one never broken
	down) is split in the proportions of the Tender/template budget - the
	same split the contract form applies (split_component_rates)."""
	from contracting.contracting.doctype.subcontractor_contract.subcontractor_contract import (
		_TENDER_LINK_BY_TABLE,
		split_component_rates,
	)

	if frappe.db.get_value("Contractor Contract Item", item.name, "pricing_mode") == "Components":
		return

	line_filter = {"work_item": item.work_item} if item.get("work_item") else {"work_item_template": item.work_item_template}
	line_rows = []
	for fieldname, child_doctype in CONTRACT_RESOURCE_TABLES:
		link_fieldname = _TENDER_LINK_BY_TABLE[fieldname][1]
		line_rows += [(fieldname, child_doctype, r) for r in frappe.get_all(
			child_doctype, filters={"parent": contract.name, "parentfield": fieldname, **line_filter},
			fields=["name", "qty", "rate", "source", "work_item_template", "item", "contract_row", link_fieldname],
		)]
	# Rows it owns; unowned rows (from before contract_row) only when nothing
	# on the line is owned by this row - a new row's lines are always stamped.
	owned = [entry for entry in line_rows if entry[2].contract_row == item.name] \
		or [entry for entry in line_rows if not entry[2].contract_row]
	if not owned:
		return

	if old_unit_price and any(flt(r.rate) for _f, _dt, r in owned):
		rates = [flt(r.rate) * flt(unit_price) / flt(old_unit_price) for _f, _dt, r in owned]
	else:
		rates = split_component_rates(
			[{
				"table": fieldname, "link": r.get(_TENDER_LINK_BY_TABLE[fieldname][1]), "source": r.source,
				"work_item_template": r.work_item_template, "item": r.item, "qty": r.qty,
			} for fieldname, _dt, r in owned],
			flt(qty) * flt(unit_price),
		)
	for (_f, child_doctype, r), rate in zip(owned, rates):
		frappe.db.set_value(child_doctype, r.name, {"rate": rate, "amount": flt(r.qty) * rate}, update_modified=False)


def delete_contract_rows(contract, row_names, deleted_keys):
	"""Remove deleted Contracted Items rows. Resource and payment-condition
	rows are keyed by the line (work item / template), so they go only when
	no other row of the contract still uses that line. Draft invoices that
	pointed at a removed row lose their (zero-progress) rows for it - the
	delete was refused if any held progress."""
	frappe.db.delete("Contractor Contract Item", {"name": ("in", row_names)})
	# 2026-10-20: lines taken for a specific row go with it.
	for fieldname, child_doctype in CONTRACT_RESOURCE_TABLES:
		frappe.db.delete(child_doctype, {"parent": contract.name, "contract_row": ("in", row_names)})
	remaining = {
		line_key(frappe._dict(r)) for r in frappe.get_all(
			"Contractor Contract Item", filters={"parent": contract.name}, fields=["work_item", "work_item_template"]
		)
	}
	orphaned = [key for key in deleted_keys if key not in remaining]

	for key in orphaned:
		column = "work_item" if key[0] == "tender" else "work_item_template"
		for fieldname, child_doctype in CONTRACT_RESOURCE_TABLES:
			frappe.db.delete(child_doctype, {"parent": contract.name, "parentfield": fieldname, column: key[1]})
		gone = frappe.get_all(
			"Contractor Contract Payment Condition", filters={"parent": contract.name, column: key[1]}, pluck="name"
		)
		if gone:
			_delete_draft_invoice_rows("Contractor Invoice Condition Progress", "payment_condition", gone)
			frappe.db.delete("Contractor Contract Payment Condition", {"name": ("in", gone)})

	_delete_draft_invoice_rows("Contractor Invoice Item", "contract_item", row_names)
	_delete_draft_invoice_rows("Contractor Invoice Condition Progress", "contract_item", row_names)
	for fieldname in ("contracted_items", "payment_conditions"):
		renumber(contract.name, fieldname)


def _delete_draft_invoice_rows(child_doctype, link_field, names):
	frappe.db.sql(
		"""delete c from `tab{child}` c join `tabContractor Invoice` i on i.name = c.parent
		where i.docstatus = 0 and c.`{field}` in %(names)s""".format(child=child_doctype, field=link_field),
		{"names": names},
	)


def renumber(contract_name, parentfield):
	child = {"contracted_items": "Contractor Contract Item", "payment_conditions": "Contractor Contract Payment Condition"}[parentfield]
	for idx, name in enumerate(
		frappe.get_all(child, filters={"parent": contract_name, "parentfield": parentfield}, order_by="idx", pluck="name"), 1
	):
		frappe.db.set_value(child, name, "idx", idx, update_modified=False)


def apply_payment_conditions(doc, contract, deleted_keys):
	"""Replacement conditions overwrite the row they name; the rest are new
	rows on the contract (conditions of deleted lines were removed above)."""
	next_idx = frappe.db.count("Contractor Contract Payment Condition", {"parent": contract.name})
	for row in doc.addendum_payment_conditions:
		values = {
			"work_item": row.work_item, "work_item_template": row.work_item_template,
			"condition": row.condition, "percent": row.percent, "category": row.category,
		}
		if row.base_condition:
			frappe.db.set_value("Contractor Contract Payment Condition", row.base_condition, values, update_modified=False)
		else:
			next_idx += 1
			frappe.get_doc({
				"doctype": "Contractor Contract Payment Condition", "parent": contract.name,
				"parenttype": "Subcontractor Contract", "parentfield": "payment_conditions",
				"idx": next_idx, **values,
			}).db_insert()


@frappe.whitelist()
def get_contract_items_for_addendum(subcontractor_contract):
	"""FR-20: the base contract's current Contracted Items, as addendum rows
	(unchanged: no action). Readable by anyone who can read the contract;
	carries the contract's own qty / unit price and invoiced qty, never the
	Tender cost (tender_unit_cost), so the contracts-department scrub is
	not widened."""
	if not subcontractor_contract:
		return []
	frappe.has_permission("Subcontractor Contract", "read", doc=subcontractor_contract, throw=True)

	rows = []
	for item in frappe.get_all(
		"Contractor Contract Item", filters={"parent": subcontractor_contract, "parenttype": "Subcontractor Contract"},
		fields=["name", "description", "work_item", "work_item_template", "qty", "unit_price"], order_by="idx",
	):
		rows.append({
			"base_contract_item": item.name, "description": item.description,
			"work_item": item.work_item, "work_item_template": item.work_item_template,
			"old_qty": item.qty, "old_unit_price": item.unit_price,
			"new_qty": item.qty, "new_unit_price": item.unit_price,
			"invoiced_qty": get_invoiced_progress(item.name)["qty"],
		})
	return rows


@frappe.whitelist()
def get_contract_payment_conditions(subcontractor_contract):
	"""Copies of the contract's Payment Conditions as replacement rows
	(base_condition set), for editing in the addendum's own table."""
	if not subcontractor_contract:
		return []
	frappe.has_permission("Subcontractor Contract", "read", doc=subcontractor_contract, throw=True)
	return [
		{
			"base_condition": c.name, "work_item": c.work_item, "work_item_template": c.work_item_template,
			"condition": c.condition, "percent": c.percent, "category": c.category,
		}
		for c in frappe.get_all(
			"Contractor Contract Payment Condition", filters={"parent": subcontractor_contract},
			fields=["name", "work_item", "work_item_template", "condition", "percent", "category"], order_by="idx",
		)
	]



def apply_on_approval(doc, method=None):
	"""doc_events on_update hook (hooks.py): FR-42-44 - once an Addendum
	reaches Approved, its items become new rows on the base contract's
	Contracted Items, plus every resource row propagation derives from the
	work item's own Tender-linked resources (2026-08-30: replaces the old
	single hand-picked labor_item/equipment_item insert - see
	allocation.RESOURCE_PROPAGATION / _propagated_resource_rows, the same
	function the base contract's own dialog and manual Add Row path use),
	and the contract's totals are recalculated.

	Guarded so it only fires on the actual transition into Approved, not on
	every later save while the addendum sits in that state.
	"""
	if doc.workflow_state != "Approved":
		return

	before = doc.get_doc_before_save()
	if before and before.workflow_state == "Approved":
		return

	contract = frappe.get_doc("Subcontractor Contract", doc.subcontractor_contract)
	resource_tables = allocation.RESOURCE_PROPAGATION.get(doc.type_subcontractor, [])

	# Update / Delete first, then conditions, so the Add loop below numbers
	# its new rows after whatever is left.
	summary = apply_amendments(doc, contract, resource_tables)
	contract = frappe.get_doc("Subcontractor Contract", doc.subcontractor_contract)

	next_idx = len(contract.contracted_items)
	next_resource_idx = {fieldname: len(contract.get(fieldname)) for _dt, _cdt, fieldname, _lf in resource_tables}

	# Follows Tender BRD: nothing below runs the base contract's validate(),
	# so the out-of-Tender flag for what this inserts is set here directly.
	has_tender = bool(contract.project_tenders)
	any_out_of_scope = False

	added = 0
	for row in doc.addendum_items:
		if row.base_contract_item:
			continue  # an Update / Delete / unchanged row - handled by apply_amendments
		added += 1
		next_idx += 1
		if row.work_item_template:
			# FR-16 / OQ-3: same shape as a Tender-line addendum - a new
			# Contracted Items row at the addendum's own price, marked with
			# its addendum so FR-06's one-row-per-template rule exempts it.
			template = frappe.db.get_value(
				TEMPLATE_DOCTYPE, row.work_item_template,
				["template_name", "description", "uom", "total_cost_per_unit"], as_dict=True,
			)
			template_item = frappe.get_doc({
				"doctype": "Contractor Contract Item",
				"parent": contract.name,
				"parenttype": "Subcontractor Contract",
				"parentfield": "contracted_items",
				"idx": next_idx,
				"work_item_template": row.work_item_template,
				"follows_tender": 0,
				"addendum": doc.name,
				"description": template.description or template.template_name,
				"uom": template.uom,
				"qty": row.qty_delta,
				"unit_price": row.unit_price,
				"amount": row.amount,
				"tender_unit_cost": template.total_cost_per_unit,
				"out_of_tender_scope": 1 if has_tender else 0,
				"resources_propagated": 1,
			})
			template_item.db_insert()
			any_out_of_scope = any_out_of_scope or has_tender

			propagated = allocation._template_resource_rows(
				row.work_item_template, row.qty_delta, doc.type_subcontractor, include_reference=True
			)
			for _dt, child_doctype, fieldname, _lf in resource_tables:
				for prop_row in propagated.get(fieldname, []):
					next_resource_idx[fieldname] += 1
					frappe.get_doc({
						"doctype": child_doctype,
						"parent": contract.name,
						"parenttype": "Subcontractor Contract",
						"parentfield": fieldname,
						"idx": next_resource_idx[fieldname],
						**prop_row,
						"contract_row": template_item.name,
						"out_of_tender_scope": 1 if has_tender else 0,
					}).db_insert()
			reprice_components(contract, template_item, flt(row.qty_delta), flt(row.unit_price))
			continue

		contract_item = frappe.get_doc({
			"doctype": "Contractor Contract Item",
			"parent": contract.name,
			"parenttype": "Subcontractor Contract",
			"parentfield": "contracted_items",
			"idx": next_idx,
			"work_item": row.work_item,
			"follows_tender": 1,
			"addendum": doc.name,
			# db_insert skips validate, so fill what the link title and the
			# invoice grids display - blank shows the raw row id instead.
			"description": frappe.db.get_value("Tender BOQ Item", row.work_item, "description")
				or frappe.db.get_value("Tender BOQ Item", row.work_item, "item_name"),
			"uom": frappe.db.get_value("Tender BOQ Item", row.work_item, "uom"),
			"qty": row.qty_delta,
			"unit_price": row.unit_price,
			"amount": row.amount,
			"resources_propagated": 1,
		})
		contract_item.db_insert()

		work_item_row = allocation.get_work_item(row.work_item)
		# No exclude_contract: unlike the base contract's own dialog/manual-add
		# paths (which exclude themselves - their new rows aren't committed
		# yet, so comparing against a pool that already includes them would be
		# circular), the base contract here is a *different*, already-submitted
		# document whose prior commitment is real and must count - the same
		# principle validate_availability above already states explicitly.
		# 2026-10-20 (partial contracts): only the lines this Add row picked
		# (all of them when it picked none), each owned by its new row.
		lines = parse_resource_lines(row.resource_lines) or None
		propagated = allocation._propagated_resource_rows(
			work_item_row, row.qty_delta, doc.type_subcontractor, lines=lines
		)
		for _dt, child_doctype, fieldname, _lf in resource_tables:
			for prop_row in propagated.get(fieldname, []):
				# apply_on_approval writes directly via db_insert and never
				# runs the base contract's own validate() (see
				# SubcontractorContract._validate_resource_rows), so this is
				# the only over-allocation guard this path gets - without it,
				# an addendum could silently over-commit a resource row that
				# validate_availability's own pre-submit check (now looser,
				# since FR-16 stopped naming a specific resource) didn't catch.
				if flt(prop_row["qty"]) > flt(prop_row["available_qty"]) + TOLERANCE:
					frappe.throw(
						_("Work item {0}: cannot allocate {1} - only {2} remains available for {3}.").format(
							frappe.bold(row.work_item), flt(prop_row["qty"]), flt(prop_row["available_qty"]),
							frappe.bold(prop_row.get("resource_item") or fieldname),
						)
					)

				next_resource_idx[fieldname] += 1
				frappe.get_doc({
					"doctype": child_doctype,
					"parent": contract.name,
					"parenttype": "Subcontractor Contract",
					"parentfield": fieldname,
					"idx": next_resource_idx[fieldname],
					**prop_row,
					"contract_row": contract_item.name,
				}).db_insert()
		reprice_components(contract, contract_item, flt(row.qty_delta), flt(row.unit_price))

	if any_out_of_scope:
		frappe.db.set_value("Subcontractor Contract", contract.name, "has_out_of_tender_items", 1, update_modified=False)

	contract.reload()
	contract.calculate_totals()
	for row in contract.contracted_items:
		frappe.db.set_value("Contractor Contract Item", row.name, "amount", row.amount, update_modified=False)
	frappe.db.set_value(
		"Subcontractor Contract", contract.name,
		{
			"net_total": contract.net_total,
			"total_additional_charges": contract.total_additional_charges,
			"grand_total": contract.grand_total,
		},
		update_modified=False,
	)

	contract.add_comment(
		"Info",
		_("Addendum {0} approved: updated {1}, deleted {2}, added {3} row(s); {4} change to Grand Total.").format(
			doc.name, summary["updated"], summary["deleted"], added, flt(doc.grand_total_delta)
		),
	)
