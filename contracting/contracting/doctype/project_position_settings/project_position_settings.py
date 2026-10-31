# Copyright (c) 2026, kazem and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ProjectPositionSettings(Document):
	def validate(self):
		self.validate_guarantee_accounts()

	def validate_guarantee_accounts(self):
		"""Reject duplicate, group and other-company accounts in one message,
		so accountants fix the whole list in one pass."""
		errors = []
		seen = set()
		for row in self.guarantee_accounts:
			if row.account in seen:
				errors.append(_("Row {0}: {1} is listed twice").format(row.idx, row.account))
				continue
			seen.add(row.account)

			is_group, company = frappe.db.get_value("Account", row.account, ["is_group", "company"]) or (0, None)
			if is_group:
				errors.append(_("Row {0}: {1} is a group account").format(row.idx, row.account))
			if company != self.company:
				errors.append(_("Row {0}: {1} belongs to another company").format(row.idx, row.account))

		if errors:
			frappe.throw("<br>".join(errors), title=_("Invalid Guarantee Accounts"))
