import frappe
from frappe import _

from contracting.contracting.api.external._common import (
	attach_unfielded,
	check_modified,
	list_view,
	load,
	parse,
	positive,
	prepare_save,
	rows_by_name,
	view,
)
from contracting.contracting.doctype.tender import tender as tender_controller

DOCTYPE = "Tender"
TEMPLATE_DOCTYPE = "Work Item Template V2"
MARK_COMPLETE_ROLE = "Tender Manager"  # tender.mark_tender_complete's own frappe.only_for

LIST_FIELDS = (
	"name", "project_name", "project_tender", "status", "tender_version", "total_direct_cost",
	"tender_final_cost", "sell_amount", "modified",
)
# FR-15: header fields a new Tender may be created with. project_name is
# fetched from the Project Tender; status, totals and the version chain are
# the controller's.
CREATE_FIELDS = ("tender_date", "submission_deadline", "tender_category", "consolidate_rates_by_item",
				 "safety_factor_percent")


def list_tenders(project_tender=None, filters=None, search=None, limit=None, start=None):
	filters = parse(filters, {}) or {}
	if project_tender:
		filters["project_tender"] = project_tender
	return list_view(DOCTYPE, LIST_FIELDS, filters, search, ("name", "project_name"), limit, start)


def get_tender_view(name):
	return _view(load(DOCTYPE, name))


