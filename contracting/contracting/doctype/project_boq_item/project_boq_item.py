# Copyright (c) 2026, kazem and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils.nestedset import NestedSet

class ProjectBOQItem(NestedSet):
	def validate(self):
		if not self.is_group and not self.boq_sourceitem and not self.source_tender_boq_item:
			frappe.throw(_("Set either Source Template Item or Source Tender BOQ Item."))
