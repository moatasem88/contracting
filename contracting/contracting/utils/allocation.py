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

# Which pools each type claims - Labor -> Labor, Installing -> Labor +
# Subcontract, Equipment -> Equipment, Supply and Install -> all four - comes
# from RESOURCE_PROPAGATION below (see type_resources()). Since 2026-10-20
# every type, Supply and Install included, claims exactly the resource rows
# it carries (partial contracts), so the same table also says whose
# submitted rows count against a pool (_committing_types()).

LABOR_DOCTYPE = "Tender Labor Item"
SUBCONTRACT_DOCTYPE = "Tender Subcontract Item"
EQUIPMENT_DOCTYPE = "Tender Equipment Item"
MATERIAL_DOCTYPE = "Tender Material Item"

# Float comparison slack, matching the existing convention in
# subcontractor_contract.py and budget.py.
TOLERANCE = 1e-6

# type_subcontractor -> [(Tender-side resource doctype, Subcontractor-Contract
# -side child doctype, that child table's fieldname, its resource-link
# fieldname), ...] - the single source of which resource tabs apply to which
# contract type and what each one is populated from. Supply and Install
# feeds all four; Installing feeds Labor + Subcontract; Labor and Equipment
# feed their own one (2026-10-18).
_MATERIAL_TAB = (MATERIAL_DOCTYPE, "Contractor Contract Material Item", "contract_material_items", "material_item")
_LABOR_TAB = (LABOR_DOCTYPE, "Contractor Contract Labor Item", "contract_labor_items", "labor_item")
_SUBCONTRACT_TAB = (
	SUBCONTRACT_DOCTYPE, "Contractor Contract Subcontract Item", "contract_subcontract_items", "subcontract_item"
)
_EQUIPMENT_TAB = (EQUIPMENT_DOCTYPE, "Contractor Contract Equipment Item", "contract_equipment_items", "equipment_item")

RESOURCE_PROPAGATION = {
	SUPPLY_AND_INSTALL: [_MATERIAL_TAB, _LABOR_TAB, _SUBCONTRACT_TAB, _EQUIPMENT_TAB],
	SUPPLY_WORKERS: [_LABOR_TAB],
	INSTALL_ONLY: [_LABOR_TAB, _SUBCONTRACT_TAB],
	EQUIPMENT: [_EQUIPMENT_TAB],
}

# Follows Tender BRD (2026-10-13): contract resource table -> the Work Item
# Template V2 table it propagates from, and the Item rule every
# template-sourced / extra row on it must satisfy (FR-08). Same rules as the
# template form's own pickers (work_item_template_v2.js), enforced here
# server-side as well so the API / Data Import can't bypass them.
TEMPLATE_TABLE_BY_CONTRACT_TABLE = {
	"contract_material_items": "material_items",
	"contract_labor_items": "labor_items",
	"contract_subcontract_items": "subcontract_items",
	"contract_equipment_items": "equipment_items",
}
TEMPLATE_CHILD_DOCTYPE = {
	"material_items": "Template Material Item V2",
	"labor_items": "Template Labor Item V2",
	"subcontract_items": "Template Subcontract Item V2",
	"equipment_items": "Template Equipment Item V2",
}
ITEM_RULE_BY_CONTRACT_TABLE = {
	"contract_material_items": ("is_stock_item", 1, "a stock item"),
	"contract_labor_items": ("item_group", "Labor", "an item in the 'Labor' Item Group"),
	"contract_subcontract_items": (
		"item_group", "Subcontractors Items", "an item in the 'Subcontractors Items' Item Group"
	),
	"contract_equipment_items": ("item_group", "Equipment", "an item in the 'Equipment' Item Group"),
}


def type_resources(contract_type):
	"""[(Tender resource doctype, resource link fieldname), ...] for the
	pools a contract type claims row by row - empty for Supply and Install,
	which claims the work item as a whole. Read off RESOURCE_PROPAGATION so
	type dispatch never hard-codes a resource (NFR-02)."""
	if contract_type == SUPPLY_AND_INSTALL:
		return []
	return [(tab[0], tab[3]) for tab in RESOURCE_PROPAGATION.get(contract_type) or []]