def list_project_tender_options(search=None, limit=None, start=None):
	"""Picker for create_tender. The Tender creators (Tender Engineer
	Civil/Electrical/Mechanical) hold no permission on Project Tender at all,
	yet every Tender must link to one - so, like get_work_item_templates for
	the contracts roles, this offers names only, to whoever may create a
	Tender. No costs, totals or client."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	or_filters = None
	if search:
		or_filters = [["name", "like", f"%{search}%"], ["project_name", "like", f"%{search}%"]]
	limit = min(max(int(limit or 20), 1), 100)
	return frappe.get_all(
		"Project Tender", fields=["name", "project_name", "status"], or_filters=or_filters,
		limit_start=max(int(start or 0), 0), limit_page_length=limit, order_by="modified desc",
	)


def create_tender(project_tender, **fields):
	frappe.has_permission(DOCTYPE, "create", throw=True)
	if not project_tender or not frappe.db.exists("Project Tender", project_tender):
		frappe.throw(_("Project Tender {0} not found").format(project_tender), frappe.DoesNotExistError)
	unknown = set(fields) - set(CREATE_FIELDS)
	if unknown:
		frappe.throw(_("Field(s) {0} cannot be set when creating a Tender.").format(", ".join(sorted(unknown))))
	doc = frappe.new_doc(DOCTYPE)
	doc.update({"project_tender": project_tender, **fields})
	doc.insert()
	return _view(doc)


def list_work_item_templates(search=None, limit=None, start=None):
	return list_view(
		TEMPLATE_DOCTYPE, ("name", "template_name", "description", "uom", "category"), None, search,
		("template_name", "description"), limit, start, order_by="template_name asc",
	)


def add_boq_item_from_template(tender, template, quantity, modified=None):
	"""FR-16. The controller appends under a row lock without needing the
	caller's snapshot (two users adding at once don't collide), so modified
	is only checked when the caller sends it."""
	doc = load(DOCTYPE, tender, "write")
	if modified:
		check_modified(doc, modified)
	load(TEMPLATE_DOCTYPE, template)
	quantity = positive(quantity, _("Quantity"))
	tender_controller.add_boq_item_from_template(tender, template, quantity)
	return _view(frappe.get_doc(DOCTYPE, tender))


def add_boq_lines(tender, groups=None, lines=None, modified=None):
	"""A reviewed client BOQ, added in one go: section rows as groups, each
	line from its template under its group, with the client's description,
	BOQ Item No. and rate (the rate pins the sell rate - see
	Tender.calculate_row_sell_pricing).

	One locked load and one save, so the batch lands whole or not at all.
	Lines whose BOQ Item No. is already on the Tender are skipped, so a
	retry after a partial failure elsewhere never adds a line twice. Lines
	whose template doesn't exist are returned in `missing` and left out;
	any other bad line fails the whole batch."""
	groups, lines = parse(groups, []) or [], parse(lines, []) or []
	if not lines:
		frappe.throw(_("No lines to add."))
	doc = load(DOCTYPE, tender, "write")
	if modified:
		check_modified(doc, modified)
	if doc.status == "Complete":
		frappe.throw(_("Tender {0} is Complete. Create a revised version to change it.").format(doc.name))
	doc = frappe.get_doc(DOCTYPE, tender, for_update=True)

	titles = {}
	for group in groups:
		if not group.get("key") or not (group.get("title") or "").strip():
			frappe.throw(_("Every group needs a key and a title."))
		titles[group["key"]] = group["title"].strip()[:140]

	existing = {row.boq_item_no for row in doc.boq_items if row.boq_item_no}
	seen, checked, missing = set(), [], {}
	for index, line in enumerate(lines, 1):
		number = (line.get("boq_item_no") or "").strip()
		label = number or _("Line {0}").format(index)
		if not number:
			frappe.throw(_("{0}: BOQ Item No. is required.").format(label))
		if number in seen:
			frappe.throw(_("BOQ Item No. {0} appears twice.").format(number))
		seen.add(number)
		if number in existing:
			continue
		if line.get("group") and line["group"] not in titles:
			frappe.throw(_("{0}: unknown group {1}.").format(label, line["group"]))
		template = line.get("template")
		if not template or not frappe.db.exists(TEMPLATE_DOCTYPE, template):
			# One line's template being gone (renamed, deleted, from another
			# site) shouldn't block the rest: it is reported, the others added.
			missing[number] = _("Work Item Template {0} not found.").format(template or "-")
			continue
		load(TEMPLATE_DOCTYPE, template)
		quantity = positive(line.get("quantity"), _("{0} quantity").format(label))
		client_rate = positive(line.get("client_rate") or 0, _("{0} client rate").format(label), allow_zero=True)
		checked.append((number, line, quantity, client_rate))

	# Everything is checked before anything is appended.
	group_rows = {row.item_name: row for row in doc.boq_items if row.is_group}
	earlier_groups = {id(row) for row in group_rows.values()}
	added, joining = {}, {}
	for number, line, quantity, client_rate in checked:
		indent, group_row = 0, None
		if line.get("group"):
			title = titles[line["group"]]
			if title not in group_rows:
				group_rows[title] = doc.append("boq_items", {"is_group": 1, "indent": 0, "item_name": title})
			group_row, indent = group_rows[title], 1
		row = tender_controller.append_boq_item_from_template(doc, line["template"], quantity, {
			"boq_item_no": number,
			"description": (line.get("description") or "").strip() or None,
			"client_rate": client_rate or None,
			"indent": indent,
		})
		added[number] = row
		if group_row is not None and id(group_row) in earlier_groups:
			joining[id(row)] = group_row
	if added:
		_place_under_groups(doc, joining)
		doc.save()
	return {
		"added": {number: row.name for number, row in added.items()},
		"skipped": sorted(seen & existing),
		"missing": missing,
		"view": _view(doc),
	}


def _place_under_groups(doc, joining):
	"""Rows belong to a group by position (a group row, then its indented
	rows). New lines for a group that was already on the Tender were
	appended at the end, so they are moved to the end of that group's
	block. Resource rows point at BOQ rows by idx and are re-pointed."""
	if not joining:
		return
	rows = list(doc.boq_items)
	rest = [row for row in rows if id(row) not in joining]
	block_end, current = {}, None
	for position, row in enumerate(rest):
		if row.is_group:
			current = row
			block_end[id(row)] = position
		elif row.indent and current is not None:
			block_end[id(current)] = position
		else:
			current = None
	moving = {}
	for row in rows:
		if id(row) in joining:
			moving.setdefault(id(joining[id(row)]), []).append(row)
	order = []
	for position, row in enumerate(rest):
		order.append(row)
		for group_id, end in block_end.items():
			if end == position:
				order += moving.pop(group_id, [])
	old_idx = {id(row): row.idx for row in rows}
	new_idx = {old_idx[id(row)]: position for position, row in enumerate(order, 1)}
	for fieldname in tender_controller.RESOURCE_TABLE_FIELDS:
		for resource in doc.get(fieldname):
			resource.work_item = new_idx.get(resource.work_item, resource.work_item)
	doc.boq_items = order
	for position, row in enumerate(order, 1):
		row.idx = position


def create_work_item_template_draft(template_name, uom, category=None, description=None):
	"""A template for a client BOQ line that has none yet: name, unit,
	category and description only. Its resources and costs are filled in
	by the Tender engineers in desk; until then the Tender line sells at the
	client's rate with no cost behind it."""
	frappe.has_permission(TEMPLATE_DOCTYPE, "create", throw=True)
	name = (template_name or "").strip()[:140]
	if not name:
		frappe.throw(_("Template name is required."))
	if not uom or not frappe.db.exists("UOM", uom):
		frappe.throw(_("Unit {0} does not exist in the ERP.").format(uom or "-"))
	description = (description or "").strip() or None
	base, n = name, 2
	while frappe.db.exists(TEMPLATE_DOCTYPE, name):
		# The same item again (another line or part of the BOQ with the same
		# text, or a retry): reuse it rather than make a numbered copy.
		same = frappe.db.get_value(TEMPLATE_DOCTYPE, name, ["uom", "description"], as_dict=True)
		if same.uom == uom and (same.description or None) == description:
			return {"name": name, "template_name": name, "uom": uom, "reused": True}
		suffix = f" ({n})"
		name = base[:140 - len(suffix)] + suffix
		n += 1
	doc = frappe.get_doc({
		"doctype": TEMPLATE_DOCTYPE, "template_name": name, "uom": uom,
		"category": (category or "").strip() or None, "description": description,
	})
	doc.insert()
	return {"name": doc.name, "template_name": doc.template_name, "uom": doc.uom, "reused": False}


