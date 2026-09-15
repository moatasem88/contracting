from frappe import _

# Discovered by naming convention (get_dashboard_data()'s first branch,
# load_doctype_module(self.name, suffix="_dashboard")) - no hooks.py wiring
# needed, since this doctype is owned by this same app. Contractor Invoice
# and Purchase Invoice already get a Connections entry for free from their
# own direct header Link fields (add_subcontractor_contract_connections.py's
# DocType Link rows); Payment Entry has none - it only carries a Payment
# Entry Reference child row (reference_doctype/reference_name) pointing back
# here - so its connection has to go through non_standard_fieldnames instead,
# the same way core's own purchase_order_dashboard.py resolves Purchase
# Order's Payment Entry connection.


def get_data():
	return {
		"fieldname": "subcontractor_contract",
		"non_standard_fieldnames": {"Payment Entry": "reference_name"},
		"transactions": [
			{"label": _("Payment"), "items": ["Payment Entry"]},
		],
	}
