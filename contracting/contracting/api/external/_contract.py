import frappe
from frappe import _

from contracting.contracting.api.external._common import (
	attach,
	list_view,
	load,
	parse,
	positive,
	prepare_save,
	preview_action,
	require_draft,
	rows_by_name,
	run_action,
	view,
)
from contracting.contracting.doctype.subcontractor_contract import subcontractor_contract as controller

DOCTYPE = "Subcontractor Contract"
ADDENDUM_DOCTYPE = "Contractor Contract Addendum"
DEPRECATED_TABLES = ("items_subcontracted",)

LIST_FIELDS = (
	"name", "contract_title", "project", "contractor_name", "type_subcontractor", "workflow_state",
	"docstatus", "net_total", "is_closed", "modified",
)
# Header fields the app may set on a draft. price_exception_acknowledged is
# the form's "Delegate to Cost Control" (validate_price_ceiling).
HEADER_FIELDS = ("contract_title", "department", "contract_date", "price_exception_acknowledged")
LINE_FIELDS = ("tender_boq_item", "work_item_template", "qty", "unit_price", "pricing_mode")
PRICING_MODES = ("Total", "Components")

OUT_OF_SCOPE_GRIDS = {
	"contracted_items": "Contracted Items",
	"contract_material_items": "Material",
	"contract_labor_items": "Labor",
	"contract_subcontract_items": "Subcontract",
	"contract_equipment_items": "Equipment",
}
OUT_OF_SCOPE_ACTION_STATE = "Pending Cost Control Approval"


def list_subcontractor_contracts(project=None, supplier=None, workflow_state=None, filters=None, search=None,
								 limit=None, start=None):
	filters = parse(filters, {}) or {}
	for field, value in (("project", project), ("contractor_name", supplier), ("workflow_state", workflow_state)):
		if value:
			filters[field] = value
	return list_view(DOCTYPE, LIST_FIELDS, filters, search, ("name", "contract_title", "contractor_name"), limit,
					 start)


def get_subcontractor_contract_view(name):
	return _view(load(DOCTYPE, name))


def get_contract_draft_options(project, contract_type, department=None):
	"""What the desk "Select Work Item" / "Select Work Item Template" dialogs
	offer, without the app ever listing a child doctype itself. No cost
	figures: the contracts department can't see them."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	if not project or not frappe.db.exists("Project", project):
		frappe.throw(_("Project {0} not found").format(project), frappe.DoesNotExistError)
	tenders = controller.get_project_tenders(project, department)
	return {
		"tenders": tenders,
		"lines": controller.get_unallocated_boq_items(tenders, contract_type) if tenders else [],
		"templates": controller.get_work_item_templates(),
	}


# The contracts roles (and Site Engineer, for invoices) hold no permission on
# Project or Supplier, though every contract and invoice links to both - so,
# like get_work_item_templates, these pickers return names only, to whoever
# may create the document that needs them.
PICKER_DOCTYPES = (DOCTYPE, "Contractor Invoice", ADDENDUM_DOCTYPE)


def list_project_options(search=None, limit=None, start=None):
	if not any(frappe.has_permission(dt, "create") for dt in PICKER_DOCTYPES):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return _picker("Project", ["name", "project_name", "status"], {}, ("name", "project_name"), search, limit, start)


def list_subcontractor_options(search=None, limit=None, start=None):
	"""Desk's contractor_name query: suppliers flagged as subcontractors."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	# Registration numbers too, so a contract read from a document can be
	# matched to its supplier by tax id / commercial register, not just by a
	# name that may be spelt differently in the document.
	return _picker(
		"Supplier", ["name", "supplier_name", "tax_id", "custom_commercial_register"],
		{"custom_is_subcontractor": 1, "disabled": 0},
		("name", "supplier_name", "tax_id", "custom_commercial_register"), search, limit, start,
	)


def _picker(doctype, fields, filters, search_fields, search, limit, start):
	or_filters = [[field, "like", f"%{search}%"] for field in search_fields] if search else None
	return frappe.get_all(
		doctype, fields=fields, filters=filters, or_filters=or_filters,
		limit_start=max(int(start or 0), 0), limit_page_length=min(max(int(limit or 20), 1), 100),
		order_by="modified desc",
	)


