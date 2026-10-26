"""Shared plumbing for the external app's API (BRD 2026-10-20).

Every v1 method runs as the logged-in ERP user (the app forwards that user's
own sid), so the ERP's permissions, workflows and validation all stay in
force. Nothing here, or in the per-doctype services beside it, passes
ignore_permissions or sets docstatus/workflow_state directly - test_v1_common
greps the package for that.
"""

import base64

import frappe
from frappe import _
from frappe.model.workflow import apply_workflow, get_transitions, get_workflow_name
from frappe.utils import cint, cstr, flt

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

DRAFT_STATES = ("", "Draft")

# Which Attach fields the app may set, per doctype. When each may change is
# still decided by the doctype's own guards (contractor_invoice.
# _enforce_attachment_windows, approval_guards.enforce_checklist_window).
ATTACH_FIELDS = {
	"Contractor Invoice": ("invoice_attachment", "checklist_attachment"),
	"Subcontractor Contract": ("checklist_attachment", "attached_contract"),
	"Contractor Contract Addendum": ("checklist_attachment",),
}

STALE_MESSAGE = "This document was changed by someone else. Reload to see the latest version."


def parse(value, default=None):
	"""Arguments arrive JSON-encoded over /api/method; Python callers pass objects."""
	if value is None or value == "":
		return default
	if isinstance(value, str):
		return frappe.parse_json(value)
	return value


# ---------------------------------------------------------------------------
# Loading and guards
# ---------------------------------------------------------------------------

def load(doctype, name, ptype="read"):
	"""FR-06: permission is always checked on the parent document."""
	if not name or not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} not found").format(_(doctype), name), frappe.DoesNotExistError)
	doc = frappe.get_doc(doctype, name)
	doc.check_permission(ptype)
	return doc


def check_modified(doc, modified):
	"""FR-08, for writes that reload the document instead of saving the
	client's copy (workflow actions, controller wrappers)."""
	if not modified:
		frappe.throw(_("modified is required"))
	if cstr(doc.modified) != cstr(modified):
		frappe.throw(_(STALE_MESSAGE), frappe.TimestampMismatchError)


def prepare_save(doc, modified):
	"""FR-08, for writes that save the loaded document: core's own
	check_if_latest compares against the value set here."""
	check_modified(doc, modified)
	doc.modified = modified


def require_draft(doc):
	"""Edits are allowed in workflow state Draft only. Every Pending state is
	docstatus 0 as well, so docstatus alone can't tell them apart."""
	if doc.docstatus != 0 or (doc.get("workflow_state") or "") not in DRAFT_STATES:
		frappe.throw(
			_("{0} {1} is {2} and can only be edited in Draft.").format(
				_(doc.doctype), doc.name, doc.get("workflow_state") or doc.docstatus
			)
		)


def positive(value, label, allow_zero=False):
	try:
		number = float(value)
	except (TypeError, ValueError):
		frappe.throw(_("{0} must be a number.").format(label))
	if number < 0 or (number == 0 and not allow_zero):
		frappe.throw(_("{0} must be greater than {1}.").format(label, "or equal to 0" if allow_zero else "0"))
	return number


def rows_by_name(doc, table, entries, allowed_fields, label):
	"""Resolve [{row_name, <field>: value}, ...] against doc's own table.
	Everything is checked before anything is applied, so a bad entry leaves
	the document untouched."""
	entries = parse(entries, []) or []
	rows = {row.name: row for row in doc.get(table)}
	resolved = []
	for entry in entries:
		row = rows.get(entry.get("row_name"))
		if not row:
			frappe.throw(_("{0}: row {1} is not a row of {2}.").format(label, entry.get("row_name"), doc.name))
		extra = set(entry) - {"row_name"} - set(allowed_fields)
		if extra:
			frappe.throw(_("{0}: field(s) {1} cannot be changed here.").format(label, ", ".join(sorted(extra))))
		resolved.append((row, entry))
	return resolved


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def strip(doc, drop_tables=()):
	"""FR-05: the document as a dict, without any field the current user
	can't read at its permlevel - on the parent and every child row - however
	System Settings "Apply Perm Level on API Calls" is set. Keys are removed,
	not nulled, so the raw response never even names a hidden field."""
	data = doc.as_dict(convert_dates_to_str=True)
	for table in drop_tables:
		data.pop(table, None)

	if frappe.session.user == "Administrator":
		return data
	access = set(doc.get_permlevel_access("read"))

	for df in doc.meta.fields:
		if df.permlevel and df.permlevel not in access:
			data.pop(df.fieldname, None)
	for table_df in doc.meta.get_table_fields():
		hidden = [
			df.fieldname for df in frappe.get_meta(table_df.options).fields
			if df.permlevel and df.permlevel not in access
		]
		for row in data.get(table_df.fieldname) or []:
			for fieldname in hidden:
				row.pop(fieldname, None)
	return data


