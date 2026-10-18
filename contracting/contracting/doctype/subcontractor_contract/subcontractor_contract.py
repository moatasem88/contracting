import erpnext
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from contracting.contracting.utils import allocation
from contracting.contracting.utils import approval_guards
from contracting.contracting.utils.allocation import (
	EQUIPMENT,
	EQUIPMENT_DOCTYPE,
	LABOR_DOCTYPE,
	LABOR_TYPES,
	MATERIAL_DOCTYPE,
	SUPPLY_AND_INSTALL,
	TEMPLATE_DOCTYPE,
	TOLERANCE,
	line_key,
)

RETENTION_CHARGE = "Retention"
MANUAL_FALLBACK_CHARGE = "Material Supplied by Company (Manual Fallback)"

# Payment Condition category -> which contract types actually populate that
# resource tab (FR-14). Server-side truth, matching validate_labor_items /
# validate_equipment_items below - Supply and Install populates all three
# tabs, same as Install Only/Equipment populate their own single one.
CATEGORY_TYPE_GATE = {
	"Material": (SUPPLY_AND_INSTALL,),
	"Labor": (*LABOR_TYPES, SUPPLY_AND_INSTALL),
	"Equipment": (EQUIPMENT, SUPPLY_AND_INSTALL),
}
_RESOURCE_FIELD_BY_CATEGORY_REVERSE = {
	"material_item": "Material",
	"labor_item": "Labor",
	"equipment_item": "Equipment",
}

# Reuses the role that already owns financial oversight on this instance,
# rather than introducing a separate override role.
COST_CONTROL_ROLE = "cost control manager"

# FR-11: who may close a contract.
CLOSE_CONTRACT_ROLES = ("Contracts Manager", "System Manager")

# Price-ceiling/approval-chain rebuild (2026-09-27 BRD, FR-07a): a work
# item's aggregated Contracted Items amount at or above this, on its first
# Approved contract, forces CEO approval. Replaces the old whole-contract
# Contracting Settings.ceo_approval_value_threshold trigger entirely.
CEO_APPROVAL_WORK_ITEM_THRESHOLD = 250000

# Editing the figures is only allowed in Draft (FR-20). Everything else -
# the pending states, Approved, Rejected - is frozen, so an approver is
# always looking at the same numbers the next approver will see.
EDITABLE_STATES = ("", "Draft")

# State the Draft -> Submit for Approval transition lands in; the overlap
# check (validate_no_overlapping_contract) fires on entering it.
SUBMITTED_STATE = "Pending Contracts Manager Approval"


