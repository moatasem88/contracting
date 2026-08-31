"""Cross-type quantity reconciliation for Subcontractor Contracts.

A work item's cost decomposes into material, labor and equipment. Any of
those can be contracted to a different subcontractor under a different
contract type, so availability cannot be tracked per type in isolation -
committing a work item as Supply and Install implicitly consumes its
labor and equipment budgets, and committing labor directly caps how much
of the item can still be given out as Supply and Install.

Everything here works off the estimating data already on Tender:

    Tender BOQ Item.original_quantity     the work item's quantity  (Q)
    Tender Labor/Equipment Item
        .qty_per_unit                     resource needed per unit  (r)
        .boq_row_id                       stable pointer to its BOQ row

so a labor row's total budget is r x Q.

This is the single source of the math (the BRD's NFR-01): the contract
controller validates through it and the client fetches display values
through it, so the two can't drift.
"""

import frappe
from frappe import _
from frappe.utils import flt

# Stored Select values of Subcontractor Contract.type_subcontractor,
# alongside the BRD's names for them.
SUPPLY_AND_INSTALL = "Supplying and installing"   # BRD: Supply and Install
INSTALL_ONLY = "Installing"                       # BRD: Install Only
SUPPLY_WORKERS = "Labor"                          # BRD: Supply Workers
EQUIPMENT = "Equipment"                           # BRD: Equipment

# Only Install Only consumes a labor row's budget now; Supply Workers was
# removed as a contract type (FR-01-03).
LABOR_TYPES = (INSTALL_ONLY,)

LABOR_DOCTYPE = "Tender Labor Item"
EQUIPMENT_DOCTYPE = "Tender Equipment Item"
MATERIAL_DOCTYPE = "Tender Material Item"

# Float comparison slack, matching the existing convention in
# subcontractor_contract.py and budget.py.
TOLERANCE = 1e-6

# type_subcontractor -> [(Tender-side resource doctype, Subcontractor-Contract
# -side child doctype, that child table's fieldname, its resource-link
# fieldname), ...] - the single source of which resource tabs apply to which
# contract type and what each one is populated from. Supply and Install
# feeds all three; Install Only / Equipment feed only their own.
RESOURCE_PROPAGATION = {
	SUPPLY_AND_INSTALL: [
		(MATERIAL_DOCTYPE, "Contractor Contract Material Item", "contract_material_items", "material_item"),
		(LABOR_DOCTYPE, "Contractor Contract Labor Item", "contract_labor_items", "labor_item"),
		(EQUIPMENT_DOCTYPE, "Contractor Contract Equipment Item", "contract_equipment_items", "equipment_item"),
	],
	INSTALL_ONLY: [
		(LABOR_DOCTYPE, "Contractor Contract Labor Item", "contract_labor_items", "labor_item"),
	],
	EQUIPMENT: [
		(EQUIPMENT_DOCTYPE, "Contractor Contract Equipment Item", "contract_equipment_items", "equipment_item"),
	],
}


# ---------------------------------------------------------------------------
# Work item / resource row resolution
# ---------------------------------------------------------------------------

