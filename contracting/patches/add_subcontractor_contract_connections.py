import frappe

# Surface the Contractor Invoices and Purchase Invoices raised against a
# Subcontractor Contract under its own Connections tab - same mechanism
# add_purchase_invoice_contractor_invoice_link.py already uses for the
# Purchase Invoice -> Contractor Invoice connection (a custom DocType Link
# row, the same thing Customize Form's own Connections editor creates for a
# plain header Link field). Payment Entry isn't included here: it has no
# direct header field to Subcontractor Contract (only a Payment Entry
# Reference child row), which a DocType Link can't express - that one is
# wired separately via override_doctype_dashboards.
TARGET_DOCTYPE = "Subcontractor Contract"
LINKS = (
	("Contractor Invoice", "contractor_contract"),
	("Purchase Invoice", "subcontractor_contract"),
)


def execute():
	for link_doctype, link_fieldname in LINKS:
		if frappe.db.exists(
			"DocType Link", {"parent": TARGET_DOCTYPE, "link_doctype": link_doctype, "custom": 1}
		):
			continue

		frappe.get_doc({
			"doctype": "DocType Link",
			"parent": TARGET_DOCTYPE,
			"parenttype": "DocType",
			"parentfield": "links",
			"link_doctype": link_doctype,
			"link_fieldname": link_fieldname,
			"group": "Contracting",
			"custom": 1,
		}).insert(ignore_permissions=True)

	frappe.clear_cache(doctype=TARGET_DOCTYPE)