def create_subcontractor_contract_draft(project, supplier, contract_type, lines, **header):
	frappe.has_permission(DOCTYPE, "create", throw=True)
	_check_header(header)
	lines = parse(lines, []) or []
	if not lines:
		frappe.throw(_("Add at least one line."))

	doc = frappe.new_doc(DOCTYPE)
	doc.update({
		"project": project,
		"contractor_name": supplier,
		"type_subcontractor": contract_type,
		**header,
	})
	tender_lines = [line for line in lines if line.get("tender_boq_item")]
	if tender_lines:
		_require_unallocated(project, header.get("department"), contract_type, tender_lines)
	for line in lines:
		doc.append("contracted_items", _line_row(line))
	doc.insert()
	return _view(doc)


def update_subcontractor_contract_lines(name, modified, rows=None, add=None, remove=None, resources=None,
										**header):
	doc = load(DOCTYPE, name, "write")
	require_draft(doc)
	_check_header(header)
	prepare_save(doc, modified)

	changes = []
	for row, entry in rows_by_name(
		doc, "contracted_items", rows, ("qty", "unit_price", "pricing_mode"), _("Contracted Items")
	):
		for field in ("qty", "unit_price"):
			if field in entry:
				changes.append((row, field, positive(entry[field], _("Row {0} {1}").format(row.idx, field))))
		if "pricing_mode" in entry:
			if entry["pricing_mode"] not in PRICING_MODES:
				frappe.throw(_("pricing_mode must be one of {0}.").format(", ".join(PRICING_MODES)))
			changes.append((row, "pricing_mode", entry["pricing_mode"]))
	changes += _resource_rate_changes(doc, resources)

	remove = set(parse(remove, []) or [])
	unknown = remove - {row.name for row in doc.contracted_items}
	if unknown:
		frappe.throw(_("Row(s) {0} are not rows of {1}.").format(", ".join(sorted(unknown)), doc.name))
	add = parse(add, []) or []
	new_rows = [_line_row(line) for line in add]
	tender_lines = [line for line in add if line.get("tender_boq_item")]
	if tender_lines:
		_require_unallocated(doc.project, doc.department, doc.type_subcontractor, tender_lines, doc.name)

	if not (changes or remove or new_rows or header):
		frappe.throw(_("Nothing to change."))
	for row, field, value in changes:
		row.set(field, value)
	if remove:
		# The controller's prune_orphaned_resource_rows cascades the removed
		# lines' resource rows, as the form's own row delete does.
		doc.set("contracted_items", [row for row in doc.contracted_items if row.name not in remove])
	for row in new_rows:
		doc.append("contracted_items", row)
	doc.update(header)
	doc.save()
	return _view(doc)


def preview_subcontractor_contract_action(name, action):
	doc = load(DOCTYPE, name)
	extra = {}
	if action == "Approve" and doc.workflow_state == OUT_OF_SCOPE_ACTION_STATE and doc.has_out_of_tender_items:
		# What desk's before_workflow_action dialog lists before this approval.
		extra["out_of_tender_rows"] = out_of_tender_rows(doc)
		extra["warning"] = _("Approving will send this contract to CEO approval.")
	return preview_action(doc, action, **extra)


def apply_subcontractor_contract_action(name, modified, action):
	return _view(run_action(load(DOCTYPE, name), modified, action))


def attach_subcontractor_contract_file(name, fieldname, modified, filename, content):
	return _view(attach(DOCTYPE, name, fieldname, modified, filename, content))


def out_of_tender_rows(doc):
	rows = []
	for fieldname, tab in OUT_OF_SCOPE_GRIDS.items():
		for row in doc.get(fieldname):
			if not row.get("out_of_tender_scope"):
				continue
			if fieldname == "contracted_items":
				reason = (_("Unit price is above the Tender cost (delegated to Cost Control)") if row.work_item
						  else _("Line built from a Work Item Template, not the Tender"))
			elif row.get("source") == "Tender":
				reason = _("Rate is above the Tender rate")
			else:
				reason = _("Row added outside the Tender ({0})").format(
					_("template") if row.get("source") == "Template" else _("extra"))
			rows.append({
				"tab": tab, "idx": row.idx,
				"label": row.get("description") or row.get("resource_item") or row.get("item"),
				"reason": reason,
			})
	return rows


