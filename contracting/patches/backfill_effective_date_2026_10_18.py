import json

import frappe
from frappe.utils import getdate

# 2026-10-18 BRD FR-08: contracts/addenda already Approved before
# effective_date existed get it backfilled once - the date workflow_state
# first changed to Approved (from Version history), else `modified`. Only
# fills empty values, so it is safe to re-run.

DOCTYPES = ("Subcontractor Contract", "Contractor Contract Addendum")


def execute():
	for doctype in DOCTYPES:
		if not frappe.get_meta(doctype).has_field("effective_date"):
			print("skipped {0}: no effective_date field yet (run bench migrate first)".format(doctype))
			continue

		for row in frappe.get_all(
			doctype, filters={"docstatus": 1, "effective_date": ["is", "not set"]}, fields=["name", "modified"]
		):
			date, source = approved_on(doctype, row.name), "Version"
			if not date:
				date, source = getdate(row.modified), "modified (fallback)"
			frappe.db.set_value(doctype, row.name, "effective_date", date, update_modified=False)
			print("{0} {1}: {2} from {3}".format(doctype, row.name, date, source))
	frappe.db.commit()


def approved_on(doctype, name):
	versions = frappe.get_all(
		"Version", filters={"ref_doctype": doctype, "docname": name},
		fields=["data", "creation"], order_by="creation asc",
	)
	for v in versions:
		try:
			changed = json.loads(v.data or "{}").get("changed", [])
		except ValueError:
			continue
		for fieldname, _old, new in changed:
			if fieldname == "workflow_state" and new == "Approved":
				return getdate(v.creation)
	return None
