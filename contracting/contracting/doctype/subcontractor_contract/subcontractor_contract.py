import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from contracting.contracting.utils import allocation
from contracting.contracting.utils.allocation import (
	EQUIPMENT,
	EQUIPMENT_DOCTYPE,
	LABOR_DOCTYPE,
	LABOR_TYPES,
	MATERIAL_DOCTYPE,
	SUPPLY_AND_INSTALL,
	TOLERANCE,
)

RETENTION_CHARGE = "Retention"
MANUAL_FALLBACK_CHARGE = "Material Supplied by Company (Manual Fallback)"

# Payment Condition category -> which contract types actually populate that
# resource tab (FR-14). Server-side truth, not the JSON depends_on strings -
# those disagree with this for Labor/Equipment on a Supplying and installing
# contract (see validate_labor_items / validate_equipment_items below).
CATEGORY_TYPE_GATE = {
	"Material": (SUPPLY_AND_INSTALL,),
	"Labor": LABOR_TYPES,
	"Equipment": (EQUIPMENT,),
}
_RESOURCE_FIELD_BY_CATEGORY_REVERSE = {
	"material_item": "Material",
	"labor_item": "Labor",
	"equipment_item": "Equipment",
}

# Reuses the role that already owns financial oversight on this instance,
# rather than introducing a separate override role.
COST_CONTROL_ROLE = "cost control manager"

# Editing the figures is only allowed in Draft (FR-20). Everything else -
# the pending states, Approved, Rejected - is frozen, so an approver is
# always looking at the same numbers the next approver will see.
EDITABLE_STATES = ("", "Draft")


class SubcontractorContract(Document):
	def validate(self):
		self.set_project_tenders()
		self.validate_subcontractor()
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
		self.calculate_totals()
		self.validate_retention_rows()
		self.set_ceo_approval_flag()

	def before_submit(self):
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

	def set_project_tenders(self):
		"""FR-01, extended 2026-08-30: every Tender under the project is in
		scope, not just one - a project can carry more than one Tender
		(phased tendering, multiple categories, ...), and a single contract
		can draw work items from any of them, not just one auto-picked one.
		"""
		if not self.project:
			self.set("project_tenders", [])
			return

		tenders = get_project_tenders(self.project, self.department)
		if not tenders:
			if self.department:
				frappe.throw(
					_("Project {0} has no linked Tender in category {1}, so there is no BOQ to "
					  "contract against.").format(frappe.bold(self.project), frappe.bold(self.department))
				)
			frappe.throw(
				_("Project {0} has no linked Tender, so there is no BOQ to contract against.").format(
					frappe.bold(self.project)
				)
			)
		self.set("project_tenders", [{"tender": t} for t in tenders])

	def project_tender_names(self):
		return {row.tender for row in self.project_tenders}

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
		if not self.contracted_items or self.type_subcontractor != SUPPLY_AND_INSTALL:
			return

		if lock:
			allocation.lock_work_items([row.work_item for row in self.contracted_items])

		own = {}
		for row in self.contracted_items:
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
		surviving = {row.work_item for row in self.contracted_items}
		for fieldname in ("contract_material_items", "contract_labor_items", "contract_equipment_items"):
			rows = self.get(fieldname)
			kept = [row for row in rows if row.work_item in surviving]
			if len(kept) != len(rows):
				self.set(fieldname, kept)

	def validate_material_items(self, lock=False):
		"""FR-11/16-18: the Material tab only applies to Supply and Install."""
		if self.type_subcontractor != SUPPLY_AND_INSTALL:
			self.set("contract_material_items", [])
			return
		self._validate_resource_rows("contract_material_items", MATERIAL_DOCTYPE, "material_item", lock=lock)

	def validate_labor_items(self, lock=False):
		"""FR-11/19: the Labor tab only applies to Install Only."""
		if self.type_subcontractor not in LABOR_TYPES:
			self.set("contract_labor_items", [])
			return
		self._validate_resource_rows("contract_labor_items", LABOR_DOCTYPE, "labor_item", lock=lock)

	def validate_equipment_items(self, lock=False):
		"""FR-11: the Equipment tab only applies to Equipment contracts."""
		if self.type_subcontractor != EQUIPMENT:
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
			allocation.lock_work_items([row.work_item for row in rows])

		project_tenders = self.project_tender_names()
		own = {}
		for row in rows:
			if not row.work_item:
				frappe.throw(_("Row {0}: Work Item is required.").format(row.idx))

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

		for row in self.payment_conditions:
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
			if resource_row and row.work_item and resource_row.work_item != row.work_item:
				frappe.throw(
					_("Row {0}: the selected resource belongs to a different work item than this "
					  "row's Work Item.").format(row.idx)
				)

	def validate_payment_conditions(self):
		"""FR-22/FR-23: each condition group must sum to 100% - the
		contract-wide default (rows with no Work Item) and, independently,
		each work item's override set (rows naming that Work Item).
		"""
		groups = {}
		for row in self.payment_conditions:
			groups.setdefault(row.work_item, []).append(row)

		for work_item, rows in groups.items():
			total = sum(flt(row.percent) for row in rows)
			if abs(total - 100.0) > TOLERANCE:
				label = _("work item {0}").format(work_item) if work_item else _("the contract-wide default")
				frappe.throw(
					_("Payment Conditions for {0} must sum to 100% - found {1}%.").format(
						frappe.bold(label), flt(total)
					)
				)

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
				c for c in self.existing_material_requests if c.work_item == row.work_item
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
		"""FR-06: same 5-way cascade as core Purchase Taxes and Charges."""
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
		"""Precompute the workflow's CEO-threshold condition.

		Frappe evaluates workflow conditions through safe_eval with only
		frappe.db.get_value / get_list exposed, so the threshold can't be
		read from Contracting Settings there. FR-20 freezes grand_total
		mid-chain, so this flag can't go stale once set.
		"""
		threshold = flt(frappe.db.get_single_value("Contracting Settings", "ceo_approval_value_threshold"))
		self.requires_ceo_approval = 1 if threshold and flt(self.grand_total) >= threshold else 0


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
