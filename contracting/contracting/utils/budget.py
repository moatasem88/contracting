import frappe
from frappe import _
from frappe.utils import flt


def validate_items_qty_with_tolerance(doc):
	"""Shared by Material Request and Purchase Order: qty within
	material_budget_tolerance_percent of allowed_qty passes silently;
	beyond it, blocked unless the submitting user holds
	budget_override_role, in which case it's allowed with a logged
	comment on the document."""
	if not doc.contracting:
		return

	tolerance = frappe.db.get_single_value("Contracting Settings", "material_budget_tolerance_percent") or 0
	override_role = frappe.db.get_single_value("Contracting Settings", "budget_override_role")
	has_override = bool(override_role) and override_role in frappe.get_roles(frappe.session.user)

	work_item_budgets = get_work_item_budgets(doc)

	for item in doc.get("items"):
		if not item.qty:
			continue

		allowed_qty, basis = resolve_allowed_qty(item, work_item_budgets)
		if not allowed_qty:
			continue

		limit = allowed_qty * (1 + tolerance / 100.0)
		if item.qty <= limit + 1e-6:
			continue

		if has_override:
			# doc.add_comment() would fail here on a document's first
			# insert - validate() runs before the row exists in the DB,
			# so the Comment's reference_name link can't resolve yet.
			# Insert it directly with ignore_links instead.
			frappe.get_doc({
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": doc.doctype,
				"reference_name": doc.name,
				"content": _(
					"Row {0}: qty {1} exceeds the budget tolerance limit ({2}, {3}% over allowed "
					"qty {4}, basis: {5}) - allowed via override by {6}."
				).format(item.idx, item.qty, limit, tolerance, allowed_qty, basis, frappe.session.user),
			}).insert(ignore_permissions=True, ignore_links=True)
		else:
			frappe.throw(
				_(
					"Row {0}: qty {1} exceeds the allowed budget of {2} (allowed qty {3} + {4}% "
					"tolerance, basis: {5}). A user with the '{6}' role can override."
				).format(
					item.idx, item.qty, limit, allowed_qty, tolerance, basis,
					override_role or _("(none configured)"),
				)
			)


def get_work_item_budgets(doc):
	"""FR-16 - tender-derived material budgets per selected work item.

	Only computed for work-item-scoped Material Requests. Returns
	{project_work_item: {item_code: budget}}.
	"""
	if doc.doctype != "Material Request":
		return {}
	if doc.get("custom_is_general_material"):
		return {}

	rows = doc.get("custom_work_items") or []
	if not rows:
		return {}

	from contracting.contracting.utils import allocation

	return {
		row.project_work_item: allocation.get_material_budget(row.project_work_item)
		for row in rows
		if row.project_work_item
	}


def resolve_allowed_qty(item, work_item_budgets):
	"""The budget this line is measured against, and where it came from.

	Two bases coexist deliberately:

	* Work-item-scoped lines use the Tender's own material estimate, net of
	  quantity already committed to Supply-and-Install contracts (FR-16) -
	  that share is the subcontractor's to supply, so the company shouldn't
	  be procuring it too.
	* Everything else keeps the pre-existing Costing Note basis, so
	  material requests that have nothing to do with subcontracting behave
	  exactly as they did before.
	"""
	project_work_item = item.get("custom_project_work_item")
	if project_work_item and project_work_item in work_item_budgets:
		budget = work_item_budgets[project_work_item].get(item.item_code)
		if budget is not None:
			return flt(budget), _("work item {0} material budget net of Supply-and-Install commitments").format(
				project_work_item
			)

	return flt(item.get("allowed_qty")), _("Costing Note remaining qty")