class SubcontractorContract(Document):
	def validate(self):
		approval_guards.enforce_checklist_window(self)
		approval_guards.protect_effective_date(self)
		self.set_default_company()
		self.set_project_tenders()
		self.apply_follows_tender_rules()
		self.validate_subcontractor()
		self.validate_no_overlapping_contract()
		self.enforce_frozen_figures()
		self.validate_contracted_items()
		self.prune_orphaned_resource_rows()
		self.validate_material_items()
		self.validate_labor_items()
		self.validate_equipment_items()
		self.validate_material_conflicts()
		self.prune_orphaned_payment_condition_resources()
		self.validate_payment_condition_resources()
		self.validate_payment_conditions()
		self.default_charge_cost_centers()
		self.compute_component_pricing()
		self.validate_price_ceiling()
		self.compute_tender_reference()
		self.compute_out_of_tender_scope()
		self.calculate_totals()
		self.validate_retention_rows()
		self.set_ceo_approval_flag()

	def before_submit(self):
		approval_guards.stamp_effective_date(self)
		self.validate_attachment()
		self.warn_if_no_retention()
		# Re-run availability inside the submitting transaction. Rows were
		# possibly drafted when more quantity was free (TC-05), and two
		# submissions can race (NFR-02).
		self.validate_allocation(lock=True)
		self.validate_material_items(lock=True)
		self.validate_labor_items(lock=True)
		self.validate_equipment_items(lock=True)

	# -- header ------------------------------------------------------------

	def set_default_company(self):
		"""FR-05: a plain default, not multi-company support - lets
		Payment Entry's get_reference_details() resolve currency the same
		way it already does for a Purchase Order reference."""
		if not self.company:
			self.company = erpnext.get_default_company()

	def set_project_tenders(self):
		"""FR-01, extended 2026-08-30: every Tender under the project is in
		scope, not just one - a project can carry more than one Tender
		(phased tendering, multiple categories, ...), and a single contract
		can draw work items from any of them, not just one auto-picked one.
		"""
		if not self.project:
			self.set("project_tenders", [])
			return

		# Follows Tender BRD (FR-03): a project with no Tender is a valid
		# contract now - its lines come from Work Item Templates instead, so
		# an empty list is no longer an error.
		tenders = get_project_tenders(self.project, self.department)
		self.set("project_tenders", [{"tender": t} for t in tenders])

	def project_tender_names(self):
		return {row.tender for row in self.project_tenders}

	def apply_follows_tender_rules(self):
		"""Follows Tender BRD FR-03/04/05/06.

		FR-03: no Project Tender -> the contract can only hold template
		lines. FR-04: the header switch only moves while Contracted Items is
		empty - checked against what was saved, since lines are what the
		mode actually governs. FR-05: each line is either a Tender line or a
		template line, fixed by which link it carries. FR-06: one row per
		template, except rows an approved Addendum inserted (those mirror
		how Tender-line addenda add a second row today).
		"""
		if not self.project_tenders:
			self.follows_tender = 0

		before = self.get_doc_before_save()
		if (
			before
			and before.get("contracted_items")
			and cint(before.follows_tender) != cint(self.follows_tender)
		):
			frappe.throw(_("Remove all Contracted Items before changing Follows Tender."))

		seen_templates = {}
		for row in self.contracted_items:
			if bool(row.work_item) == bool(row.work_item_template):
				frappe.throw(
					_("Row {0}: set exactly one of Work Item (Tender line) or Work Item Template "
					  "(template line).").format(row.idx)
				)
			row.follows_tender = 1 if row.work_item else 0

			if row.work_item_template and not row.addendum:
				if row.work_item_template in seen_templates:
					frappe.throw(
						_("Work Item Template {0} is already on row {1}.").format(
							frappe.bold(row.work_item_template), seen_templates[row.work_item_template]
						)
					)
				seen_templates[row.work_item_template] = row.idx

	def validate_no_overlapping_contract(self):
		"""2026-10-18 BRD FR-06: on Submit for Approval, block when another
		live contract for the same contractor and project already covers any
		of this contract's work items. A work item is shared when the
		Tender BOQ line is the same, or when the Work Item Template is the
		same on either side (a template-mode line vs a BOQ line carrying that
		template). Only checked on the move out of Draft, so existing
		duplicates and later edits are untouched.
		"""
		before = self.get_doc_before_save()
		if self.workflow_state != SUBMITTED_STATE:
			return
		if before and (before.workflow_state or "") not in EDITABLE_STATES:
			return
		if not (self.contractor_name and self.project and self.contracted_items):
			return

		# Serialise concurrent submissions for the same contractor: the rival
		# rows being compared are not the ones about to change, so locking
		# them would not stop two simultaneous submits both passing.
		frappe.db.sql("select name from `tabSupplier` where name=%s for update", (self.contractor_name,))

		rivals = frappe.get_all(
			"Subcontractor Contract",
			filters={
				"contractor_name": self.contractor_name,
				"project": self.project,
				"docstatus": ["<", 2],
				"is_closed": 0,
				"workflow_state": ["not in", ["Rejected", "cancelled"]],
				"name": ["!=", self.name],
			},
			pluck="name",
		)
		if not rivals:
			return

		my_items, my_templates = set(), {}
		for row in self.contracted_items:
			if row.work_item:
				my_items.add(row.work_item)
			template = row.work_item_template or (
				frappe.db.get_value("Tender BOQ Item", row.work_item, "work_item_template") if row.work_item else None
			)
			if template:
				my_templates.setdefault(template, row.work_item or template)

		conflicts = {}
		for rival_row in frappe.db.sql(
			"""select cci.parent, cci.work_item,
				coalesce(nullif(cci.work_item_template, ''), boq.work_item_template) as template
			from `tabContractor Contract Item` cci
			left join `tabTender BOQ Item` boq on boq.name = cci.work_item
			where cci.parenttype = 'Subcontractor Contract' and cci.parent in %(rivals)s""",
			{"rivals": rivals},
			as_dict=True,
		):
			if rival_row.work_item and rival_row.work_item in my_items:
				conflicts.setdefault(rival_row.parent, set()).add(rival_row.work_item)
			elif rival_row.template and rival_row.template in my_templates:
				conflicts.setdefault(rival_row.parent, set()).add(rival_row.template)

		if conflicts:
			frappe.throw(
				_("This contractor already has a live contract on this project that covers the same work items. "
				  "{0}").format(
					"; ".join(
						_("{0}: {1}").format(frappe.bold(name), ", ".join(sorted(items)))
						for name, items in sorted(conflicts.items())
					)
				),
				title=_("Overlapping Contract"),
			)

	def validate_subcontractor(self):
		if not self.contractor_name:
			return

		if not frappe.db.get_value("Supplier", self.contractor_name, "custom_is_subcontractor"):
			frappe.throw(
				_("Supplier {0} is not marked as a subcontractor. Set 'Is Subcontractor' on the supplier first.").format(
					frappe.bold(self.contractor_name)
				)
			)

	def enforce_frozen_figures(self):
		"""FR-20: contracted items and charges are read-only outside Draft.

		The field-level read_only_depends_on covers the UI; this covers the
		API and Data Import, and is what actually guarantees an approval
		reflects the figures the next approver sees.
		"""
		before = self.get_doc_before_save()
		if not before:
			return
		if (before.workflow_state or "") in EDITABLE_STATES:
			return

		for fieldname in (
			"contracted_items", "additional_costs", "payment_conditions",
			"contract_material_items", "contract_labor_items", "contract_equipment_items",
			"project_tenders",
		):
			if self._table_signature(self.get(fieldname)) != self._table_signature(before.get(fieldname)):
				frappe.throw(
					_("{0} cannot be changed while this contract is in state {1}. "
					  "Move it back to Draft first - that restarts the approval chain."
					  ).format(_(self.meta.get_label(fieldname)), frappe.bold(before.workflow_state))
				)

	@staticmethod
	def _table_signature(rows):
		return [
			(
				row.get("work_item"),
				row.get("work_item_template"),
				row.get("item"),
				row.get("labor_item"),
				row.get("equipment_item"),
				row.get("material_item"),
				flt(row.get("qty")),
				flt(row.get("unit_price")),
				row.get("tax_charge_type"),
				row.get("cost_category"),
				row.get("charge_type"),
				row.get("category"),
				row.get("row_id"),
				row.get("account_head"),
				row.get("cost_center"),
				row.get("rate_type"),
				flt(row.get("rate")),
				flt(row.get("tax_amount")),
				row.get("add_deduct_tax"),
				flt(row.get("total")),
				row.get("condition"),
				flt(row.get("percent")),
				row.get("tender"),
			)
			for row in (rows or [])
		]

	def validate_attachment(self):
		"""FR-13: needed to submit, not to draft."""
		if not self.attached_contract:
			frappe.throw(
				_("Attach the signed contract (Attached Contract) before submitting. "
				  "Saving as a draft without it is fine.")
			)

	# -- contracted items --------------------------------------------------

	def validate_contracted_items(self):
		if not self.contracted_items:
			return

		project_tenders = self.project_tender_names()
		for row in self.contracted_items:
			if not row.work_item:
				# Template line (FR-05): descriptive fields come off the
				# template; no Tender membership/availability applies (FR-09).
				template = frappe.db.get_value(
					TEMPLATE_DOCTYPE, row.work_item_template, ["template_name", "description", "uom"], as_dict=True
				)
				if not template:
					frappe.throw(_("Row {0}: Work Item Template {1} does not exist.").format(
						row.idx, frappe.bold(row.work_item_template)))
				row.description = template.description or template.template_name
				row.uom = template.uom
				self.validate_row_resource(row, None)
				continue

			work_item_row = allocation.get_work_item(row.work_item)

			if work_item_row.parent not in project_tenders:
				frappe.throw(
					_("Row {0}: work item {1} does not belong to any of this contract's Project "
					  "Tenders ({2}).").format(
						row.idx, frappe.bold(row.work_item), frappe.bold(", ".join(project_tenders) or "")
					)
				)

			# Descriptive only - deliberately no cost, margin or sell price
			# is copied from the tender (FR-06). Falls back to item_name
			# since most live BOQ rows still have description blank
			# (FR-SC-01/02).
			row.description = work_item_row.description or work_item_row.item_name
			row.uom = work_item_row.uom

			self.validate_row_resource(row, work_item_row)

		self.validate_allocation()

	def validate_row_resource(self, row, work_item_row):
		"""Deprecated (FR-11-19): resource selection now lives on the
		Labor/Equipment tabs (validate_labor_items / validate_equipment_items
		below), not on the Contracted Items row itself. Supply and Install
		never used these fields; Install Only / Equipment used to, so this
		just keeps clearing them rather than leaving stale values from
		before a type change or from pre-migration data.
		"""
		row.labor_item = None
		row.equipment_item = None
		row.resource_item = None

	def validate_allocation(self, lock=False):
		"""FR-07-FR-10 via the shared reconciliation util.

		Only meaningful for Supply and Install: that type's Contracted
		Items row *is* the allocation-relevant claim (a whole BOQ line's
		quantity). Install Only / Equipment now carry their
		allocation-relevant claim on the Labor/Equipment tabs instead
		(validate_labor_items / validate_equipment_items) - for those
		types, Contracted Items is a financial line only, decoupled from
		any specific resource row.

		Own rows are aggregated per work item first (a contract can carry
		more than one row against the same one), then compared against
		what is available once everything committed elsewhere is taken out.
		"""
		tender_lines = [row for row in self.contracted_items if row.work_item]
		if not tender_lines or self.type_subcontractor != SUPPLY_AND_INSTALL:
			return

		if lock:
			allocation.lock_work_items([row.work_item for row in tender_lines])

		own = {}
		for row in tender_lines:
			entry = own.setdefault(row.work_item, {"qty": 0.0, "idx": row.idx, "rows": []})
			entry["qty"] += flt(row.qty)
			entry["rows"].append(row)

		for work_item, entry in own.items():
			available = allocation.get_row_availability(
				self.type_subcontractor, work_item, exclude_contract=self.name,
			)

			# Show what this document may still take, not a stale figure.
			for row in entry["rows"]:
				row.available_qty = available

			if entry["qty"] > available + TOLERANCE:
				frappe.throw(
					_("Row {0}: cannot allocate {1} - only {2} remains available for {3}. "
					  "Availability is reconciled across all contract types, so quantity committed "
					  "under another type on the same work item reduces this."
					  ).format(entry["idx"], flt(entry["qty"]), flt(available), frappe.bold(work_item))
				)

	# -- resource tabs (FR-11-FR-19) ---------------------------------------

	def prune_orphaned_resource_rows(self):
		"""FR-20: cascade-delete. A resource row whose work item is no
		longer in Contracted Items is orphaned - drop it.

		The client mirrors this immediately on row removal
		(subcontractor_contract.js), so this normally only confirms what
		the grid already did. It is what actually guarantees the rule
		under the API and Data Import, which bypass the client entirely.
		"""
		# Keyed on line_key (Follows Tender BRD): a line is a Tender work
		# item *or* a Work Item Template. Only rows that belonged to a line
		# that actually existed (on the saved document, or auto-propagated
		# from one) are cascade-deleted - a hand-added extra row pointing at
		# something that was never a line is an input error, not an orphan,
		# and says so instead of silently disappearing (FR-08).
		surviving = {line_key(row) for row in self.contracted_items} - {None}
		before = self.get_doc_before_save()
		before_lines = {line_key(row) for row in before.contracted_items} if before else set()

		for fieldname in ("contract_material_items", "contract_labor_items", "contract_equipment_items"):
			rows = self.get(fieldname)
			kept = []
			for row in rows:
				key = line_key(row)
				if key is None or key in surviving:
					# key None: left for _validate_resource_rows to reject.
					kept.append(row)
				elif key in before_lines or row.auto_propagated:
					continue
				else:
					frappe.throw(
						_("{0} row {1}: {2} is not a line on Contracted Items. Add the line first, "
						  "or point the row at an existing line.").format(
							_(self.meta.get_label(fieldname)), row.idx, frappe.bold(key[1])
						)
					)
			if len(kept) != len(rows):
				self.set(fieldname, kept)

	def validate_material_items(self, lock=False):
		"""FR-11/16-18: the Material tab only applies to Supply and Install."""
		if self.type_subcontractor != SUPPLY_AND_INSTALL:
			self.set("contract_material_items", [])
			return
		self._validate_resource_rows("contract_material_items", MATERIAL_DOCTYPE, "material_item", lock=lock)

	def validate_labor_items(self, lock=False):
		"""FR-11/19: the Labor tab applies to Install Only, and (FR-06,
		2026-08-30) Supply and Install - RESOURCE_PROPAGATION already
		declares Supply and Install feeds all three resource tabs; this
		gate has to agree or it wipes right back out what the client just
		propagated."""
		if self.type_subcontractor not in (*LABOR_TYPES, SUPPLY_AND_INSTALL):
			self.set("contract_labor_items", [])
			return
		self._validate_resource_rows("contract_labor_items", LABOR_DOCTYPE, "labor_item", lock=lock)

	def validate_equipment_items(self, lock=False):
		"""FR-11: the Equipment tab applies to Equipment contracts, and
		(FR-06, 2026-08-30) Supply and Install - see validate_labor_items."""
		if self.type_subcontractor not in (EQUIPMENT, SUPPLY_AND_INSTALL):
			self.set("contract_equipment_items", [])
			return
		self._validate_resource_rows("contract_equipment_items", EQUIPMENT_DOCTYPE, "equipment_item", lock=lock)

	def _validate_resource_rows(self, fieldname, doctype, link_fieldname, lock=False):
		"""Shared by the three tab validators above: every row's resource
		has to belong to its selected work item (TC-11, generalised off the
		validation Contracted Items used to do inline), and rows sharing
		one resource can't together exceed what
		allocation.get_resource_availability says is free - the same
		aggregate-then-compare shape validate_allocation uses for
		Contracted Items, now keyed by resource row instead of by work item
		since one resource row belongs to exactly one work item already.

		lock mirrors validate_allocation's own lock parameter (NFR-02): set
		from before_submit, to serialise concurrent submissions against the
		same work item inside the submitting transaction.
		"""
		rows = self.get(fieldname)
		if not rows:
			return

		if lock:
			allocation.lock_work_items([row.work_item for row in rows if row.work_item and row.get(link_fieldname)])

		project_tenders = self.project_tender_names()
		tender_lines = {row.work_item for row in self.contracted_items if row.work_item}
		own = {}
		for row in rows:
			if bool(row.work_item) == bool(row.work_item_template):
				frappe.throw(
					_("{0} row {1}: set exactly one of Work Item or Work Item Template.").format(
						_(self.meta.get_label(fieldname)), row.idx
					)
				)

			if row.get(link_fieldname):
				row.source = "Tender"
			elif row.work_item_template and row.auto_propagated:
				row.source = "Template"
			else:
				row.source = "Extra"

			if row.source != "Tender":
				# FR-08/09: template-sourced and extra rows carry a free Item
				# under the tab's Item rule, and draw on no Tender quantity.
				if not row.item:
					frappe.throw(
						_("{0} row {1}: Item is required.").format(_(self.meta.get_label(fieldname)), row.idx)
					)
				# Rows copied from the template are the template's own data (its
				# item picker rule is client-side only, so it can hold items this
				# rule rejects). Only hand-added Extra rows are held to the rule.
				if row.source != "Template":
					allocation.validate_resource_item(fieldname, row.item, row.idx)
				if row.work_item and row.work_item not in tender_lines:
					frappe.throw(
						_("{0} row {1}: work item {2} is not a Tender line on Contracted Items.").format(
							_(self.meta.get_label(fieldname)), row.idx, frappe.bold(row.work_item)
						)
					)
				item_name, stock_uom = frappe.db.get_value("Item", row.item, ["item_name", "stock_uom"])
				row.resource_item = item_name or row.item
				row.uom = stock_uom
				row.available_qty = 0
				continue

			if not row.work_item:
				frappe.throw(
					_("{0} row {1}: a Tender resource row needs its Tender Work Item.").format(
						_(self.meta.get_label(fieldname)), row.idx
					)
				)

			work_item_row = allocation.get_work_item(row.work_item)
			if work_item_row.parent not in project_tenders:
				frappe.throw(
					_("Row {0}: work item {1} does not belong to any of this contract's Project "
					  "Tenders ({2}).").format(
						row.idx, frappe.bold(row.work_item), frappe.bold(", ".join(project_tenders) or "")
					)
				)
			row.uom = work_item_row.uom

			resource_value = row.get(link_fieldname)
			if not resource_value:
				label = frappe.get_meta(row.doctype).get_label(link_fieldname)
				frappe.throw(_("Row {0}: {1} is required.").format(row.idx, _(label)))

			resource_row = allocation.get_resource_row(doctype, resource_value)
			row.resource_item = resource_row.item
			# One Item column labels every row in the grid, whatever its source.
			row.item = resource_row.item

			if not allocation.resource_belongs_to_work_item(resource_row, work_item_row):
				frappe.throw(
					_("Row {0}: {1} {2} does not belong to work item {3}.").format(
						row.idx, _(doctype), frappe.bold(resource_value),
						frappe.bold(work_item_row.item_name or row.work_item)
					)
				)

			entry = own.setdefault(resource_value, {"qty": 0.0, "idx": row.idx, "rows": []})
			entry["qty"] += flt(row.qty)
			entry["rows"].append(row)

		for resource_value, entry in own.items():
			available = allocation.get_resource_availability(doctype, resource_value, exclude_contract=self.name)

			for row in entry["rows"]:
				row.available_qty = available

			if entry["qty"] > available + TOLERANCE:
				frappe.throw(
					_("Row {0}: cannot allocate {1} - only {2} remains available for {3}. "
					  "Availability is reconciled across all contract types, so quantity "
					  "committed elsewhere against the same resource reduces this."
					  ).format(entry["idx"], flt(entry["qty"]), flt(available), frappe.bold(resource_value))
				)

	# -- payment conditions (FR-21-FR-24) -----------------------------------

	def prune_orphaned_payment_condition_resources(self):
		"""Edge case: a condition's resource tag falls back to whole-work-item
		scope if that resource row was removed from the contract - same
		cascade-clear philosophy prune_orphaned_resource_rows already
		applies to the resource tabs themselves.
		"""
		live = {
			"material_item": {r.name for r in self.contract_material_items},
			"labor_item": {r.name for r in self.contract_labor_items},
			"equipment_item": {r.name for r in self.contract_equipment_items},
		}
		for row in self.payment_conditions:
			for fieldname, alive in live.items():
				value = row.get(fieldname)
				if value and value not in alive:
					row.set(fieldname, None)
					row.category = None

	def validate_payment_condition_resources(self):
		"""FR-14: category is gated by the same server-side truth that
		actually populates the resource tabs (LABOR_TYPES/EQUIPMENT/
		SUPPLY_AND_INSTALL), not the JSON depends_on strings, which disagree
		with it for Labor/Equipment on a Supplying and installing contract -
		offering a category the tabs can never populate would be a dead end.

		FR-15: a tagged resource must match its row's category, and (if the
		row's work_item is also set) must belong to that same work item.
		"""
		tables = {
			"material_item": {r.name: r for r in self.contract_material_items},
			"labor_item": {r.name: r for r in self.contract_labor_items},
			"equipment_item": {r.name: r for r in self.contract_equipment_items},
		}

		template_lines = {r.work_item_template for r in self.contracted_items if r.work_item_template}
		for row in self.payment_conditions:
			if row.work_item and row.work_item_template:
				frappe.throw(
					_("Payment Conditions row {0}: set Work Item or Work Item Template, not both.").format(row.idx)
				)
			if row.work_item_template and row.work_item_template not in template_lines:
				frappe.throw(
					_("Payment Conditions row {0}: Work Item Template {1} is not a line on this contract.").format(
						row.idx, frappe.bold(row.work_item_template)
					)
				)

			if row.category:
				allowed_types = CATEGORY_TYPE_GATE.get(row.category, ())
				if self.type_subcontractor not in allowed_types:
					frappe.throw(
						_("Row {0}: Category {1} is not applicable to a {2} contract."
						  ).format(row.idx, row.category, self.type_subcontractor)
					)

			set_fields = [f for f in ("material_item", "labor_item", "equipment_item") if row.get(f)]
			if not set_fields:
				continue
			if len(set_fields) > 1:
				frappe.throw(_("Row {0}: set only one of Material/Labor/Equipment Item.").format(row.idx))

			fieldname = set_fields[0]
			expected_category = _RESOURCE_FIELD_BY_CATEGORY_REVERSE[fieldname]
			if row.category != expected_category:
				frappe.throw(
					_("Row {0}: {1} is set, so Category must be {2}.").format(
						row.idx, frappe.get_meta(row.doctype).get_label(fieldname), expected_category
					)
				)

			resource_row = tables[fieldname].get(row.get(fieldname))
			if resource_row and line_key(row) and line_key(resource_row) != line_key(row):
				frappe.throw(
					_("Row {0}: the selected resource belongs to a different work item than this "
					  "row's Work Item.").format(row.idx)
				)

	def validate_payment_conditions(self):
		"""FR-22/FR-23: each condition group must sum to 100% - the
		contract-wide default (rows with no Work Item) and, independently,
		each work item's override set (rows naming that Work Item).
		"""
		allocation.validate_condition_groups(self.payment_conditions)

	# -- material conflict (FR-14 / FR-15) ---------------------------------

	def validate_material_conflicts(self):
		"""FR-14: a Supply-and-Install contract for a work item the company
		is already buying material for needs Cost Control's explicit override.

		The signal is a submitted Material Request line pointing at the same
		work item - which means the company may already be procuring exactly
		what this subcontractor is being paid to supply.

		Conflicts are collected onto the *header* table, tagged with the work
		item they belong to. The BRD puts this table on the contracted item
		row, but Frappe cannot render a Table field inside a grid row form
		(the control has no docfields there and throws), so the row keeps
		only its override checkbox and the detail lives on the parent.
		"""
		self.set("existing_material_requests", [])

		if self.type_subcontractor != SUPPLY_AND_INSTALL:
			for row in self.contracted_items:
				row.material_conflict_override = 0
			return

		# One work item can appear on several rows; look it up once.
		by_work_item = {}
		for row in self.contracted_items:
			if not row.work_item:
				continue  # template line: no Tender work item to conflict with (FR-09)
			if row.work_item not in by_work_item:
				by_work_item[row.work_item] = find_material_conflicts(row.work_item)

		for work_item, conflicts in by_work_item.items():
			for conflict in conflicts:
				entry = dict(conflict)
				entry["work_item"] = work_item
				entry["is_tagged_for_deduction"] = 1 if (
					entry.pop("_tagged_supplier", None) == self.contractor_name
				) else 0
				self.append("existing_material_requests", entry)

		for row in self.contracted_items:
			conflicts = [
				c for c in self.existing_material_requests if row.work_item and c.work_item == row.work_item
			]
			if not conflicts:
				row.material_conflict_override = 0
				continue

			self._validate_conflict_override(row, conflicts)

	def _validate_conflict_override(self, row, conflicts):
		if not row.material_conflict_override:
			frappe.throw(
				_("Row {0}: {1} Material Request(s) already exist against work item {2}, "
				  "so the company may already be procuring material this subcontractor is "
				  "meant to supply. See the Material Conflicts table. A user with the '{3}' "
				  "role must tick Material Conflict Override on the row to proceed."
				  ).format(
					row.idx, len(conflicts),
					frappe.bold(row.description or row.work_item), frappe.bold(COST_CONTROL_ROLE),
				)
			)

		# Server-side, not merely a hidden field: the UI restriction is
		# bypassable by API and Data Import.
		if COST_CONTROL_ROLE not in frappe.get_roles(frappe.session.user):
			frappe.throw(
				_("Row {0}: only a user with the '{1}' role may set Material Conflict Override.").format(
					row.idx, frappe.bold(COST_CONTROL_ROLE)
				)
			)

		# FR-15: the override alone isn't enough - the conflicting material
		# has to actually be tagged for recovery from this subcontractor,
		# otherwise the company silently pays for it twice.
		#
		# The manual fallback charge is the accepted alternative (TC-19):
		# once material has been fully invoiced there is nothing left to tag,
		# so the recovery has to be a deduction on the contract instead.
		tagged = any(c.is_tagged_for_deduction for c in conflicts)
		manual_fallback = any(
			c.cost_category == MANUAL_FALLBACK_CHARGE for c in self.additional_costs
		)
		if not tagged and not manual_fallback:
			frappe.throw(
				_("Row {0}: override requires at least one conflicting Material Request line "
				  "to be tagged for subcontractor deduction against {1}. Set "
				  "'Procured on Subcontractor's Behalf' and the Deduction Subcontractor on the "
				  "conflicting Material Request first, or - if that material was already fully "
				  "invoiced - add a 'Material Supplied by Company (Manual Fallback)' row to "
				  "Additional Costs instead."
				  ).format(row.idx, frappe.bold(self.contractor_name or ""))
			)

	# -- cost center ---------------------------------------------------------

	def default_charge_cost_centers(self):
		"""FR-18: a blank Additional Costs row's Cost Center defaults from the
		linked Project, so charges post against the Project's own cost center
		instead of silently falling through to the Company default further
		downstream (create_purchase_invoice / create_sales_invoice).

		Only fills blanks - a row with its own Cost Center set (manually, or
		from an earlier save before the Project had one) is left alone.
		"""
		if not self.project:
			return

		project_cost_center = frappe.db.get_value("Project", self.project, "cost_center")
		if not project_cost_center:
			return

		for row in self.additional_costs:
			if not row.cost_center:
				row.cost_center = project_cost_center

	# -- pricing mode / price ceiling (2026-09-27 BRD) ----------------------

	def _figures_are_frozen(self):
		"""Same guard enforce_frozen_figures uses: once a document has left
		Draft, contracted_items/resource rows are guaranteed unchanged (that
		method throws earlier in validate() if they aren't) - so pricing
		mode/ceiling logic below has nothing new to compute or check on a
		save that starts from a non-Draft state. This is what keeps the
		price ceiling from retroactively validating the 10 already-Approved
        contracts' historical pricing (BRD Non-Goal)."""
		before = self.get_doc_before_save()
		return bool(before) and (before.workflow_state or "") not in EDITABLE_STATES

	def compute_component_pricing(self):
		"""FR-01/02/14: in Components mode, unit_price is never independently
		editable - it's a pure rollup of this row's priced Material/Labor/
		Equipment rows, the same relationship Tender BOQ Item.total_amount
		already has to its own resource rows.

		validate_material_items/validate_labor_items/validate_equipment_items
		(above, earlier in validate()) have already pruned whichever of the
		three resource tables don't apply to this contract's
		type_subcontractor down to [] - so summing across all three
		unfiltered here already respects CATEGORY_TYPE_GATE without
		re-checking it.
		"""
		if self._figures_are_frozen():
			return

		resource_rows = (
			list(self.contract_material_items)
			+ list(self.contract_labor_items)
			+ list(self.contract_equipment_items)
		)

		for row in self.contracted_items:
			if row.pricing_mode != "Components":
				continue

			matching = [r for r in resource_rows if line_key(r) == line_key(row)]
			if not matching:
				frappe.throw(
					_("Row {0}: {1} is priced by Components but has no Material/Labor/"
					  "Equipment rows. Add priced component rows, or switch this row back "
					  "to Total pricing.").format(row.idx, frappe.bold(row.description or row.work_item))
				)

			component_total = sum(flt(r.qty) * flt(r.rate) for r in matching)
			row.unit_price = component_total / flt(row.qty) if row.qty else 0.0

	def validate_price_ceiling(self):
		"""FR-04/05/06: a Contracted Items row's per-unit price - however it
		was arrived at (Total or Components mode) - may not exceed its work
		item's own Tender Direct Cost per unit, without an acknowledged
		price exception. Never discloses the Tender's actual cost/price
		figures in the block message (FR-05).

		Skipped once the contract has left Draft (see _figures_are_frozen) -
		contracted_items are frozen by then, so this would otherwise
		retroactively flag already-Approved contracts priced before this
		ceiling existed (BRD Non-Goal).
		"""
		if self._figures_are_frozen():
			return

		for row in self.contracted_items:
			if not row.work_item:
				continue  # template line: no Tender ceiling to check (FR-09)

			work_item_row = allocation.get_work_item(row.work_item)

			if not work_item_row.original_quantity:
				continue

			ceiling = flt(work_item_row.boq_direct_cost) / flt(work_item_row.original_quantity)
			if flt(row.unit_price) <= ceiling + TOLERANCE:
				continue

			if self.price_exception_acknowledged:
				continue

			frappe.throw(
				_("Row {0}: the price for {1} exceeds this bench's approved ceiling for that "
				  "work item. Use 'Delegate to Cost Control' to proceed anyway.").format(
					row.idx, frappe.bold(row.description or row.work_item)
				)
			)

	# -- Tender reference / out-of-Tender flag (Follows Tender BRD) ---------

	def compute_tender_reference(self):
		"""FR-15: what the Tender (or, for a template line, the template)
		expected to pay - permlevel 1, so only Cost Control and above ever
		receive it. Tender resource rates are read in company currency
		(rate_egp), the same currency the contract's own rates are in.
		Frozen once the contract leaves Draft (NFR-02).
		"""
		if self._figures_are_frozen():
			return

		for row in self.contracted_items:
			if row.work_item:
				work_item_row = allocation.get_work_item(row.work_item)
				row.tender_unit_cost = (
					flt(work_item_row.boq_direct_cost) / flt(work_item_row.original_quantity)
					if flt(work_item_row.original_quantity) else 0
				)
			else:
				row.tender_unit_cost = flt(
					frappe.db.get_value(TEMPLATE_DOCTYPE, row.work_item_template, "total_cost_per_unit")
				)

		template_rates = {}
		for fieldname, (resource_doctype, link_fieldname) in self._resource_tables().items():
			template_table = allocation.TEMPLATE_TABLE_BY_CONTRACT_TABLE[fieldname]
			for row in self.get(fieldname):
				if row.source == "Tender":
					row.tender_rate = flt(frappe.db.get_value(resource_doctype, row.get(link_fieldname), "rate_egp"))
				elif row.source == "Template":
					key = (row.work_item_template, template_table)
					if key not in template_rates:
						template_rates[key] = {
							r.item: flt(r.rate) for r in frappe.get_all(
								allocation.TEMPLATE_CHILD_DOCTYPE[template_table],
								filters={"parent": row.work_item_template, "parenttype": TEMPLATE_DOCTYPE},
								fields=["item", "rate"],
							)
						}
					row.tender_rate = template_rates[key].get(row.item)
				else:
					row.tender_rate = None

	def compute_out_of_tender_scope(self):
		"""FR-10/11: a line or resource row is out of the original Tender's
		scope (shown red, forces Cost Control's confirmation and CEO
		approval) when the contract has a Project Tender and:
		  (a) the line is a template line;
		  (b) a Tender line is priced above its Tender unit cost (only
		      saveable after 'Delegate to Cost Control');
		  (c) a resource row did not come from the Tender (template / extra);
		  (d) a Tender resource row's rate is above the Tender's own rate.
		A project with no Tender has nothing to exceed - every flag is 0.
		Frozen once the contract leaves Draft (NFR-02).
		"""
		if self._figures_are_frozen():
			return

		has_tender = bool(self.project_tenders)
		any_out = False

		for row in self.contracted_items:
			out = has_tender and (
				not row.work_item
				or (flt(row.tender_unit_cost) > 0 and flt(row.unit_price) > flt(row.tender_unit_cost) + TOLERANCE)
			)
			row.out_of_tender_scope = 1 if out else 0
			any_out = any_out or out

		for fieldname in self._resource_tables():
			for row in self.get(fieldname):
				out = has_tender and (
					row.source != "Tender"
					or (row.tender_rate is not None and flt(row.tender_rate) > 0
						and flt(row.rate) > flt(row.tender_rate) + TOLERANCE)
				)
				row.out_of_tender_scope = 1 if out else 0
				any_out = any_out or out

		self.has_out_of_tender_items = 1 if any_out else 0

	@staticmethod
	def _resource_tables():
		return {
			"contract_material_items": (MATERIAL_DOCTYPE, "material_item"),
			"contract_labor_items": (LABOR_DOCTYPE, "labor_item"),
			"contract_equipment_items": (EQUIPMENT_DOCTYPE, "equipment_item"),
		}

	# -- totals ------------------------------------------------------------

	def calculate_totals(self):
		self.net_total = 0.0
		for row in self.contracted_items:
			row.amount = flt(row.qty) * flt(row.unit_price)
			self.net_total += flt(row.amount)

		total_charges = 0.0
		running_total = flt(self.net_total)
		for row in self.additional_costs:
			amount = self._compute_charge_amount(row)
			row.tax_amount = amount

			signed = -amount if row.add_deduct_tax == "Deduct" else amount
			running_total += signed
			row.total = running_total
			total_charges += signed

		self.total_additional_charges = total_charges
		self.grand_total = flt(self.net_total) + flt(total_charges)

	def _compute_charge_amount(self, row):
		"""FR-06: same 5-way cascade as core Purchase Taxes and Charges.

		Rate Type overrides that cascade: Fixed Amount means Rate is charged
		as-is regardless of Charge Type, making Charge Type's base (net
		total, previous row, item quantity) irrelevant for this row's own
		amount. Percentage (the default, so every pre-existing row is
		unaffected) falls through to the cascade unchanged.
		"""
		if (row.rate_type or "Percentage") == "Fixed Amount":
			return flt(row.rate)

		charge_type = row.charge_type or "On Net Total"

		if charge_type == "Actual":
			return flt(row.tax_amount)
		if charge_type == "On Net Total":
			return flt(self.net_total) * flt(row.rate) / 100.0
		if charge_type in ("On Previous Row Amount", "On Previous Row Total"):
			ref_row = self._get_referenced_charge_row(row)
			base = flt(ref_row.tax_amount) if charge_type == "On Previous Row Amount" else flt(ref_row.total)
			return base * flt(row.rate) / 100.0
		if charge_type == "On Item Quantity":
			total_qty = sum(flt(r.qty) for r in self.contracted_items)
			return flt(row.rate) * total_qty

		frappe.throw(_("Row {0}: unrecognised Charge Type {1}.").format(row.idx, charge_type))

	def _get_referenced_charge_row(self, row):
		try:
			ref_idx = int(row.row_id)
		except (TypeError, ValueError):
			frappe.throw(
				_("Row {0}: Reference Row # is required and must be a row number when Charge Type "
				  "is {1}.").format(row.idx, row.charge_type)
			)
		ref_row = next((r for r in self.additional_costs if r.idx == ref_idx), None)
		if not ref_row:
			frappe.throw(
				_("Row {0}: Reference Row # {1} does not exist in Additional Costs.").format(row.idx, row.row_id)
			)
		return ref_row

	def validate_retention_rows(self):
		"""FR-12: at most one Retention row. FR-17: a Retention row must
		withhold money, not add it.

		Additional Costs is optional in full - a contract may carry no charges
		at all, and none of them is individually required. But when a Retention
		row *is* present it is the agreed retention for the contract and the
		source of truth Contractor Invoice reads from, so a second one would
		make that lookup ambiguous.

		No Retention row is a legitimate contract; it just means nothing is
		withheld. before_submit() says so out loud rather than letting it pass
		unnoticed.
		"""
		count = self._retention_row_count()
		if count > 1:
			frappe.throw(
				_("Only one 'Retention' row is allowed in Additional Costs - found {0}.").format(count)
			)

		for row in self.additional_costs:
			if row.cost_category == RETENTION_CHARGE and row.add_deduct_tax != "Deduct":
				frappe.throw(
					_("Contract {0}, Additional Costs row {1}: a Retention charge must be Deduct, not {2}.")
					.format(self.name or _("(unsaved)"), row.idx, row.add_deduct_tax)
				)

	def _retention_row_count(self):
		return len([r for r in self.additional_costs if r.cost_category == RETENTION_CHARGE])

	def warn_if_no_retention(self):
		"""Retention is optional, but its absence is worth saying out loud.

		Contractor Invoice reads its Retention % from the contract's Retention
		row, so with no row every invoice against this contract withholds 0%.
		Deliberately msgprint and not throw - this is a valid contract.
		"""
		if self._retention_row_count():
			return

		frappe.msgprint(
			_("No 'Retention' row in Additional Costs, so invoices against this contract "
			  "will withhold 0% retention."),
			title=_("No Retention Held"),
			indicator="orange",
		)

	def set_ceo_approval_flag(self):
		"""Precompute the workflow's CEO-threshold condition (2026-09-27 BRD,
		FR-07). Replaces the old whole-contract Contracting Settings.
		ceo_approval_value_threshold check entirely - that field is left in
		place but no longer read here.

		Frappe evaluates workflow conditions through safe_eval with only
		frappe.db.get_value / get_list exposed, so this has to be
		precomputed here rather than read live from the workflow condition.
		FR-20 freezes contracted_items mid-chain, so this flag can't go
		stale once set - guarded the same way as validate_price_ceiling,
		since (b)'s cross-contract lookup means a naive unconditional
		recompute could otherwise flip an already-Approved contract's flag
		based on some *other*, later contract's approval.

		Requires CEO approval when either:
		  (a) any Contracted Items row(s) for a single work_item - summed
		      together when more than one row shares that work_item, same
		      aggregation validate_allocation already does - total at least
		      CEO_APPROVAL_WORK_ITEM_THRESHOLD, and that work_item has never
		      appeared on any *other* Approved Subcontractor Contract; or
		  (b) a price exception was acknowledged via 'Delegate to Cost
		      Control' (validate_price_ceiling).
		"""
		if self._figures_are_frozen():
			return

		by_line = {}
		for row in self.contracted_items:
			key = line_key(row)
			by_line[key] = by_line.get(key, 0.0) + flt(row.amount)

		def previously_approved(key):
			# Follows Tender BRD OQ-4: a template line is a "work item" for
			# this rule too, tracked by its own template.
			if key[0] == "tender":
				return allocation.work_item_previously_approved(key[1], exclude_contract=self.name)
			return allocation.template_previously_approved(key[1], exclude_contract=self.name)

		over_threshold_and_new = any(
			amount >= CEO_APPROVAL_WORK_ITEM_THRESHOLD and not previously_approved(key)
			for key, amount in by_line.items()
			if key
		)

		# (c) Follows Tender BRD FR-13: anything outside the original Tender.
		self.requires_ceo_approval = 1 if (
			over_threshold_and_new or self.price_exception_acknowledged or self.has_out_of_tender_items
		) else 0

	def set_total_advance_paid(self):
		"""FR-06/FR-09: recomputed from submitted Payment Entry Reference
		rows referencing this contract - mirrors Purchase Order's own
		set_total_advance_paid() in spirit (accounts_controller.py), but as
		a direct sum over Payment Entry Reference rather than a Payment
		Ledger Entry query, since this doctype carries no PLE infrastructure
		(it's a plain Document, not an AccountsController).

		Called from custom_payment_entry's update_advance_paid() on Payment
		Entry submit/cancel, and explicitly from create_purchase_invoice()
		after an advance is reconciled into a Purchase Invoice - that path
		doesn't go through Payment Entry submit/cancel, so nothing else
		would refresh this value there.

		A direct frappe.db.set_value, not a full save: this runs
		mid-transaction from other doctypes' code and shouldn't re-trigger
		this document's own validate().
		"""
		advance_paid = flt(frappe.db.sql(
			"""
			select sum(per.allocated_amount)
			from `tabPayment Entry Reference` per
			inner join `tabPayment Entry` pe on pe.name = per.parent
			where per.reference_doctype = 'Subcontractor Contract'
			  and per.reference_name = %s
			  and pe.docstatus = 1
			""",
			self.name,
		)[0][0] or 0)
		frappe.db.set_value("Subcontractor Contract", self.name, "advance_paid", advance_paid)


