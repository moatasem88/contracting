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
		{
			"fieldname": "custom_contractor_invoice",
			"fieldtype": "Link",
			"options": "Contractor Invoice",
			"label": "Contractor Invoice",
			"insert_after": "project",
			"read_only": 1,
			"description": "The Contractor Invoice this Purchase Invoice was generated from, when applicable. Set by create_purchase_invoice() at generation time.",
		},
		{
			"fieldname": "subcontractor_contract",
			"fieldtype": "Link",
			"options": "Subcontractor Contract",
			"label": "Subcontractor Contract",
			"insert_after": "custom_contractor_invoice",
			"read_only": 1,
			"no_copy": 1,
			"description": "The Subcontractor Contract this Purchase Invoice traces back to, via its source Contractor Invoice. Set by create_purchase_invoice() at generation time - a direct field so it can show under the contract's own Connections, which can't reach a two-hop relation.",
		},
		{
			"fieldname": "custom_invoice_attachment",
			"fieldtype": "Attach",
			"label": "Subcontractor Invoice Attachment",
			"insert_after": "custom_contractor_invoice",
			"read_only": 1,
			"description": "Independent copy of the source Contractor Invoice's own Invoice Attachment, when it had one.",
		},
		{
			"fieldname": "custom_checklist_attachment",
			"fieldtype": "Attach",
			"label": "Cost Control Checklist",
			"insert_after": "custom_invoice_attachment",
			"read_only": 1,
			"description": "Independent copy of the source Contractor Invoice's own Cost Control Checklist, when it had one.",
		},
	],
	"Purchase Invoice Item": _work_item_field("item_code") + _deduction_item_fields(
		"custom_project_work_item", read_only=1
	) + [
		{
			"fieldname": "custom_contract_item",
			"fieldtype": "Link",
			"options": "Contractor Contract Item",
			"label": "Contract Item",
			"insert_after": "custom_project_work_item",
			"read_only": 1,
			"description": "The exact Contractor Contract Item line this PI row was generated from, when applicable - finer-grained than custom_project_work_item (which points at the coarser Project BOQ Item and can't disambiguate between multiple contract lines/contracts sharing one BOQ work item). Feeds Contractor Contract Item's invoiced_percentage/paid_percentage. Set by create_purchase_invoice() at generation time.",
		},
		{
			"fieldname": "custom_payment_condition",
			"fieldtype": "Data",
			"label": "Payment Condition",
			"insert_after": "rate",
			"read_only": 1,
			"description": "The Payment Condition this line was billed against (e.g. 'on supply'), when the source contract billed per condition. Blank for a plain-contract-sourced row. Free text, not a Link - same reasoning as Contractor Invoice Condition Progress's own condition_label.",
		},
		{
			"fieldname": "custom_payout_rate",
			"fieldtype": "Percent",
			"label": "Payout Rate",
			"insert_after": "custom_payment_condition",
			"read_only": 1,
			"description": "The payment-condition percent this line's rate was scaled by (100 for a plain-contract-sourced row). System-set only.",
		},
	],
	"Sales Taxes and Charges": [
		{
			"fieldname": "custom_tax_charge_type",
			"fieldtype": "Link",
			"options": "Tax and Charge Type",
			"label": "Tax / Charge Type",
			"insert_after": "charge_type",
		},
	],
	"Purchase Taxes and Charges": [
		{
			"fieldname": "custom_tax_charge_type",
			"fieldtype": "Link",
			"options": "Tax and Charge Type",
			"label": "Tax / Charge Type",
			"insert_after": "charge_type",
		},
	],
	# Added via Customize Form in the UI, backfilled here to bring them
	# under version control (same reasoning as the module docstring).
	# custom_unit_'s trailing underscore (and its label's trailing space) on
	# Template Material Item V2 is a live typo, kept verbatim rather than
	# "fixed" - update=True can't rename a fieldname, so cleaning it up
	# would require a separate migration to retire the old field.
	"Template Material Item V2": [
		{
			"fieldname": "custom_item_name",
			"fieldtype": "Data",
			"label": "Item Name",
			"insert_after": "item",
		},
		{
			"fieldname": "custom_unit_",
			"fieldtype": "Data",
			"label": "Unit ",
			"insert_after": "amount",
		},
	],
	"Template Labor Item V2": [
		{
			"fieldname": "custom_item_name",
			"fieldtype": "Data",
			"label": "Item Name",
			"insert_after": "item",
		},
		{
			"fieldname": "custom_unit",
			"fieldtype": "Data",
			"label": "Unit",
			"insert_after": "amount",
		},
	],
	"Template Equipment Item V2": [
		{
			"fieldname": "custom_item_name",
			"fieldtype": "Data",
			"label": "Item Name",
			"insert_after": "item",
		},
		{
			"fieldname": "custom_unit",
			"fieldtype": "Data",
			"label": "Unit",
			"insert_after": "amount",
		},
	],
}


def apply_custom_fields():
	# Imported lazily so hooks.py can pull custom_field_names() without
	# dragging frappe's custom-field module in at hook-load time.
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(CUSTOM_FIELDS, update=True)


# Historical fetch_from wiring for the Link above - REMOVED. Frappe's Link
# control clears every fetch_from target the instant it parses an empty
# value (control/link.js validate_link_and_fetch(): the `else` branch calls
# update_dependant_fields({}), which blanks every mapped field via
# frappe.model.set_value(..., "")) - not only on a real edit but on any
# re-parse of the row (confirmed: opening a grid row's detail panel re-parses
# every control against its current value). Since custom_tax_charge_type is
# an optional convenience field left blank on most rows, this wiring
# silently wiped the mandatory native `description` (and charge_type/
# account_head/rate/...) back to blank well after the user had typed them,
# surfacing only at save as "Value missing for: Description". Replaced by an
# explicit custom_tax_charge_type change-handler (public/js/tax_charge_type.js)
# that copies defaults only on a genuine selection and never touches these
# fields when the link is blank - the same "explicit, not fetch_from" fix
# already used for Contractor Invoice Condition Progress.condition_label.
PROPERTY_SETTERS = [
	("Sales Taxes and Charges", "charge_type"),
	("Sales Taxes and Charges", "account_head"),
	("Sales Taxes and Charges", "description"),
	("Sales Taxes and Charges", "rate"),
	("Sales Taxes and Charges", "included_in_print_rate"),
	("Purchase Taxes and Charges", "charge_type"),
	("Purchase Taxes and Charges", "account_head"),
	("Purchase Taxes and Charges", "description"),
	("Purchase Taxes and Charges", "rate"),
	("Purchase Taxes and Charges", "included_in_print_rate"),
	("Purchase Taxes and Charges", "add_deduct_tax"),
]


def apply_tax_charge_type_fetch_setters():
	"""Retract the fetch_from Property Setters above wherever a past
	migrate already created them - see the comment on PROPERTY_SETTERS.
	Idempotent: delete_property_setter() no-ops when none exists."""
	from frappe.custom.doctype.property_setter.property_setter import delete_property_setter

	for doctype, fieldname in PROPERTY_SETTERS:
		delete_property_setter(doctype, "fetch_from", fieldname)


def custom_field_names():
	"""`dt-fieldname` names, for the hooks.py fixtures filter."""
	names = []
	for doctype, fields in CUSTOM_FIELDS.items():
		for field in fields:
			names.append("{0}-{1}".format(doctype, field["fieldname"]))
	return sorted(names)