TEMPLATE_DOCTYPE = "Work Item Template V2"


def line_key(row):
	"""The contract line a Contracted Items row *is*, or a resource/payment
	condition row *belongs to*: a Tender BOQ line or a Work Item Template.
	Every lookup that used to key on row.work_item keys on this instead -
	keying on work_item alone makes every template row (work_item blank)
	match every other one, since None == None.
	"""
	if row.get("work_item"):
		return ("tender", row.get("work_item"))
	if row.get("work_item_template"):
		return ("template", row.get("work_item_template"))
	return None


def validate_condition_groups(rows, only_keys=None):
	"""FR-22/FR-23: payment condition rows are grouped by the line they
	belong to (line_key - the contract-wide default has key None) and each
	group must total 100%. only_keys limits the check to those groups -
	Contractor Contract Addendum uses it so an addendum is only held to the
	groups it actually touched, not to a pre-existing group it never edited.
	Shared by SubcontractorContract and Contractor Contract Addendum.
	"""
	groups = {}
	for row in rows:
		# Follows Tender BRD (FR-16): a template-scoped condition is its own
		# group - keying on work_item alone would fold it into the
		# contract-wide default (both blank).
		groups.setdefault(line_key(row), []).append(row)

	for key, group_rows in groups.items():
		if only_keys is not None and key not in only_keys:
			continue
		total = sum(flt(row.get("percent")) for row in group_rows)
		if abs(total - 100.0) > TOLERANCE:
			if not key:
				label = _("the contract-wide default")
			elif key[0] == "tender":
				label = _("work item {0}").format(key[1])
			else:
				label = _("work item template {0}").format(key[1])
			frappe.throw(
				_("Payment Conditions for {0} must sum to 100% - found {1}%.").format(
					frappe.bold(label), flt(total)
				)
			)


def validate_resource_item(table_fieldname, item, idx):
	"""FR-08: the per-tab Item rule for template-sourced and extra rows."""
	fieldname, expected, label = ITEM_RULE_BY_CONTRACT_TABLE[table_fieldname]
	actual = frappe.db.get_value("Item", item, fieldname)
	if actual is None:
		frappe.throw(_("Row {0}: Item {1} does not exist.").format(idx, frappe.bold(item)))
	if (flt(actual) != flt(expected)) if fieldname == "is_stock_item" else (actual != expected):
		frappe.throw(
			_("Row {0}: Item {1} is not allowed on this tab - it must be {2}.").format(
				idx, frappe.bold(item), label
			)
		)


# ---------------------------------------------------------------------------
# Work item / resource row resolution
# ---------------------------------------------------------------------------