def get_project_tenders(project, department=None):
	"""FR-01, extended 2026-08-30: every Tender in scope for a project, not
	just one - a project can carry more than one Tender (phased tendering,
	multiple categories, ...), and a Subcontractor Contract can draw work
	items from any of them, not just one auto-picked one.

	Two project shapes exist on this bench: legacy single-tender projects
	set Project.tender directly (returned as a 1-item list, unchanged
	behaviour); newer ones (post Won Project Tender BRD) go through
	Project Tender instead, which can carry several Tenders via Project
	Tender Direct Cost Detail. Project.tender is left blank on that newer
	shape, so a Subcontractor Contract against such a project used to see
	"no Tender" even though real Tenders existed underneath it.

	department (FR-SC-06), when given, narrows the result to Tenders whose
	own tender_category matches - so a contract scoped to one department
	only ever offers work items from that department's Tender(s).

	Shared by set_project_tenders (server validate) and
	get_project_tenders_for_project (the client's project-change handler),
	so both resolve the same way.
	"""
	if not project:
		return []

	tender = frappe.db.get_value("Project", project, "tender")
	if tender:
		tenders = [tender]
	else:
		project_tender = frappe.db.get_value("Project", project, "project_tender")
		if not project_tender:
			return []

		tenders = [
			t for t in frappe.get_all(
				"Project Tender Direct Cost Detail",
				filters={"parent": project_tender},
				order_by="idx",
				pluck="tender",
			)
			if t
		]

	if department and tenders:
		categories = frappe.db.get_values(
			"Tender", {"name": ["in", tenders]}, ["name", "tender_category"], as_dict=True
		)
		in_category = {row.name for row in categories if row.tender_category == department}
		tenders = [t for t in tenders if t in in_category]

	return tenders