def _view(doc):
	# Contracts Engineer reads contracts but not addenda.
	addenda = frappe.get_list(
		ADDENDUM_DOCTYPE, filters={"subcontractor_contract": doc.name},
		fields=["name", "workflow_state", "docstatus", "grand_total_delta", "addendum_date"], order_by="creation asc",
	) if frappe.has_permission(ADDENDUM_DOCTYPE, "read") else []
	data = view(doc, drop_tables=DEPRECATED_TABLES, extra_can={
		"edit_lines": bool(doc.has_permission("write")) and doc.docstatus == 0
		and (doc.workflow_state or "") in ("", "Draft"),
	})
	data["data"]["addenda"] = addenda
	return data


def _check_header(header):
	unknown = set(header) - set(HEADER_FIELDS)
	if unknown:
		frappe.throw(_("Field(s) {0} cannot be set here.").format(", ".join(sorted(unknown))))


def _line_row(line):
	unknown = set(line) - set(LINE_FIELDS)
	if unknown:
		frappe.throw(_("Line field(s) {0} are not accepted.").format(", ".join(sorted(unknown))))
	if bool(line.get("tender_boq_item")) == bool(line.get("work_item_template")):
		frappe.throw(_("Each line needs exactly one of tender_boq_item or work_item_template."))
	pricing_mode = line.get("pricing_mode") or "Total"
	if pricing_mode not in PRICING_MODES:
		frappe.throw(_("pricing_mode must be one of {0}.").format(", ".join(PRICING_MODES)))
	row = {
		"work_item": line.get("tender_boq_item"),
		"work_item_template": line.get("work_item_template"),
		"qty": positive(line.get("qty"), _("Line qty")),
		"pricing_mode": pricing_mode,
	}
	if pricing_mode == "Total":
		row["unit_price"] = positive(line.get("unit_price"), _("Line unit_price"))
	return row


def _require_unallocated(project, department, contract_type, tender_lines, contract=None):
	"""BRD §10: a Tender line with nothing left to contract is refused up
	front, with no draft created. A line with *some* quantity left but less
	than asked goes through to the controller's own over-allocation error,
	which is the one desk shows."""
	tenders = controller.get_project_tenders(project, department)
	available = {
		row["work_item"]
		for row in (controller.get_unallocated_boq_items(tenders, contract_type, contract) if tenders else [])
	}
	missing = [line["tender_boq_item"] for line in tender_lines if line["tender_boq_item"] not in available]
	if missing:
		frappe.throw(_("No unallocated quantity on the selected Tender lines: {0}").format(", ".join(missing)))


RESOURCE_TABLES = (
	"contract_material_items", "contract_labor_items", "contract_subcontract_items", "contract_equipment_items",
)


def _resource_rate_changes(doc, resources):
	"""Components pricing: [{table, row_name, rate}] on the contract's own
	resource rows. The line's unit_price is then the controller's rollup
	(compute_component_pricing) - it stays the only place prices are worked
	out."""
	changes = []
	for entry in parse(resources, []) or []:
		table = entry.get("table")
		if table not in RESOURCE_TABLES:
			frappe.throw(_("{0} is not a resource table.").format(table))
		extra = set(entry) - {"table", "row_name", "rate"}
		if extra or "rate" not in entry:
			frappe.throw(_("Resource rows take only table, row_name and rate."))
		row = next((r for r in doc.get(table) if r.name == entry.get("row_name")), None)
		if not row:
			frappe.throw(_("Resource row {0} is not a row of {1}.").format(entry.get("row_name"), doc.name))
		changes.append((row, "rate", positive(entry["rate"], _("{0} row {1} rate").format(table, row.idx),
											   allow_zero=True)))
	return changes