def get_work_item(work_item):
	"""The BOQ row being contracted, with the tender it belongs to."""
	row = frappe.db.get_value(
		"Tender BOQ Item",
		work_item,
		[
			"name", "parent", "idx", "item_name", "description", "uom", "original_quantity",
			"is_group", "boq_direct_cost", "material_cost", "labor_cost", "subcontract_cost", "equipment_cost",
		],
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
	"""Item-level quantity committed to submitted Supply-and-Install
	contracts. FR-13: an open contract's rows count in full (unchanged); a
	*closed* contract's rows are capped at what's actually been invoiced
	against them - the difference becomes available to other contracts on
	the same work item. Kept as two separate sums rather than a parameter
	threaded through the shared _committed() helper, so get_labor_committed/
	get_equipment_committed/get_material_committed - which have no notion of
	is_closed - are untouched."""
	open_committed = _committed(
		"Contractor Contract Item",
		"t.work_item = %(work_item)s and sc.type_subcontractor = %(type)s and sc.is_closed = 0",
		{"work_item": work_item, "type": SUPPLY_AND_INSTALL},
		exclude_contract,
	)
	return open_committed + _closed_contract_supply_install_committed(work_item, exclude_contract)


def _closed_contract_supply_install_committed(work_item, exclude_contract=None):
	"""FR-13: a closed Supply-and-Install contract's contribution is capped,
	per Contractor Contract Item row, at what's actually been invoiced
	(docstatus=1) against it - not its full contracted qty. Computed on
	read (no stored field, no hook to keep in sync), the same join shape
	progress_invoicing.get_invoiced_qty_for_work_item already uses."""
	params = {"work_item": work_item, "type": SUPPLY_AND_INSTALL, "exclude": exclude_contract or ""}
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(least(cci.qty, coalesce(invoiced.qty, 0))), 0)
			from `tabContractor Contract Item` cci
			inner join `tabSubcontractor Contract` sc on sc.name = cci.parent
			left join (
				select cii.contract_item, coalesce(sum(cii.this_period_qty), 0) as qty
				from `tabContractor Invoice Item` cii
				inner join `tabContractor Invoice` ci on ci.name = cii.parent
				where ci.docstatus = 1
				group by cii.contract_item
			) invoiced on invoiced.contract_item = cci.name
			where cci.work_item = %(work_item)s
			  and sc.type_subcontractor = %(type)s
			  and sc.docstatus = 1
			  and sc.is_closed = 1
			  and sc.name != %(exclude)s
			""",
			params,
		)[0][0]
	)


def work_item_previously_approved(work_item, exclude_contract=None):
	"""FR-07(a): has this work item ever reached an *Approved* Subcontractor
	Contract other than the one currently being saved? Rejected/cancelled
	contracts don't count - only workflow_state == 'Approved' does. Same
	`Contractor Contract Item` -> `Subcontractor Contract` join shape as
	_closed_contract_supply_install_committed above, just keyed on
	workflow_state instead of docstatus/is_closed."""
	return bool(frappe.db.sql(
		"""
		select 1
		from `tabContractor Contract Item` cci
		inner join `tabSubcontractor Contract` sc on sc.name = cci.parent
		where cci.work_item = %(work_item)s
		  and sc.workflow_state = 'Approved'
		  and sc.name != %(exclude)s
		limit 1
		""",
		{"work_item": work_item, "exclude": exclude_contract or ""},
	))


def template_previously_approved(work_item_template, exclude_contract=None):
	"""work_item_previously_approved's counterpart for template lines (Follows
	Tender BRD, OQ-4): the 250,000 first-approval CEO threshold applies per
	template the same way it applies per Tender work item."""
	return bool(frappe.db.sql(
		"""
		select 1
		from `tabContractor Contract Item` cci
		inner join `tabSubcontractor Contract` sc on sc.name = cci.parent
		where cci.work_item_template = %(template)s
		  and sc.workflow_state = 'Approved'
		  and sc.name != %(exclude)s
		limit 1
		""",
		{"template": work_item_template, "exclude": exclude_contract or ""},
	))


def _committing_types(doctype):
	"""Every contract type whose tabs carry this resource doctype."""
	return tuple(
		contract_type for contract_type, tabs in RESOURCE_PROPAGATION.items()
		if any(tab[0] == doctype for tab in tabs)
	)


_TAB_BY_DOCTYPE = {tab[0]: tab for tabs in RESOURCE_PROPAGATION.values() for tab in tabs}


def _direct_committed(doctype, resource_name, exclude_contract=None):
	"""Quantity of one Tender resource row committed by submitted contracts
	- whichever types carry that pool (2026-10-20: row-level for every type;
	a contract claims only the lines it carries).

	FR-13, row-level since 2026-10-20: a *closed* Supply-and-Install
	contract's row counts only for the share of its line actually invoiced
	(submitted Contractor Invoices), the rest is released to other contracts.
	"""
	_tender_doctype, child_doctype, _table, link_fieldname = _TAB_BY_DOCTYPE[doctype]
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(case
				when sc.is_closed = 1 and sc.type_subcontractor = %(sai)s
					then coalesce(t.qty * least(1, coalesce(inv.qty, 0) / nullif(line.qty, 0)), 0)
				else t.qty end), 0)
			from `tab{child}` t
			inner join `tabSubcontractor Contract` sc on sc.name = t.parent
			left join (
				select parent, work_item, sum(qty) as qty
				from `tabContractor Contract Item`
				where parenttype = 'Subcontractor Contract'
				group by parent, work_item
			) line on line.parent = t.parent and line.work_item = t.work_item
			left join (
				select cci.parent, cci.work_item, sum(cii.this_period_qty) as qty
				from `tabContractor Invoice Item` cii
				inner join `tabContractor Invoice` ci on ci.name = cii.parent and ci.docstatus = 1
				inner join `tabContractor Contract Item` cci on cci.name = cii.contract_item
				group by cci.parent, cci.work_item
			) inv on inv.parent = t.parent and inv.work_item = t.work_item
			where t.{link} = %(resource)s
			  and sc.type_subcontractor in %(types)s
			  and sc.docstatus = 1
			  and sc.name != %(exclude)s
			""".format(child=child_doctype, link=link_fieldname),
			{
				"resource": resource_name,
				"types": _committing_types(doctype),
				"exclude": exclude_contract or "",
				"sai": SUPPLY_AND_INSTALL,
			},
		)[0][0]
	)