def allowed_actions(doc):
	if doc.is_new() or not get_workflow_name(doc.doctype):
		return []
	return [{"action": t.action, "next_state": t.next_state} for t in get_transitions(doc)]


def can(doc, **extra):
	result = {
		"read": bool(doc.has_permission("read")),
		"write": bool(doc.has_permission("write")),
		"create": bool(frappe.has_permission(doc.doctype, "create")),
		"submit": bool(doc.meta.is_submittable and doc.has_permission("submit")),
	}
	result.update(extra)
	return result


def view(doc, data=None, drop_tables=(), extra_can=None, extra_meta=None):
	"""FR-09 envelope."""
	meta = {
		"doctype": doc.doctype,
		"name": doc.name,
		"modified": cstr(doc.modified),
		"docstatus": doc.docstatus,
		"workflow_state": doc.get("workflow_state"),
		"allowed_actions": allowed_actions(doc),
		"can": can(doc, **(extra_can or {})),
	}
	meta.update(extra_meta or {})
	return {"data": data if data is not None else strip(doc, drop_tables), "meta": meta}


def list_view(doctype, fields, filters=None, search=None, search_fields=("name",), limit=None, start=None,
			  order_by="modified desc"):
	"""FR-10: frappe.get_list, so only records the user can read come back."""
	limit = min(max(cint(limit) or DEFAULT_LIMIT, 1), MAX_LIMIT)
	start = max(cint(start), 0)
	or_filters = None
	if search:
		or_filters = [[doctype, field, "like", f"%{search}%"] for field in search_fields]
	rows = frappe.get_list(
		doctype, fields=list(fields), filters=parse(filters, {}) or {}, or_filters=or_filters,
		limit_start=start, limit_page_length=limit, order_by=order_by,
	)
	return {"data": rows, "meta": {"doctype": doctype, "start": start, "limit": limit, "count": len(rows)}}


# ---------------------------------------------------------------------------
# Workflow actions
# ---------------------------------------------------------------------------

def find_action(doc, action):
	for transition in allowed_actions(doc):
		if transition["action"] == action:
			return transition
	frappe.throw(_("Action not allowed for you at this stage."), frappe.PermissionError)


def preview_action(doc, action, **extra):
	transition = find_action(doc, action)
	return {
		"doctype": doc.doctype, "name": doc.name, "modified": cstr(doc.modified),
		"action": action, "current_state": doc.get("workflow_state"), "next_state": transition["next_state"],
		**extra,
	}


def run_action(doc, modified, action):
	"""FR-23/28: only an action get_transitions offers this user right now."""
	check_modified(doc, modified)
	find_action(doc, action)
	apply_workflow(doc, action)
	return frappe.get_doc(doc.doctype, doc.name)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def attach(doctype, name, fieldname, modified, filename, content):
	"""Upload a file into one of ATTACH_FIELDS. content is base64."""
	if fieldname not in ATTACH_FIELDS.get(doctype, ()):
		frappe.throw(_("{0} cannot be attached here.").format(fieldname), frappe.PermissionError)
	doc = load(doctype, name, "write")
	prepare_save(doc, modified)
	file_doc = insert_file(doctype, name, filename, content, fieldname)
	doc.set(fieldname, file_doc.file_url)
	doc.save()
	return doc


def attach_unfielded(doctype, name, modified, filename, content):
	"""Attach a file to a document that has no Attach field for it (shows in
	the form's attachments sidebar). Needs write permission on the document."""
	doc = load(doctype, name, "write")
	check_modified(doc, modified)
	insert_file(doctype, name, filename, content)
	return frappe.get_doc(doctype, name)


def insert_file(doctype, name, filename, content, fieldname=None):
	if not filename or not content:
		frappe.throw(_("filename and content are required"))
	try:
		data = base64.b64decode(content, validate=True)
	except (ValueError, TypeError):
		frappe.throw(_("content must be base64-encoded"))
	if len(data) > MAX_ATTACHMENT_BYTES:
		frappe.throw(_("File is larger than {0} MB.").format(MAX_ATTACHMENT_BYTES // (1024 * 1024)))

	return frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"attached_to_doctype": doctype,
		"attached_to_name": name,
		"attached_to_field": fieldname,
		"is_private": 1,
		"content": data,
	}).insert()


def as_float(value):
	return flt(value)
