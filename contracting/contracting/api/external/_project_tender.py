import frappe
from frappe import _

from contracting.contracting.api.external._common import list_view, load, positive, prepare_save, rows_by_name, view

DOCTYPE = "Project Tender"

# BRD §4 / FR-13 (2026-10-20): status is never written through the API, and a
# closed bid's lines are frozen - every save of a Won Project Tender re-runs
# handle_won_automation and cross-syncs its Tenders.
LOCKED_STATUSES = ("Won", "Lost", "Cancelled", "Superseded")

LIST_FIELDS = ("name", "project_name", "client", "status", "project_type", "total_sell_price", "modified")

# Which value drives a cost line is fixed by its calculation_type
# (project_tender.py derives the other).
FIELD_BY_CALCULATION_TYPE = {
	"Fixed Amount": "amount",
	"Percentage of Direct Cost": "rate",
	"Percentage of Total Price": "rate",
}
EDITABLE_TABLES = {
	"indirect_cost_details": "Indirect Cost",
	"overhead_deduction_details": "Overhead / Deduction",
}


def list_project_tenders(filters=None, search=None, limit=None, start=None):
	return list_view(DOCTYPE, LIST_FIELDS, filters, search, ("name", "project_name"), limit, start)


def get_project_tender_view(name):
	doc = load(DOCTYPE, name)
	return _view(doc)


def update_project_tender_lines(name, modified, indirect_cost_details=None, overhead_deduction_details=None):
	doc = load(DOCTYPE, name, "write")
	if doc.status in LOCKED_STATUSES:
		frappe.throw(_("Project Tender is {0} and can no longer be edited here.").format(doc.status))
	prepare_save(doc, modified)

	changes = []
	for table, entries in (
		("indirect_cost_details", indirect_cost_details),
		("overhead_deduction_details", overhead_deduction_details),
	):
		for row, entry in rows_by_name(doc, table, entries, ("rate", "amount"), EDITABLE_TABLES[table]):
			if "rate" in entry and "amount" in entry:
				frappe.throw(_("Row {0}: send either rate or amount, not both.").format(row.idx))
			field = FIELD_BY_CALCULATION_TYPE.get(row.calculation_type)
			if not field:
				frappe.throw(_("Row {0} has no Calculation Type and can't be edited here.").format(row.idx))
			if field not in entry:
				frappe.throw(
					_("Row {0} is {1}: set its {2}.").format(row.idx, row.calculation_type, field)
				)
			changes.append((row, field, positive(entry[field], _("Row {0} {1}").format(row.idx, field), True)))

	if not changes:
		frappe.throw(_("Nothing to change."))
	for row, field, value in changes:
		row.set(field, value)
	doc.save()
	return _view(doc)


def _view(doc):
	return view(doc, extra_can={"edit_lines": bool(doc.has_permission("write")) and doc.status not in LOCKED_STATUSES})