def get_labor_committed(labor_item, exclude_contract=None):
	return _direct_committed(LABOR_DOCTYPE, labor_item, exclude_contract)


def get_subcontract_committed(subcontract_item, exclude_contract=None):
	return _direct_committed(SUBCONTRACT_DOCTYPE, subcontract_item, exclude_contract)


def get_equipment_committed(equipment_item, exclude_contract=None):
	return _direct_committed(EQUIPMENT_DOCTYPE, equipment_item, exclude_contract)


def get_material_committed(material_item, exclude_contract=None):
	return _direct_committed(MATERIAL_DOCTYPE, material_item, exclude_contract)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def get_resource_availability(doctype, resource_name, exclude_contract=None, work_item_row=None):
	"""FR-08 / FR-09 - how much of one Tender resource row is still free:

	    budget = r x Q,   available = budget - committed to this row

	2026-10-20: every contract type - Supply and Install included - claims
	only the rows it actually carries, so there is no longer an implicit
	"r x Supply-and-Install qty" share on top: a Supply-and-Install contract's
	carried rows are direct commitments like any other type's.
	"""
	resource_row = get_resource_row(doctype, resource_name)
	if work_item_row is None:
		work_item_row = _work_item_for_resource(resource_row)

	budget = flt(resource_row.qty_per_unit) * flt(work_item_row.original_quantity)
	return max(0.0, budget - _direct_committed(doctype, resource_name, exclude_contract))


def get_line_availability(contract_type, work_item_row, exclude_contract=None):
	"""2026-10-20 (partial contracts): every line of a work item this type
	could claim - one entry per Tender resource row in the type's pools -
	with what is still free on it, in resource units and in work-item units
	(available_units = available_qty / ratio). Zero-ratio rows constrain
	nothing and are left out (BRD TC-10)."""
	quantity = flt(work_item_row.original_quantity)
	lines = []
	for doctype, _child, table_fieldname, link_fieldname in RESOURCE_PROPAGATION.get(contract_type, []):
		for row in get_resource_rows(doctype, work_item_row):
			ratio = flt(row.qty_per_unit)
			if ratio <= TOLERANCE:
				continue
			budget = ratio * quantity
			available = max(0.0, budget - _direct_committed(doctype, row.name, exclude_contract))
			lines.append(frappe._dict({
				"doctype": doctype,
				"table": table_fieldname,
				"link_fieldname": link_fieldname,
				"name": row.name,
				"item": row.item,
				"ratio": ratio,
				"budget": budget,
				"available_qty": available,
				"available_units": available / ratio,
			}))
	return lines


def get_supply_install_availability(work_item, exclude_contract=None, work_item_row=None):
	"""Work-item units a Supply-and-Install contract can still take - see
	_item_level_availability (the same rule for every type since 2026-10-20)."""
	if work_item_row is None:
		work_item_row = get_work_item(work_item)
	return _item_level_availability(work_item_row, SUPPLY_AND_INSTALL, exclude_contract)


