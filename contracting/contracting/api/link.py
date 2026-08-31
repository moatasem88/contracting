import frappe
from frappe import _
from frappe.client import validate_link as _core_validate_link


@frappe.whitelist()
def validate_link(doctype: str, docname: str, fields=None):
	"""Core's validate_link cannot pass a child DocType.

	It calls has_permission(doctype, "select") with no parent context, and
	has_child_permission() always fails without one - so a Link field
	pointing at a child row (Tender BOQ Item, Tender Labor Item, ...) can
	never be set from the desk. Child tables also can't be given their own
	DocPerms (the Role Permissions Manager filters istable=0), so there is
	no configuration-side fix.

	Resolve the parent from the row itself, the way frappe.client.get_value
	already does, and let core decide from there.
	"""
	if isinstance(doctype, str) and isinstance(docname, str) and frappe.is_table(doctype):
		if _may_select_child_row(doctype, docname):
			values = frappe._dict(name=frappe.db.get_value(doctype, docname, cache=True))
			fields = frappe.parse_json(fields)
			if values.name and fields:
				values.update(frappe.db.get_value(doctype, docname, fields, as_dict=True) or {})
			return values

	return _core_validate_link(doctype, docname, fields)


def _may_select_child_row(doctype: str, docname: str) -> bool:
	# doc=<name> is what makes this work: has_child_permission resolves
	# parenttype/parentfield off the row and delegates to the parent's
	# DocPerms. throw defaults to False, so a plain "no" returns quietly
	# instead of msgprinting the internal permission check log.
	#
	# "select" or "read" mirrors core validate_link's own condition.
	return any(
		frappe.has_permission(doctype, ptype, doc=docname) for ptype in ("select", "read")
	)


# Columns worth putting in front of the user in a Link dropdown. Currency /
# Float / Check say nothing useful about which row you are picking, and
# Text Editor would render its raw HTML into the suggestion list.
DISPLAY_FIELDTYPES = ("Data", "Link", "Select", "Small Text", "Text", "Read Only")

# name plus this many descriptive columns. Enough to tell two rows apart
# without turning every suggestion into a paragraph.
MAX_DISPLAY_FIELDS = 3


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def child_row_query(doctype, txt, searchfield, start, page_len, filters, as_dict=False):
	"""standard_queries handler for Link fields whose target is a child DocType.

	The dropdown behind such a field is as broken as validate_link was, for
	the same underlying reason but on a different code path:
	frappe.desk.search.search_widget calls frappe.get_list() without a
	parent_doctype, and DatabaseQuery._set_permission_map then falls back to
	`parent_doctype or self.doctype` - i.e. it asks whether Tender Item is a
	valid parent of Tender Item. It never is (parent_meta.istable), so every
	non-Administrator gets "Not permitted" the moment the picker opens.

	Registering this as a standard query takes the search off that path
	entirely (search_widget's "by method" branch does no permission check of
	its own) and lets us supply the parent_doctype core is missing.

	Permission therefore derives from the parent DocTypes, matching how
	_may_select_child_row above already grants on individual rows: you can
	search the rows that live under a document you may read, and the
	parenttype filter keeps the result set to exactly those.
	"""
	parenttypes = _permitted_parenttypes(doctype)
	if not parenttypes:
		frappe.throw(
			_("Not permitted to select {0}. It requires read access on {1}.").format(
				frappe.bold(_(doctype)),
				", ".join(_(pt) for pt in _parenttypes(doctype)) or _("its parent document"),
			),
			frappe.PermissionError,
		)

	meta = frappe.get_meta(doctype)
	fields = _display_fields(meta, searchfield)

	filters = _as_filter_list(filters)
	filters.append(["parenttype", "in", parenttypes])

	or_filters = [[fieldname, "like", f"%{txt}%"] for fieldname in fields] if txt else []

	return frappe.get_list(
		doctype,
		filters=filters,
		or_filters=or_filters,
		fields=[*fields, "parent"],
		# The whole point of this function: core never passes this, and
		# without it the permission check below the query self-destructs.
		# Any entry in parenttypes works - they are all readable, and the
		# filter above confines the rows to that same set.
		parent_doctype=parenttypes[0],
		limit_start=start,
		limit_page_length=page_len,
		order_by="parent asc, idx asc",
		as_list=not as_dict,
		strict=False,
	)


def _parenttypes(doctype: str) -> list[str]:
	"""Every DocType that holds this child table, standard or customised."""
	table_fieldtypes = ("Table", "Table MultiSelect")
	parents = frappe.get_all(
		"DocField",
		filters={"fieldtype": ("in", table_fieldtypes), "options": doctype},
		pluck="parent",
	)
	parents += frappe.get_all(
		"Custom Field",
		filters={"fieldtype": ("in", table_fieldtypes), "options": doctype},
		pluck="dt",
	)
	return list(dict.fromkeys(parents))


def _permitted_parenttypes(doctype: str) -> list[str]:
	return [
		parenttype
		for parenttype in _parenttypes(doctype)
		if any(frappe.has_permission(parenttype, ptype) for ptype in ("select", "read"))
	]


def _display_fields(meta, searchfield) -> list[str]:
	"""name first - it is the value the Link field stores - then whatever
	identifies the row to a human: the title field, the declared search
	fields, and the textual columns already shown in the grid."""
	fields = ["name"]
	candidates = [searchfield, meta.title_field, *meta.get_search_fields()]
	candidates += [df.fieldname for df in meta.fields if df.in_list_view]

	for fieldname in candidates:
		if len(fields) > MAX_DISPLAY_FIELDS:
			break
		df = meta.get_field(fieldname) if fieldname else None
		if not df or df.fieldtype not in DISPLAY_FIELDTYPES or df.fieldname in fields:
			continue
		# A Link to another child row shows the same opaque hash we are
		# already trying to make sense of - Contractor Contract Item.work_item
		# would put a Tender BOQ Item id in the suggestion line.
		if df.fieldtype == "Link" and df.options and frappe.is_table(df.options):
			continue
		fields.append(df.fieldname)

	return fields


def _as_filter_list(filters) -> list:
	"""Whatever a get_query gave us, as a list db_query can extend."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)

	if not filters:
		return []

	if isinstance(filters, dict):
		return [
			[fieldname, *(value if isinstance(value, list | tuple) else ["=", value])]
			for fieldname, value in filters.items()
		]

	return [list(f) for f in filters]
