"""Contractor Contract Addendum authoring (confirmed in scope 2026-10-20).

Desk edits an addendum through row buttons - Update / Delete on the
contract's own rows, plain new rows for Add (contractor_contract_addendum.js).
The server already recomputes every derived figure from just the reference
and the requested change (_validate_base_row / _validate_added_row), so the
app sends that same vocabulary:

	{"base_contract_item": X, "action": "Update", "new_qty": q, "new_unit_price": p}
	{"base_contract_item": X, "action": "Delete"}
	{"base_contract_item": X, "action": ""}          undo a change
	{"action": "Add", "work_item" | "work_item_template": Y, "qty_delta": q,
	 "unit_price": p, "resource_lines": [tender resource row names]?}
	{"row_name": R, "remove": true}                  drop an Add row
	{"row_name": R, "qty_delta"?, "unit_price"?, "resource_lines"?}  edit an Add row
"""

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
	run_action,
	view,
)
from contracting.contracting.doctype.contractor_contract_addendum import contractor_contract_addendum as controller

DOCTYPE = "Contractor Contract Addendum"
CONTRACT_DOCTYPE = "Subcontractor Contract"
UPDATE, DELETE, ADD = controller.UPDATE, controller.DELETE, controller.ADD
LIST_FIELDS = ("name", "subcontractor_contract", "project", "addendum_date", "workflow_state", "docstatus",
			   "grand_total_delta", "modified")
ADD_FIELDS = ("action", "work_item", "work_item_template", "qty_delta", "unit_price", "resource_lines")
CONDITION_FIELDS = ("base_condition", "work_item", "work_item_template", "condition", "percent", "category")


def list_addenda(subcontractor_contract=None, workflow_state=None, filters=None, search=None, limit=None, start=None):
	filters = parse(filters, {}) or {}
	if subcontractor_contract:
		filters["subcontractor_contract"] = subcontractor_contract
	if workflow_state:
		filters["workflow_state"] = workflow_state
	return list_view(DOCTYPE, LIST_FIELDS, filters, search, ("name", "subcontractor_contract"), limit, start)


def get_addendum_view(name):
	return _view(load(DOCTYPE, name))


def get_addendum_draft_options(subcontractor_contract):
	"""The contract's rows and Payment Conditions as the addendum's starting
	point - what desk's load_base_items / Load Contract Conditions fetch."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	_require_amendable(subcontractor_contract)
	return {
		"items": controller.get_contract_items_for_addendum(subcontractor_contract),
		"payment_conditions": controller.get_contract_payment_conditions(subcontractor_contract),
	}


def get_addendum_add_options(subcontractor_contract, work_item):
	"""Desk's Pick Lines dialog for an Add row."""
	return controller.get_addable_lines(subcontractor_contract, work_item)


def create_addendum_draft(subcontractor_contract, items=None, payment_conditions=None, addendum_date=None):
	frappe.has_permission(DOCTYPE, "create", throw=True)
	_require_amendable(subcontractor_contract)
	doc = frappe.new_doc(DOCTYPE)
	doc.update({
		"subcontractor_contract": subcontractor_contract,
		"addendum_date": addendum_date,
	})
	for row in controller.get_contract_items_for_addendum(subcontractor_contract):
		doc.append("addendum_items", row)
	_apply_items(doc, parse(items, []) or [])
	_set_conditions(doc, payment_conditions)
	doc.insert()
	return _view(doc)


def update_addendum_draft(name, modified, items=None, payment_conditions=None, addendum_date=None):
	"""payment_conditions, when sent, replaces the addendum's whole
	Payment Conditions table (rows with base_condition replace a contract
	condition, rows without one are new)."""
	doc = load(DOCTYPE, name, "write")
	require_draft(doc)
	prepare_save(doc, modified)
	items = parse(items, []) or []
	if not items and payment_conditions is None and addendum_date is None:
		frappe.throw(_("Nothing to change."))
	_apply_items(doc, items)
	if payment_conditions is not None:
		_set_conditions(doc, payment_conditions)
	if addendum_date is not None:
		doc.addendum_date = addendum_date
	doc.save()
	return _view(doc)


def preview_addendum_action(name, action):
	return preview_action(load(DOCTYPE, name), action)


def apply_addendum_action(name, modified, action):
	return _view(run_action(load(DOCTYPE, name), modified, action))


def attach_addendum_file(name, fieldname, modified, filename, content):
	return _view(attach(DOCTYPE, name, fieldname, modified, filename, content))


