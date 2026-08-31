import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from contracting.contracting.utils import allocation
from contracting.contracting.utils.allocation import EQUIPMENT, LABOR_TYPES, TOLERANCE


class ContractorContractAddendum(Document):
	def validate(self):
		self.set_project_tenders()
		self.validate_items()
		self.calculate_grand_total_delta()
		self.set_ceo_approval_flag()

	def before_submit(self):
		self.validate_availability(lock=True)

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

	def validate_items(self):
		"""2026-08-30: an addendum item only needs work_item + qty_delta (+
		unit_price for Add Work Item) - resource selection is no longer
		hand-picked here at all. labor_item/equipment_item stay on the
		doctype but are unused going forward (any Draft addendum with them
		already filled just carries dead values, not an error); the base
		contract's resource rows come entirely from propagation on approval
		(apply_on_approval below), reading the work item's own Tender-linked
		resources - the same source manual entry always drew from anyway.
		"""
		if not self.addendum_items:
			frappe.throw(_("Add at least one Addendum Item."))

		project_tenders = {row.tender for row in self.project_tenders}
		for row in self.addendum_items:
			work_item_row = allocation.get_work_item(row.work_item)
			if work_item_row.parent not in project_tenders:
				frappe.throw(
					_("Row {0}: work item {1} does not belong to any of the base contract's Project "
					  "Tenders ({2}).").format(
						row.idx, frappe.bold(row.work_item), frappe.bold(", ".join(project_tenders) or "")
					)
				)

			row.amount = flt(row.qty_delta) * flt(row.unit_price)

	def calculate_grand_total_delta(self):
		self.grand_total_delta = sum(flt(row.amount) for row in self.addendum_items)

	def set_ceo_approval_flag(self):
		"""Mirrors Subcontractor Contract's own set_ceo_approval_flag,
		against this addendum's own Grand Total Delta rather than the base
		contract's Grand Total - workflow conditions run through safe_eval,
		which cannot read a Single doctype, so this has to be precomputed.
		"""
		threshold = flt(frappe.db.get_single_value("Contracting Settings", "ceo_approval_value_threshold"))
		self.requires_ceo_approval = 1 if threshold and flt(self.grand_total_delta) >= threshold else 0

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
		if lock:
			allocation.lock_work_items([row.work_item for row in self.addendum_items])

		own = {}
		for row in self.addendum_items:
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
		if self.type_subcontractor in LABOR_TYPES:
			return ("labor", row.work_item, row.labor_item, None)
		if self.type_subcontractor == EQUIPMENT:
			return ("equipment", row.work_item, None, row.equipment_item)
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

	next_idx = len(contract.contracted_items)
	next_resource_idx = {fieldname: len(contract.get(fieldname)) for _dt, _cdt, fieldname, _lf in resource_tables}

	for row in doc.addendum_items:
		next_idx += 1
		frappe.get_doc({
			"doctype": "Contractor Contract Item",
			"parent": contract.name,
			"parenttype": "Subcontractor Contract",
			"parentfield": "contracted_items",
			"idx": next_idx,
			"work_item": row.work_item,
			"qty": row.qty_delta,
			"unit_price": row.unit_price,
			"amount": row.amount,
		}).db_insert()

		work_item_row = allocation.get_work_item(row.work_item)
		# No exclude_contract: unlike the base contract's own dialog/manual-add
		# paths (which exclude themselves - their new rows aren't committed
		# yet, so comparing against a pool that already includes them would be
		# circular), the base contract here is a *different*, already-submitted
		# document whose prior commitment is real and must count - the same
		# principle validate_availability above already states explicitly.
		propagated = allocation._propagated_resource_rows(work_item_row, row.qty_delta, doc.type_subcontractor)
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
				}).db_insert()

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
		_("Addendum {0} approved: added {1} row(s), +{2} to Grand Total.").format(
			doc.name, len(doc.addendum_items), flt(doc.grand_total_delta)
		),
	)
