import frappe
from frappe.model.document import Document


class ClientProgressInvoiceSalesOrder(Document):
	# Not to be confused with Project Sales Order Detail - that one is
	# Won-automation output attached to Project; this one is a
	# Client-Progress-Invoice-scoped, system-managed picker for this claim.
	pass