@frappe.whitelist()
def get_project_tenders_for_project(project, department=None):
	"""Client-side counterpart to set_project_tenders - the project(frm)/
	department(frm) handlers in subcontractor_contract.js call this instead
	of reading Project.tender directly, so both resolve the same way.
	"""
	return get_project_tenders(project, department)


@frappe.whitelist()
def get_work_item_templates(txt=None):
	"""Rows for the "Select Work Item Template" dialog (FR-05). Every
	template, not filtered by contract - FR-06's one-row-per-template rule is
	enforced on save. Deliberately no cost figures: the dialog is open to
	Contracts Engineers, who don't see Tender/template cost (NFR-03).
	"""
	frappe.has_permission(TEMPLATE_DOCTYPE, "select", throw=True)
	filters = None
	if txt:
		filters = {"template_name": ["like", f"%{txt}%"]}
	return frappe.get_all(
		TEMPLATE_DOCTYPE,
		filters=filters,
		fields=["name", "template_name", "description", "uom"],
		order_by="template_name",
	)


@frappe.whitelist()
def close_contract(name):
	"""FR-11: marks a contract Closed, releasing any never-invoiced
	remaining quantity back to general availability (allocation.
	get_supply_install_committed reads is_closed on every call). Server-side
	role gate - the "Close Contract" button is also hidden client-side for
	other roles as defense in depth, matching material_conflict_override's
	own role-restriction pattern above."""
	if not set(frappe.get_roles(frappe.session.user)) & set(CLOSE_CONTRACT_ROLES):
		frappe.throw(
			_("Only {0} may close a contract.").format(" or ".join(CLOSE_CONTRACT_ROLES)),
			frappe.PermissionError,
		)

	workflow_state = frappe.db.get_value("Subcontractor Contract", name, "workflow_state")
	if workflow_state != "Approved":
		frappe.throw(_("Only an Approved contract can be closed."))

	frappe.db.set_value("Subcontractor Contract", name, "is_closed", 1)


