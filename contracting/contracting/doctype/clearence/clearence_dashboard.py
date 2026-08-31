from frappe import _


def get_data(*args,**Kwargs):
	return {
		"heatmap": True,
		"heatmap_message": _("Dashboard"),
		"fieldname": "reference_name",
		"transactions": [
		
			{"label": _("Accounts"), "items": ["Journal Entry","Payment Entry"]},
		
		],
	}