def get_work_item(work_item):
	"""The BOQ row being contracted, with the tender it belongs to."""
	row = frappe.db.get_value(
		"Tender BOQ Item",
		work_item,
		["name", "parent", "idx", "item_name", "uom", "original_quantity", "is_group"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Work item {0} does not exist.").format(work_item))
	return row


def get_resource_rows(doctype, work_item_row):
	"""Labor/equipment/material rows belonging to one BOQ row.

	Resource rows point at their BOQ row two ways: boq_row_id (the row's
	stable name) and work_item (its idx). boq_row_id is preferred because
	idx shifts when BOQ rows are reordered; the idx match is only a
	fallback for rows predating the backfill.
	"""
	return frappe.db.sql(
		"""
		select name, item, qty_per_unit, boq_row_id, work_item
		from `tab{0}`
		where parent = %(tender)s
		  and (
			boq_row_id = %(boq_row)s
			or (ifnull(boq_row_id, '') = '' and work_item = %(idx)s)
		  )
		""".format(doctype),
		{"tender": work_item_row.parent, "boq_row": work_item_row.name, "idx": work_item_row.idx},
		as_dict=True,
	)


def get_resource_row(doctype, name):
	row = frappe.db.get_value(
		doctype, name, ["name", "parent", "item", "qty_per_unit", "boq_row_id", "work_item"], as_dict=True
	)
	if not row:
		frappe.throw(_("{0} {1} does not exist.").format(_(doctype), name))
	return row


def resource_belongs_to_work_item(resource_row, work_item_row):
	"""TC-11: a labor/equipment row must belong to the selected work item.

	Checked here rather than relying on the UI's filtered dropdown, since
	API calls and Data Import bypass that entirely.
	"""
	if resource_row.parent != work_item_row.parent:
		return False
	if resource_row.boq_row_id:
		return resource_row.boq_row_id == work_item_row.name
	return resource_row.work_item == work_item_row.idx


# ---------------------------------------------------------------------------
# Committed quantity (FR-10: only docstatus = 1 counts)
# ---------------------------------------------------------------------------

def _committed(doctype, where, params, exclude_contract=None):
	params = dict(params)
	params["exclude"] = exclude_contract or ""
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(t.qty), 0)
			from `tab{0}` t
			inner join `tabSubcontractor Contract` sc on sc.name = t.parent
			where {1}
			  and sc.docstatus = 1
			  and sc.name != %(exclude)s
			""".format(doctype, where),
			params,
		)[0][0]
	)


def get_supply_install_committed(work_item, exclude_contract=None):
	"""Item-level quantity committed to submitted Supply-and-Install contracts."""
	return _committed(
		"Contractor Contract Item",
		"t.work_item = %(work_item)s and sc.type_subcontractor = %(type)s",
		{"work_item": work_item, "type": SUPPLY_AND_INSTALL},
		exclude_contract,
	)


def get_labor_committed(labor_item, exclude_contract=None):
	"""Quantity committed directly to one labor row by Install Only contracts."""
	return _committed(
		"Contractor Contract Labor Item",
		"t.labor_item = %(labor_item)s and sc.type_subcontractor in %(types)s",
		{"labor_item": labor_item, "types": LABOR_TYPES},
		exclude_contract,
	)


def get_equipment_committed(equipment_item, exclude_contract=None):
	"""Quantity committed directly to one equipment row by Equipment contracts."""
	return _committed(
		"Contractor Contract Equipment Item",
		"t.equipment_item = %(equipment_item)s and sc.type_subcontractor = %(type)s",
		{"equipment_item": equipment_item, "type": EQUIPMENT},
		exclude_contract,
	)


def get_material_committed(material_item, exclude_contract=None):
	"""Quantity committed directly to one material row by Supply-and-Install contracts."""
	return _committed(
		"Contractor Contract Material Item",
		"t.material_item = %(material_item)s and sc.type_subcontractor = %(type)s",
		{"material_item": material_item, "type": SUPPLY_AND_INSTALL},
		exclude_contract,
	)


def _direct_committed(doctype, resource_name, exclude_contract=None):
	if doctype == LABOR_DOCTYPE:
		return get_labor_committed(resource_name, exclude_contract)
	if doctype == EQUIPMENT_DOCTYPE:
		return get_equipment_committed(resource_name, exclude_contract)
	return get_material_committed(resource_name, exclude_contract)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def get_resource_availability(doctype, resource_name, exclude_contract=None, work_item_row=None):
	"""FR-08 / FR-09 - how much of one labor, equipment or material row is
	still free.

	    budget          = r x Q
	    already used    = committed directly to this row
	                    + r x (item qty committed as Supply and Install)

	The second term is the cross-type part: a Supply-and-Install contract
	never names a labor/equipment row, but it consumes one all the same.

	Material rows (FR-16-18) don't get that second term: they are only ever
	committed *directly* by Supply-and-Install contracts themselves (never
	by a different, disjoint contract type the way Labor/Equipment's direct
	commitments are), so "committed directly" already fully accounts for
	every Supply-and-Install contract's usage of the row. Adding the second
	term back in would count that same usage twice.
	"""
	resource_row = get_resource_row(doctype, resource_name)
	if work_item_row is None:
		work_item_row = _work_item_for_resource(resource_row)

	ratio = flt(resource_row.qty_per_unit)
	quantity = flt(work_item_row.original_quantity)

	budget = ratio * quantity
	direct = _direct_committed(doctype, resource_name, exclude_contract)

	if doctype == MATERIAL_DOCTYPE:
		return max(0.0, budget - direct)

	via_supply_install = ratio * get_supply_install_committed(work_item_row.name, exclude_contract)
	return max(0.0, budget - direct - via_supply_install)


def get_supply_install_availability(work_item, exclude_contract=None, work_item_row=None):
	"""FR-07 - how much of a work item can still be given out as Supply and Install.

	Capped by the item's own remaining quantity, and by whichever
	labor/equipment sub-row has the least budget left (the bottleneck):
	a row with ratio r and D units of budget left can only support D / r
	further units of the item.

	Zero-ratio rows are skipped - they constrain nothing, and dividing by
	their ratio would raise (BRD TC-10).
	"""
	if work_item_row is None:
		work_item_row = get_work_item(work_item)

	quantity = flt(work_item_row.original_quantity)
	committed = get_supply_install_committed(work_item_row.name, exclude_contract)

	# The item's own pool.
	available = quantity - committed

	for doctype in (LABOR_DOCTYPE, EQUIPMENT_DOCTYPE):
		for row in get_resource_rows(doctype, work_item_row):
			ratio = flt(row.qty_per_unit)
			if ratio <= TOLERANCE:
				continue

			direct = _direct_committed(doctype, row.name, exclude_contract)
			# Budget left on this row once its direct commitments are taken
			# out, expressed back in units of the work item. Labor/Equipment
			# rows are only ever committed *directly* by Install Only /
			# Equipment contracts - a disjoint population from the
			# Supply-and-Install contracts `committed` already counts - so
			# subtracting both here is additive, not double-counting.
			supportable = quantity - committed - (direct / ratio)
			available = min(available, supportable)

	# Material rows (FR-16-18) are different: they are only ever populated
	# by Supply-and-Install contracts themselves - the *same* population
	# `committed` is drawn from, not a disjoint one - so `direct` here
	# already fully reflects that population's usage of this row. Combining
	# it with `committed` too would count the same contract's claim twice;
	# the row's own remaining budget in item-terms stands on its own.
	for row in get_resource_rows(MATERIAL_DOCTYPE, work_item_row):
		ratio = flt(row.qty_per_unit)
		if ratio <= TOLERANCE:
			continue

		direct = get_material_committed(row.name, exclude_contract)
		supportable = quantity - (direct / ratio)
		available = min(available, supportable)

	return max(0.0, available)


def _work_item_for_resource(resource_row):
	"""The BOQ row a resource row belongs to, via boq_row_id or idx."""
	if resource_row.boq_row_id:
		row = frappe.db.get_value(
			"Tender BOQ Item",
			resource_row.boq_row_id,
			["name", "parent", "idx", "item_name", "uom", "original_quantity", "is_group"],
			as_dict=True,
		)
		if row:
			return row

	row = frappe.db.get_value(
		"Tender BOQ Item",
		{"parent": resource_row.parent, "idx": resource_row.work_item},
		["name", "parent", "idx", "item_name", "uom", "original_quantity", "is_group"],
		as_dict=True,
	)
	if not row:
		frappe.throw(
			_("Could not resolve the work item for {0} {1} - its BOQ row is missing.").format(
				_(resource_row.get("doctype") or "resource row"), resource_row.name
			)
		)
	return row


def _item_level_availability(work_item_row, exclude_contract=None):
	"""The work item's own remaining quantity, with no per-resource
	bottleneck applied - used for an Installing/Equipment claim that names
	only the work item, not a specific resource row yet (2026-08-30: the
	unified "Select Work Item" dialog, and an Addendum item once FR-16
	stopped requiring one).

	Only Supply and Install ever draws down the item's own quantity pool
	directly (Installing/Equipment commitments live on their own resource
	rows instead - get_labor_committed/get_equipment_committed - so this
	only ever needs to net out Supply and Install's own share, the same
	starting point get_supply_install_availability itself uses before it
	applies its own additional resource-bottleneck cap.

	Deliberately simpler than that cap: this can show a work item as
	"available" even when one of its specific labor/equipment rows is
	actually tighter - the real, precise guard is still
	SubcontractorContract._validate_resource_rows at save time (for the
	base contract's own dialog/manual-add paths) and the matching guard in
	apply_on_approval (for the Addendum path, which never runs the base
	contract's own validate()).
	"""
	quantity = flt(work_item_row.original_quantity)
	committed = get_supply_install_committed(work_item_row.name, exclude_contract)
	return max(0.0, quantity - committed)


def get_row_availability(contract_type, work_item, labor_item=None, equipment_item=None,
						 exclude_contract=None):
	"""Availability for one contract row, dispatched on the header's type.

	Single entry point so the controller and any client-side display go
	through identical math.
	"""
	work_item_row = get_work_item(work_item)

	if contract_type == SUPPLY_AND_INSTALL:
		return get_supply_install_availability(
			work_item, exclude_contract=exclude_contract, work_item_row=work_item_row
		)

	if contract_type in LABOR_TYPES:
		if not labor_item:
			return _item_level_availability(work_item_row, exclude_contract)
		return get_resource_availability(
			LABOR_DOCTYPE, labor_item, exclude_contract=exclude_contract, work_item_row=work_item_row
		)

	if contract_type == EQUIPMENT:
		if not equipment_item:
			return _item_level_availability(work_item_row, exclude_contract)
		return get_resource_availability(
			EQUIPMENT_DOCTYPE, equipment_item, exclude_contract=exclude_contract,
			work_item_row=work_item_row
		)


def lock_work_items(work_items):
	"""Serialise concurrent submissions against the same work item (NFR-02).

	Locks the Tender BOQ Item rows for the duration of the transaction.
	Locking the BOQ row rather than the aggregate matters: the rows being
	summed are the ones about to be inserted, so a FOR UPDATE over them
	would lock nothing. The BOQ row always exists, so two submissions
	touching the same work item serialise on it and the second re-reads
	the first's committed quantity.
	"""
	work_items = [w for w in set(work_items or []) if w]
	if not work_items:
		return

	frappe.db.sql(
		"""
		select name from `tabTender BOQ Item`
		where name in %(names)s
		order by name
		for update
		""",
		{"names": tuple(work_items)},
	)


# ---------------------------------------------------------------------------
# Propagation (2026-08-30 BRD: auto-populate resource tabs on work item add)
# ---------------------------------------------------------------------------

def _propagated_resource_rows(work_item_row, contracted_qty, contract_type, subcontractor_contract=None):
	"""One dict per linked Tender resource row, per applicable resource table.

	Builds rows only - it does not check availability (FR-11: a propagated
	row is created at its full computed qty regardless of what's left; the
	existing over-allocation frappe.throw in
	SubcontractorContract._validate_resource_rows is what catches an
	over-budget row, at save time, same as it already does for a manually
	entered one). available_qty is still populated per row (FR-10) purely
	for display, via the same get_resource_availability manual entry uses.

	Returns {table_fieldname: [row_dict, ...]} for every table
	RESOURCE_PROPAGATION lists for contract_type - always all of them, even
	when a given table's list comes back empty (FR-CC-06: a work item with
	no linked resources of a type just yields an empty list for that table,
	not an error).
	"""
	result = {}
	for resource_doctype, _child_doctype, table_fieldname, link_fieldname in RESOURCE_PROPAGATION.get(contract_type, []):
		rows = []
		for resource_row in get_resource_rows(resource_doctype, work_item_row):
			ratio = flt(resource_row.qty_per_unit)
			if ratio <= TOLERANCE:
				continue

			rows.append({
				"work_item": work_item_row.name,
				link_fieldname: resource_row.name,
				"resource_item": resource_row.item,
				"uom": work_item_row.uom,
				"qty": ratio * flt(contracted_qty),
				"available_qty": get_resource_availability(
					resource_doctype, resource_row.name,
					exclude_contract=subcontractor_contract, work_item_row=work_item_row,
				),
			})
		result[table_fieldname] = rows
	return result


@frappe.whitelist()
def get_propagated_resource_rows(work_item, qty, contract_type, subcontractor_contract=None):
	"""Client-facing entry point - the dialog and the manual Add Row path
	both call this. Addendum approval calls _propagated_resource_rows
	directly (server-side, work_item_row already resolved there).
	"""
	return _propagated_resource_rows(get_work_item(work_item), flt(qty), contract_type, subcontractor_contract)


# ---------------------------------------------------------------------------
# Material budgets (FR-16)
# ---------------------------------------------------------------------------

def resolve_project_work_item(project_work_item):
	"""The Tender BOQ row behind a Project BOQ Item.

	Project BOQ Item rows are created by Tender.carry_boq_to_project() and
	keep a source_tender_boq_item link back to the BOQ line they came
	from - which is how a project-side work item reaches its estimating
	data.
	"""
	source = frappe.db.get_value("Project BOQ Item", project_work_item, "source_tender_boq_item")
	if not source:
		return None

	return frappe.db.get_value(
		"Tender BOQ Item",
		source,
		["name", "parent", "idx", "item_name", "uom", "original_quantity", "is_group"],
		as_dict=True,
	)


def get_material_budget(project_work_item, exclude_contract=None):
	"""FR-16 - {item_code: budgeted qty} for one project work item.

	The budget is the tender's own material estimate (ratio x item qty),
	*less* the share already covered by submitted Supply-and-Install
	contracts: that portion is the subcontractor's to supply, so the
	company shouldn't also be procuring it.

	    budget(item) = ratio x (item qty - Supply-and-Install committed qty)
	"""
	work_item_row = resolve_project_work_item(project_work_item)
	if not work_item_row:
		return {}

	quantity = flt(work_item_row.original_quantity)
	committed = get_supply_install_committed(work_item_row.name, exclude_contract)
	remaining = max(0.0, quantity - committed)

	budget = {}
	for row in get_resource_rows(MATERIAL_DOCTYPE, work_item_row):
		budget[row.item] = budget.get(row.item, 0.0) + flt(row.qty_per_unit) * remaining

	return budget


def get_budgeted_items(project_work_items):
	"""{item_code: {project_work_item: budget}} across several work items.

	Used both to restrict what a Material Request may ask for and to work
	out which work item an item line belongs to.
	"""
	by_item = {}
	for project_work_item in project_work_items:
		for item_code, budget in get_material_budget(project_work_item).items():
			by_item.setdefault(item_code, {})[project_work_item] = budget
	return by_item


# ---------------------------------------------------------------------------
# Link field queries
# ---------------------------------------------------------------------------

@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def work_item_query(doctype, txt, searchfield, start, page_len, filters):
	"""FR-02 - only BOQ rows of the contract's Project Tenders, and never
	group rows. `tenders` is a list (FR-01, extended 2026-08-30 - a
	project, and so a contract, can be in scope of more than one Tender).

	Column order matters: Tender BOQ Item sets show_title_field_in_link, and
	frappe.desk.search.build_for_autosuggest renders *column 1* of each row as
	the dropdown label. item_name has to stay second or the link falls back to
	showing the hash name.
	"""
	tenders = (filters or {}).get("tenders")
	tenders = [t for t in (tenders or []) if t]
	if not tenders:
		return []

	return frappe.db.sql(
		"""
		select name, item_name, uom
		from `tabTender BOQ Item`
		where parent in %(tenders)s
		  and ifnull(is_group, 0) = 0
		  and (name like %(txt)s or ifnull(item_name, '') like %(txt)s)
		order by parent, idx
		limit %(start)s, %(page_len)s
		""",
		{"tenders": tuple(tenders), "txt": "%%%s%%" % txt, "start": start, "page_len": page_len},
	)


def _resource_query(child_doctype, txt, start, page_len, filters):
	"""FR-04 / FR-05 - resource rows within the selected work item.

	Not expressible as a plain link filter: resource rows live in
	header-level tables on Tender and point at their BOQ row by
	boq_row_id / idx, so this resolves the work item first.

	As with work_item_query, `item` has to stay column 1 - that is what
	build_for_autosuggest turns into the dropdown label.
	"""
	filters = filters or {}
	work_item = filters.get("work_item")
	if not work_item:
		return []

	work_item_row = get_work_item(work_item)

	return frappe.db.sql(
		"""
		select name, item, qty_per_unit
		from `tab{0}`
		where parent = %(tender)s
		  and (
			boq_row_id = %(boq_row)s
			or (ifnull(boq_row_id, '') = '' and work_item = %(idx)s)
		  )
		  and (name like %(txt)s or ifnull(item, '') like %(txt)s)
		order by idx
		limit %(start)s, %(page_len)s
		""".format(child_doctype),
		{
			"tender": work_item_row.parent,
			"boq_row": work_item_row.name,
			"idx": work_item_row.idx,
			"txt": "%%%s%%" % txt,
			"start": start,
			"page_len": page_len,
		},
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def labor_item_query(doctype, txt, searchfield, start, page_len, filters):
	return _resource_query(LABOR_DOCTYPE, txt, start, page_len, filters)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def equipment_item_query(doctype, txt, searchfield, start, page_len, filters):
	return _resource_query(EQUIPMENT_DOCTYPE, txt, start, page_len, filters)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def material_item_query(doctype, txt, searchfield, start, page_len, filters):
	return _resource_query(MATERIAL_DOCTYPE, txt, start, page_len, filters)


@frappe.whitelist()
def get_available_qty(contract_type, work_item, labor_item=None, equipment_item=None,
					  subcontractor_contract=None):
	"""Display-side fetch for the Available Qty column."""
	return get_row_availability(
		contract_type, work_item, labor_item=labor_item, equipment_item=equipment_item,
		exclude_contract=subcontractor_contract,
	)


@frappe.whitelist()
def get_material_available_qty(material_item, subcontractor_contract=None):
	"""Display-side fetch for the Material tab's Available Qty column.

	Material isn't a contract_type get_row_availability dispatches on - it's
	a resource within a Supply-and-Install work item - so this goes straight
	to get_resource_availability, the same way the Labor/Equipment tabs'
	per-row availability ultimately does.
	"""
	return get_resource_availability(MATERIAL_DOCTYPE, material_item, exclude_contract=subcontractor_contract)
