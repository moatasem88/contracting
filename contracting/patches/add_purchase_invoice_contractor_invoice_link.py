import frappe

# FR-18: surface the Contractor Invoice that generated a Purchase Invoice
# under the PI's own Connections tab. Built as a custom DocType Link row -
# the same mechanism Customize Form's own "Connections" editor creates for a
# plain header Link field (see frappe/custom/doctype/customize_form/
# customize_form.py:set_property_setters_for_actions_and_links) - rather
# than hooks.py's override_doctype_dashboards, which is unused on this bench
# today and whose internal_links mechanism is for child-table rollups, not a
# plain header Link like custom_contractor_invoice.
LINK_DOCTYPE = "Contractor Invoice"
LINK_FIELDNAME = "custom_contractor_invoice"
TARGET_DOCTYPE = "Purchase Invoice"


def execute():
	if frappe.db.exists(
		"DocType Link", {"parent": TARGET_DOCTYPE, "link_doctype": LINK_DOCTYPE, "custom": 1}
	):
		return

	frappe.get_doc({
		"doctype": "DocType Link",
		"parent": TARGET_DOCTYPE,
		"parenttype": "DocType",
		"parentfield": "links",
		"link_doctype": LINK_DOCTYPE,
		"link_fieldname": LINK_FIELDNAME,
		"group": "Contracting",
		"custom": 1,
	}).insert(ignore_permissions=True)
	frappe.clear_cache(doctype=TARGET_DOCTYPE)