def _work_item_for_resource(resource_row):
	"""The BOQ row a resource row belongs to, via boq_row_id or idx."""
	if resource_row.boq_row_id:
		row = frappe.db.get_value(
			"Tender BOQ Item",
			resource_row.boq_row_id,
			["name", "parent", "idx", "item_name", "description", "uom", "original_quantity", "is_group"],
			as_dict=True,
		)
		if row:
			return row

	row = frappe.db.get_value(
		"Tender BOQ Item",
		{"parent": resource_row.parent, "idx": resource_row.work_item},
		["name", "parent", "idx", "item_name", "description", "uom", "original_quantity", "is_group"],
		as_dict=True,
	)
	if not row:
		frappe.throw(
			_("Could not resolve the work item for {0} {1} - its BOQ row is missing.").format(
				_(resource_row.get("doctype") or "resource row"), resource_row.name
			)
		)
	return row


def _item_level_availability(work_item_row, contract_type, exclude_contract=None):
	"""The most work-item units any single line this type could claim can
	still support - what the "Select Work Item" dialog lists a work item
	with, and an Addendum item that names no line is checked against.

	2026-10-20 (partial contracts): a work item stays offered while *any* of
	its lines has budget left - one contract may take subcontract line A and
	another line B - so this is the max over lines, not the old
	min-over-all-lines bottleneck. A work item with no line in the type's
	pools has nothing for that type to claim: 0.

	The real guard is still per row: SubcontractorContract._validate_resource_rows
	at save time, and apply_on_approval's own check on the Addendum path.
	"""
	lines = get_line_availability(contract_type, work_item_row, exclude_contract)
	return max((line.available_units for line in lines), default=0.0)


def get_row_availability(contract_type, work_item, labor_item=None, equipment_item=None,
						 exclude_contract=None, subcontract_item=None):
	"""Availability for one contract row, dispatched on the header's type.

	Single entry point so the controller and any client-side display go
	through identical math. A named resource link in one of the type's own
	pools reads that row; otherwise item level (_item_level_availability).
	"""
	work_item_row = get_work_item(work_item)

	links = {"labor_item": labor_item, "subcontract_item": subcontract_item, "equipment_item": equipment_item}
	for resource_doctype, _child, _table, link_fieldname in RESOURCE_PROPAGATION.get(contract_type, []):
		if links.get(link_fieldname):
			return get_resource_availability(
				resource_doctype, links[link_fieldname], exclude_contract=exclude_contract,
				work_item_row=work_item_row,
			)
	return _item_level_availability(work_item_row, contract_type, exclude_contract)


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