RESOURCE_TABLE_GROUPS = {
	"labor_items": "Labor",
	"subcontract_items": "Subcontractors Items",
	"equipment_items": "Equipment",
}
TEMPLATE_TABLES = ("material_items", "labor_items", "subcontract_items", "equipment_items")


def list_item_options(search=None, item_groups=None, limit=None, start=None):
	"""Item picker for the breakdown intake's materials mapping: code, name,
	group, stock UOM and that UOM's conversions. Item read permission."""
	frappe.has_permission("Item", "read", throw=True)
	filters = {"disabled": 0}
	groups = parse(item_groups, None)
	if groups:
		filters["item_group"] = ["in", groups]
	result = list_view("Item", ("name", "item_name", "item_group", "stock_uom", "is_stock_item"), filters, search,
					   ("name", "item_name"), limit, start, order_by="item_name asc")
	codes = [row["name"] for row in result["data"]]
	conversions = {}
	if codes:
		for row in frappe.get_all("UOM Conversion Detail", filters={"parent": ["in", codes], "parenttype": "Item"},
								  fields=["parent", "uom", "conversion_factor"]):
			conversions.setdefault(row.parent, []).append({"uom": row.uom, "factor": row.conversion_factor})
	for row in result["data"]:
		row["uoms"] = conversions.get(row["name"], [])
	return result


def create_resource_item(item_name, item_group, uom):
	"""A Labor / Subcontractors Items / Equipment Item for a breakdown line
	that has none yet: non-stock, reused when one of that name already
	exists in that group. Material items are the stock department's."""
	if item_group not in RESOURCE_TABLE_GROUPS.values():
		frappe.throw(_("Only {0} items can be created here.").format(", ".join(RESOURCE_TABLE_GROUPS.values())))
	name = (item_name or "").strip()[:140]
	if not name:
		frappe.throw(_("Item name is required."))
	if not uom or not frappe.db.exists("UOM", uom):
		frappe.throw(_("Unit {0} does not exist in the ERP.").format(uom or "-"))
	existing = frappe.db.get_value("Item", {"item_name": name, "item_group": item_group, "disabled": 0}, "name")
	if existing:
		return {"name": existing, "reused": True}
	frappe.has_permission("Item", "create", throw=True)
	code = name if not frappe.db.exists("Item", name) else f"{name[:130]} - {item_group}"
	doc = frappe.get_doc({
		"doctype": "Item", "item_code": code, "item_name": name, "item_group": item_group, "stock_uom": uom,
		"is_stock_item": 0, "include_item_in_manufacturing": 0,
	})
	doc.insert()
	return {"name": doc.name, "reused": False}


