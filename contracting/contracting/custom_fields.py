"""Custom fields this app adds to native doctypes.

Applied by `install_app_requirements` (the after_migrate hook), so the set
is re-asserted on every migrate rather than only once at patch time.
`create_custom_fields(..., update=True)` is idempotent.

Everything defined here is also listed in hooks.py `fixtures`, so a fresh
site rebuilds it from source control.

Two conventions matter for the subcontractor-deduction fields:

* The fieldnames are identical on Material Request Item, Purchase Order
  Item, Purchase Receipt Item and Purchase Invoice Item, and none of them
  sets `no_copy`. That is what makes the tag propagate through ERPNext's
  standard Create chain for free - frappe/model/mapper.py:map_fields
  copies every field present on both source and target that isn't no_copy.
* The flag exists at both header and row level. The header is where it is
  normally set (one request, one subcontractor) and cascades down; the row
  values are what actually drive the deduction, so a single document can
  still mix subcontractor and company-use lines.
"""

DEDUCTION_CHECK = "custom_is_subcontractor_deduction"
DEDUCTION_SUPPLIER = "custom_deduction_subcontractor"

SUBCONTRACTOR_LINK_FILTER = '{"custom_is_subcontractor": 1}'


def _deduction_header_fields(insert_after, read_only=0):
	"""Header-level tag. Set on Material Request; carried (read-only) downstream."""
	return [
		{
			"fieldname": "custom_subcontractor_deduction_section",
			"fieldtype": "Section Break",
			"label": "Subcontractor Deduction",
			"insert_after": insert_after,
			"collapsible": 1,
		},
		{
			"fieldname": DEDUCTION_CHECK,
			"fieldtype": "Check",
			"label": "Procured on Subcontractor's Behalf",
			"insert_after": "custom_subcontractor_deduction_section",
			"read_only": read_only,
			"description": "Marks this document's material as bought for a subcontractor, to be recovered from their account. Cascades to the item rows, which can still differ individually.",
		},
		{
			"fieldname": DEDUCTION_SUPPLIER,
			"fieldtype": "Link",
			"options": "Supplier",
			"label": "Deduction Subcontractor",
			"insert_after": DEDUCTION_CHECK,
			"read_only": read_only,
			"depends_on": "eval:doc.%s" % DEDUCTION_CHECK,
			"mandatory_depends_on": "eval:doc.%s" % DEDUCTION_CHECK,
			"link_filters": SUBCONTRACTOR_LINK_FILTER,
		},
	]


def _deduction_item_fields(insert_after, read_only=0):
	"""Row-level tag - what FR-22's per-line deduction actually reads."""
	return [
		{
			"fieldname": DEDUCTION_CHECK,
			"fieldtype": "Check",
			"label": "Subcontractor Deduction",
			"insert_after": insert_after,
			"read_only": read_only,
		},
		{
			"fieldname": DEDUCTION_SUPPLIER,
			"fieldtype": "Link",
			"options": "Supplier",
			"label": "Deduction Subcontractor",
			"insert_after": DEDUCTION_CHECK,
			"read_only": read_only,
			"depends_on": "eval:doc.%s" % DEDUCTION_CHECK,
			"mandatory_depends_on": "eval:doc.%s" % DEDUCTION_CHECK,
			"link_filters": SUBCONTRACTOR_LINK_FILTER,
		},
	]


def _work_item_field(insert_after):
	"""Same fieldname as Material Request Item.custom_project_work_item, so
	Frappe's map_fields (frappe/model/mapper.py) copies it automatically
	through Create From chains (Material Request -> PO -> PR -> PI), the
	same way DEDUCTION_CHECK/DEDUCTION_SUPPLIER already propagate. Lets the
	Project Cost & Billing rollup (contracting.contracting.utils.
	project_cost_billing) trace a purchase line back to the Tender it was
	bought for, via Project BOQ Item.source_tender_boq_item."""
	return [
		{
			"fieldname": "custom_project_work_item",
			"fieldtype": "Link",
			"options": "Project BOQ Item",
			"label": "Work Item",
			"insert_after": insert_after,
			"read_only": 1,
		},
	]


CUSTOM_FIELDS = {
	"Supplier": [
		{
			"fieldname": "custom_is_subcontractor",
			"fieldtype": "Check",
			"label": "Is Subcontractor",
			"insert_after": "is_transporter",
			"description": "Subcontractor Contract only offers suppliers with this set.",
		},
	],
	"Material Request": [
		{
			"fieldname": "custom_is_general_material",
			"fieldtype": "Check",
			"label": "General / Non-Site Material",
			"insert_after": "project",
			"description": "Set for requests that aren't site work - hospitality, office supplies and the like. Lifts the work-item restriction so any coded stock item can be requested.",
		},
		{
			"fieldname": "custom_work_items",
			"fieldtype": "Table",
			"options": "Material Request Work Item",
			"label": "Work Items",
			"insert_after": "custom_is_general_material",
			"depends_on": "eval:!doc.custom_is_general_material",
			"description": "Which project work items this request draws against. Items are then restricted to the materials budgeted under them.",
		},
	] + _deduction_header_fields("custom_work_items"),
	"Material Request Item": [
		{
			"fieldname": "custom_project_work_item",
			"fieldtype": "Link",
			"options": "Project BOQ Item",
			"label": "Work Item",
			"insert_after": "item_code",
			"description": "Work item this line draws against. Filled automatically when the material belongs to only one of the selected work items.",
		},
	] + _deduction_item_fields("custom_project_work_item"),
	"Purchase Order": _deduction_header_fields("supplier", read_only=1),
	"Purchase Order Item": _work_item_field("item_code") + _deduction_item_fields(
		"custom_project_work_item", read_only=1
	),
	"Purchase Receipt": _deduction_header_fields("supplier", read_only=1),
	"Purchase Receipt Item": _work_item_field("item_code") + _deduction_item_fields(
		"custom_project_work_item", read_only=1
	),
	"Purchase Invoice": _deduction_header_fields("supplier", read_only=1) + [
		{
			"fieldname": "custom_deduction_gl_account",
			"fieldtype": "Link",
			"options": "Account",
			"label": "Deduction GL Account",
			"insert_after": DEDUCTION_SUPPLIER,
			"depends_on": "eval:doc.%s" % DEDUCTION_CHECK,
			"description": "Ledger account the subcontractor deduction posts against. Chosen per invoice by the accountant; required once any line is flagged.",
		},
	],
	"Purchase Invoice Item": _work_item_field("item_code") + _deduction_item_fields(
		"custom_project_work_item", read_only=1
	),
}


def apply_custom_fields():
	# Imported lazily so hooks.py can pull custom_field_names() without
	# dragging frappe's custom-field module in at hook-load time.
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(CUSTOM_FIELDS, update=True)


def custom_field_names():
	"""`dt-fieldname` names, for the hooks.py fixtures filter."""
	names = []
	for doctype, fields in CUSTOM_FIELDS.items():
		for field in fields:
			names.append("{0}-{1}".format(doctype, field["fieldname"]))
	return sorted(names)