def _propagated_resource_rows(work_item_row, contracted_qty, contract_type, subcontractor_contract=None,
							  lines=None):
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

	lines (2026-10-20, partial contracts): Tender resource-row names to take;
	None takes every line of the work item.
	"""
	lines = set(lines) if lines is not None else None
	result = {}
	for resource_doctype, _child_doctype, table_fieldname, link_fieldname in RESOURCE_PROPAGATION.get(contract_type, []):
		rows = []
		for resource_row in get_resource_rows(resource_doctype, work_item_row):
			ratio = flt(resource_row.qty_per_unit)
			if ratio <= TOLERANCE:
				continue
			if lines is not None and resource_row.name not in lines:
				continue

			rows.append({
				"work_item": work_item_row.name,
				link_fieldname: resource_row.name,
				# item too, not just its label: the Addendum inserts these rows
				# directly (no validate() to fill it), and the grid shows it.
				"item": resource_row.item,
				"resource_item": resource_row.item,
				"uom": work_item_row.uom,
				"qty": ratio * flt(contracted_qty),
				"available_qty": get_resource_availability(
					resource_doctype, resource_row.name,
					exclude_contract=subcontractor_contract, work_item_row=work_item_row,
				),
				"auto_propagated": 1,
				"source": "Tender",
			})
		result[table_fieldname] = rows
	return result


def _template_resource_rows(work_item_template, qty, contract_type, include_reference=False):
	"""FR-07: the template-line counterpart to _propagated_resource_rows -
	one dict per template resource row, per resource table the contract type
	fills (same RESOURCE_PROPAGATION gating). No Tender link and no
	availability: a template line has no Tender quantity to draw down (FR-09).

	No rate: the template's own rates are reference cost, and nobody in the
	contracts department may see them (2026-10-13 decision, tightening the
	BRD's FR-07) - the engineer enters what the subcontractor actually
	charges. include_reference adds the template rate as tender_rate, for
	server-side callers only (Addendum approval); never pass it through to
	a whitelisted endpoint.
	"""
	template = frappe.get_doc(TEMPLATE_DOCTYPE, work_item_template)
	result = {}
	for _resource_doctype, _child_doctype, table_fieldname, _link in RESOURCE_PROPAGATION.get(contract_type, []):
		rows = []
		for resource_row in template.get(TEMPLATE_TABLE_BY_CONTRACT_TABLE[table_fieldname]):
			ratio = flt(resource_row.qty_per_unit)
			if ratio <= TOLERANCE:
				continue
			item_name, stock_uom = frappe.db.get_value("Item", resource_row.item, ["item_name", "stock_uom"]) or (None, None)
			row = {
				"work_item_template": template.name,
				"item": resource_row.item,
				"resource_item": item_name or resource_row.item,
				"uom": stock_uom,
				"qty": ratio * flt(qty),
				"auto_propagated": 1,
				"source": "Template",
			}
			if include_reference:
				row["tender_rate"] = flt(resource_row.rate)
			rows.append(row)
		result[table_fieldname] = rows
	return result


@frappe.whitelist()
def get_template_resource_rows(work_item_template, qty, contract_type):
	"""Client entry point for template-line propagation (dialog + qty rescale).
	'select' is enough - the contracts roles only hold select on the
	template (they must not open it and see its rates), and nothing
	returned here carries a rate."""
	frappe.has_permission(TEMPLATE_DOCTYPE, "select", throw=True)
	return _template_resource_rows(work_item_template, flt(qty), contract_type)


@frappe.whitelist()
def get_propagated_resource_rows(work_item, qty, contract_type, subcontractor_contract=None, lines=None):
	"""Client-facing entry point - the dialog and the manual Add Row path
	both call this. Addendum approval calls _propagated_resource_rows
	directly (server-side, work_item_row already resolved there). lines is
	the dialog's ticked line names (JSON list), or None for all of them.
	"""
	if isinstance(lines, str):
		lines = frappe.parse_json(lines)
	return _propagated_resource_rows(
		get_work_item(work_item), flt(qty), contract_type, subcontractor_contract, lines=lines
	)


@frappe.whitelist()
def get_work_item_lines(work_item, contract_type, subcontractor_contract=None):
	"""The line picker's data (dialog + Addendum): every line this type could
	claim on the work item, with what is still free on it."""
	return get_line_availability(contract_type, get_work_item(work_item), exclude_contract=subcontractor_contract)


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

	The tender's own material estimate (ratio x item qty) for each material
	row, *less* what submitted Supply-and-Install contracts carry of that
	row: that portion is the subcontractor's to supply, so the company
	shouldn't also be procuring it. Row-level since 2026-10-20 - a partial
	Supply-and-Install contract that doesn't carry a material line leaves it
	to the company.

	    budget(item) = sum over its rows of (ratio x Q - committed to the row)
	"""
	work_item_row = resolve_project_work_item(project_work_item)
	if not work_item_row:
		return {}

	quantity = flt(work_item_row.original_quantity)
	budget = {}
	for row in get_resource_rows(MATERIAL_DOCTYPE, work_item_row):
		remaining = max(0.0, flt(row.qty_per_unit) * quantity - get_material_committed(row.name, exclude_contract))
		budget[row.item] = budget.get(row.item, 0.0) + remaining

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
		select name, item_name, uom, item_code
		from `tabTender BOQ Item`
		where parent in %(tenders)s
		  and ifnull(is_group, 0) = 0
		  and (name like %(txt)s or ifnull(item_name, '') like %(txt)s or ifnull(item_code, '') like %(txt)s)
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
def subcontract_item_query(doctype, txt, searchfield, start, page_len, filters):
	return _resource_query(SUBCONTRACT_DOCTYPE, txt, start, page_len, filters)


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
					  subcontractor_contract=None, subcontract_item=None):
	"""Display-side fetch for the Available Qty column."""
	return get_row_availability(
		contract_type, work_item, labor_item=labor_item, equipment_item=equipment_item,
		exclude_contract=subcontractor_contract, subcontract_item=subcontract_item,
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