def create_work_item_template(template_name, uom, description=None, category=None, lines=None, expected_total=None):
	"""A priced template from a breakdown block (docs/template-rules.md):
	lines = [{table, item, qty_per_unit, rate}]. The saved total must equal
	expected_total (the block's TOTAL ITEM DIRECT BUDGET) within 0.05, or
	nothing is kept. The same name with the same description and total
	returns the existing template; the same name with other content is an
	error - the caller names variants (rules §3)."""
	frappe.has_permission(TEMPLATE_DOCTYPE, "create", throw=True)
	lines = parse(lines, []) or []
	name = (template_name or "").strip()
	description = (description or "").strip() or None
	if not name or len(name) > 140:
		frappe.throw(_("Template name is required and must be at most 140 characters."))
	if not uom or not frappe.db.exists("UOM", uom):
		frappe.throw(_("Unit {0} does not exist in the ERP.").format(uom or "-"))
	if not lines:
		frappe.throw(_("A template needs at least one priced line."))
	expected = float(expected_total) if expected_total not in (None, "") else None

	if frappe.db.exists(TEMPLATE_DOCTYPE, name):
		same = frappe.db.get_value(TEMPLATE_DOCTYPE, name, ["description", "total_cost_per_unit"], as_dict=True)
		if (same.description or None) == description and (
				expected is None or abs((same.total_cost_per_unit or 0) - expected) <= 0.05):
			return {"name": name, "total_cost_per_unit": same.total_cost_per_unit, "reused": True}
		frappe.throw(_("A different template is already named {0}.").format(name), frappe.DuplicateEntryError)

	doc = frappe.new_doc(TEMPLATE_DOCTYPE)
	doc.update({"template_name": name, "uom": uom, "category": (category or "").strip() or None,
				"description": description})
	for index, line in enumerate(lines, 1):
		table = line.get("table")
		if table not in TEMPLATE_TABLES:
			frappe.throw(_("Line {0}: unknown table {1}.").format(index, table))
		item = line.get("item")
		if not item or not frappe.db.exists("Item", item):
			frappe.throw(_("Line {0}: Item {1} not found.").format(index, item or "-"))
		doc.append(table, {
			"item": item,
			"qty_per_unit": positive(line.get("qty_per_unit"), _("Line {0} quantity").format(index)),
			"rate": positive(line.get("rate"), _("Line {0} rate").format(index), allow_zero=True),
		})
	# A savepoint, so a wrong total leaves nothing behind for in-process
	# callers too (an HTTP request rolls back on the error anyway).
	frappe.db.savepoint("work_item_template_create")
	doc.insert()
	if expected is not None and abs((doc.total_cost_per_unit or 0) - expected) > 0.05:
		total = doc.total_cost_per_unit
		frappe.db.rollback(save_point="work_item_template_create")
		frappe.throw(_("The template adds up to {0}, the breakdown says {1}.").format(
			frappe.format(total, "Currency"), frappe.format(expected, "Currency")))
	return {"name": doc.name, "total_cost_per_unit": doc.total_cost_per_unit, "reused": False}


def update_tender_boq_quantities(tender, modified, rows):
	doc = load(DOCTYPE, tender, "write")
	prepare_save(doc, modified)
	changes = [
		(row, positive(entry.get("original_quantity"), _("Row {0} quantity").format(row.idx)))
		for row, entry in rows_by_name(doc, "boq_items", rows, ("original_quantity",), _("BOQ"))
	]
	if not changes:
		frappe.throw(_("Nothing to change."))
	for row, qty in changes:
		row.original_quantity = qty
	doc.save()
	return _view(doc)


def mark_tender_complete(tender, modified):
	doc = load(DOCTYPE, tender, "write")
	# The controller's frappe.only_for is skipped whenever flags.in_test is
	# set, so the role rule is enforced here as well, where it is testable.
	if not _is_tender_manager():
		frappe.throw(_("Only a {0} can mark a Tender Complete.").format(MARK_COMPLETE_ROLE), frappe.PermissionError)
	check_modified(doc, modified)
	tender_controller.mark_tender_complete(tender)
	return _view(frappe.get_doc(DOCTYPE, tender))


def create_revised_version(tender, modified):
	doc = load(DOCTYPE, tender, "write")
	check_modified(doc, modified)
	new_name = tender_controller.create_revised_version(tender)
	return _view(frappe.get_doc(DOCTYPE, new_name))


def attach_tender_file(tender, modified, filename, content):
	"""The client BOQ a Tender was built from. Tender has no Attach field, so
	it goes on the Tender's attachments."""
	return _view(attach_unfielded(DOCTYPE, tender, modified, filename, content))


def _view(doc):
	writable = bool(doc.has_permission("write"))
	return view(doc, extra_can={
		"add_boq": writable and doc.status != "Complete",
		"mark_complete": writable and doc.status == "In Progress" and _is_tender_manager(),
		"revise": writable and doc.status == "Complete",
	})


def _is_tender_manager():
	return frappe.session.user == "Administrator" or MARK_COMPLETE_ROLE in frappe.get_roles()
