"""Who the caller is, and what happened to a document - for the external
app's shell (sidebar, default language) and its read-only status timeline."""

import frappe
from frappe import _
from frappe.utils import cstr

from contracting.contracting.api.external._common import load

# The doctypes the external app has screens for.
APP_DOCTYPES = (
	"Project Tender",
	"Tender",
	"Subcontractor Contract",
	"Contractor Invoice",
	"Contractor Contract Addendum",
)
TIMELINE_COMMENT_TYPES = ("Workflow", "Comment")


def get_session_info():
	user = frappe.session.user
	values = frappe.db.get_value("User", user, ["full_name", "language"], as_dict=True) or {}
	return {
		"user": user,
		"full_name": values.get("full_name") or user,
		"language": values.get("language") or frappe.db.get_single_value("System Settings", "language") or "en",
		"roles": sorted(frappe.get_roles(user)),
		"can": {
			doctype: {
				"read": bool(frappe.has_permission(doctype, "read")),
				"create": bool(frappe.has_permission(doctype, "create")),
			}
			for doctype in APP_DOCTYPES
		},
	}


def get_document_timeline(doctype, name):
	"""Workflow history (Frappe writes one "Workflow" Comment per state
	change, owned by whoever made it) plus the comments people left -
	rejection reasons are written there. Oldest first."""
	if doctype not in APP_DOCTYPES:
		frappe.throw(_("No timeline for {0}.").format(doctype), frappe.PermissionError)
	load(doctype, name)

	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": doctype,
			"reference_name": name,
			"comment_type": ["in", TIMELINE_COMMENT_TYPES],
		},
		fields=["comment_type", "content", "owner", "creation"],
		order_by="creation asc",
	)
	names = {}
	for row in rows:
		if row.owner not in names:
			names[row.owner] = frappe.db.get_value("User", row.owner, "full_name") or row.owner

	doc_owner, doc_creation = frappe.db.get_value(doctype, name, ["owner", "creation"])
	events = [{"type": "Created", "content": None, "user": doc_owner,
			   "full_name": frappe.db.get_value("User", doc_owner, "full_name") or doc_owner,
			   "at": cstr(doc_creation)}]
	for row in rows:
		events.append({
			"type": "State" if row.comment_type == "Workflow" else "Comment",
			"content": row.content,
			"user": row.owner,
			"full_name": names[row.owner],
			"at": cstr(row.creation),
		})
	return {"data": events, "meta": {"doctype": doctype, "name": name}}
