import frappe
from frappe.model.workflow import get_workflow_name

from contracting.contracting.api.external._common import allowed_actions

INBOX_DOCTYPES = ("Subcontractor Contract", "Contractor Invoice", "Contractor Contract Addendum")
TITLE_FIELD = {
	"Subcontractor Contract": "contract_title",
	"Contractor Invoice": "contractor_contract",
	"Contractor Contract Addendum": "subcontractor_contract",
}
MAX_CANDIDATES = 500


def list_my_pending_approvals(limit=50, start=0):
	"""FR-35: every document the current user can act on right now. Narrowed
	in SQL to the states where one of the user's roles owns a transition,
	then each candidate is checked with get_transitions, so a transition
	condition (e.g. checklist_attachment) is honoured the same as in desk."""
	roles = set(frappe.get_roles())
	result = []
	for doctype in INBOX_DOCTYPES:
		workflow_name = get_workflow_name(doctype)
		if not workflow_name or not frappe.has_permission(doctype, "read"):
			continue
		states = sorted({
			t.state for t in frappe.get_cached_doc("Workflow", workflow_name).transitions if t.allowed in roles
		})
		if not states:
			continue
		for row in frappe.get_list(
			doctype, filters={"workflow_state": ["in", states], "docstatus": ["<", 2]},
			fields=["name", "workflow_state", "modified", TITLE_FIELD[doctype]],
			order_by="modified asc", limit_page_length=MAX_CANDIDATES,
		):
			actions = allowed_actions(frappe.get_doc(doctype, row.name))
			if actions:
				result.append({
					"doctype": doctype, "name": row.name, "title": row.get(TITLE_FIELD[doctype]),
					"workflow_state": row.workflow_state, "modified": str(row.modified),
					"allowed_actions": actions,
				})
	start, limit = max(int(start or 0), 0), min(max(int(limit or 50), 1), 100)
	return {"data": result[start:start + limit], "meta": {"count": len(result), "start": start, "limit": limit}}