def find_material_conflicts(work_item):
	"""Submitted Material Request lines drawing against the same work item.

	A contract row names a Tender BOQ Item; Material Request lines name a
	Project BOQ Item. They meet at Project BOQ Item.source_tender_boq_item,
	which Tender.carry_boq_to_project() populates when a tender is won.

	Cancelled requests are excluded - they aren't procuring anything.
	"""
	if not work_item:
		return []

	rows = frappe.db.sql(
		"""
		select
			mri.parent as material_request,
			mri.name as material_request_item,
			mri.item_code as item,
			mri.qty as qty,
			mri.amount as amount,
			mri.custom_is_subcontractor_deduction as tagged,
			mri.custom_deduction_subcontractor as tagged_supplier
		from `tabMaterial Request Item` mri
		inner join `tabMaterial Request` mr on mr.name = mri.parent
		inner join `tabProject BOQ Item` pbi on pbi.name = mri.custom_project_work_item
		where pbi.source_tender_boq_item = %(work_item)s
		  and mr.docstatus = 1
		order by mri.parent, mri.idx
		""",
		{"work_item": work_item},
		as_dict=True,
	)

	conflicts = []
	for row in rows:
		conflicts.append({
			"material_request": row.material_request,
			"material_request_item": row.material_request_item,
			"item": row.item,
			"qty": row.qty,
			"amount": row.amount,
			"_tagged_supplier": row.tagged_supplier if row.tagged else None,
		})
	return conflicts