def _require_amendable(subcontractor_contract):
	contract = frappe.db.get_value(
		CONTRACT_DOCTYPE, subcontractor_contract, ["docstatus", "workflow_state", "is_closed"], as_dict=True
	)
	if not contract:
		frappe.throw(_("Subcontractor Contract {0} not found").format(subcontractor_contract), frappe.DoesNotExistError)
	frappe.has_permission(CONTRACT_DOCTYPE, "read", doc=subcontractor_contract, throw=True)
	if contract.docstatus != 1 or contract.workflow_state != "Approved":
		frappe.throw(_("An addendum can only amend an Approved contract."))
	if contract.is_closed:
		frappe.throw(_("Contract {0} is closed and cannot be amended.").format(subcontractor_contract))


def _apply_items(doc, entries):
	"""Checks every entry before changing anything."""
	base_rows = {row.base_contract_item: row for row in doc.addendum_items if row.base_contract_item}
	own_rows = {row.name: row for row in doc.addendum_items if not row.base_contract_item}
	plan = []
	for entry in entries:
		if entry.get("base_contract_item"):
			row = base_rows.get(entry["base_contract_item"])
			if not row:
				frappe.throw(_("{0} is not a row of contract {1}.").format(entry["base_contract_item"],
																		 doc.subcontractor_contract))
			action = entry.get("action") or ""
			extra = set(entry) - {"base_contract_item", "action", "new_qty", "new_unit_price"}
			if extra or action not in (UPDATE, DELETE, ""):
				frappe.throw(_("A contract row can only be Updated (new_qty, new_unit_price), Deleted, or reset."))
			if action == UPDATE:
				values = {
					"action": UPDATE,
					"new_qty": positive(entry.get("new_qty"), _("new_qty"), allow_zero=True),
					"new_unit_price": positive(entry.get("new_unit_price"), _("new_unit_price"), allow_zero=True),
				}
			else:
				# Delete / reset: the server derives the figures from the
				# base row on validate.
				values = {"action": action}
			plan.append(("set", row, values))
		elif entry.get("row_name"):
			row = own_rows.get(entry["row_name"])
			if not row:
				frappe.throw(_("{0} is not an added row of {1}.").format(entry["row_name"], doc.name))
			if entry.get("remove"):
				plan.append(("remove", row, None))
				continue
			extra = set(entry) - {"row_name", "qty_delta", "unit_price", "resource_lines"}
			if extra:
				frappe.throw(_("Field(s) {0} can't be changed on an added row.").format(", ".join(sorted(extra))))
			plan.append(("set", row, _add_values(entry, partial=True)))
		elif entry.get("action") == ADD:
			unknown = set(entry) - set(ADD_FIELDS)
			if unknown:
				frappe.throw(_("Field(s) {0} are not accepted on an Add row.").format(", ".join(sorted(unknown))))
			if bool(entry.get("work_item")) == bool(entry.get("work_item_template")):
				frappe.throw(_("An Add row needs exactly one of work_item or work_item_template."))
			values = _add_values(entry)
			values.update({"action": ADD, "work_item": entry.get("work_item"),
						   "work_item_template": entry.get("work_item_template")})
			plan.append(("append", None, values))
		else:
			frappe.throw(_("Each item needs base_contract_item, row_name, or action Add."))

	for op, row, values in plan:
		if op == "set":
			row.update(values)
		elif op == "remove":
			doc.remove(row)
		else:
			doc.append("addendum_items", values)


def _add_values(entry, partial=False):
	values = {}
	if "qty_delta" in entry or not partial:
		values["qty_delta"] = positive(entry.get("qty_delta"), _("qty_delta"))
	if "unit_price" in entry or not partial:
		values["unit_price"] = positive(entry.get("unit_price"), _("unit_price"), allow_zero=True)
	if "resource_lines" in entry:
		lines = parse(entry.get("resource_lines"), []) or []
		values["resource_lines"] = frappe.as_json(lines, indent=None) if lines else None
	return values


def _set_conditions(doc, payment_conditions):
	rows = parse(payment_conditions, []) or []
	for row in rows:
		unknown = set(row) - set(CONDITION_FIELDS)
		if unknown:
			frappe.throw(_("Payment Condition field(s) {0} are not accepted.").format(", ".join(sorted(unknown))))
		positive(row.get("percent"), _("Payment Condition percent"))
	doc.set("addendum_payment_conditions", [])
	for row in rows:
		doc.append("addendum_payment_conditions", row)


def _view(doc):
	return view(doc, extra_can={
		"edit": bool(doc.has_permission("write")) and doc.docstatus == 0
		and (doc.workflow_state or "") in ("", "Draft"),
	})