@frappe.whitelist()
def get_unallocated_boq_items(tenders, contract_type, subcontractor_contract=None):
	"""Rows for the "Select Work Item" dialog - one row per BOQ line, for
	every contract type (2026-08-30 BRD: previously the labor/equipment
	types listed one row per resource line instead; unified so propagation
	has a single, consistent granularity to key off - see
	allocation.get_propagated_resource_rows). tenders is one or more Tender
	names (FR-01, extended 2026-08-30 - a project, and so a contract, can be
	in scope of more than one Tender) - each BOQ row already carries its own
	`parent`, so rows from different Tenders sit side by side in one result
	set.
	"""
	tenders = frappe.parse_json(tenders) if isinstance(tenders, str) else tenders
	tenders = [t for t in (tenders or []) if t]
	if not tenders:
		return []

	boq_rows = frappe.get_all(
		"Tender BOQ Item",
		filters={"parent": ["in", tenders], "is_group": 0},
		fields=["name", "item_name", "description", "item_code", "uom", "original_quantity", "idx", "parent"],
		order_by="parent, idx",
	)

	result = []
	for boq_row in boq_rows:
		available = allocation.get_row_availability(
			contract_type, boq_row.name, exclude_contract=subcontractor_contract
		)
		if available > TOLERANCE:
			result.append({
				"work_item": boq_row.name,
				"tender": boq_row.parent,
				# Not a field on the child row - the client uses it to prime the
				# link-title cache, then strips it before add_child.
				"item_name": boq_row.item_name,
				"description": boq_row.description or boq_row.item_name,
				"item_code": boq_row.item_code,
				"uom": boq_row.uom,
				"available_qty": available,
				# FR-SC-09: type-scoped, same population get_row_availability
				# above already reads.
				"contracted": boq_row.original_quantity - available,
				"qty": available,
			})

	return result
