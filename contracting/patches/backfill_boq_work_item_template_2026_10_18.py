import frappe

# 2026-10-18 BRD FR-05: boq_work_item_template (fetch_from
# work_item.work_item_template) is filled on save, but existing contracts are
# only re-saved when someone opens them; this fills the current rows once.
# Frozen (Approved) contracts can't be re-saved, hence db.set_value.


def execute():
	if not frappe.get_meta("Contractor Contract Item").has_field("boq_work_item_template"):
		print("skipped: boq_work_item_template not on the doctype yet (run bench migrate first)")
		return

	rows = frappe.db.sql(
		"""select cci.name, boq.work_item_template
		from `tabContractor Contract Item` cci
		join `tabTender BOQ Item` boq on boq.name = cci.work_item
		where ifnull(boq.work_item_template, '') != ''
		  and ifnull(cci.boq_work_item_template, '') != boq.work_item_template""",
		as_dict=True,
	)
	for row in rows:
		frappe.db.set_value(
			"Contractor Contract Item", row.name, "boq_work_item_template", row.work_item_template,
			update_modified=False,
		)
	frappe.db.commit()
	print("filled boq_work_item_template on {0} row(s)".format(len(rows)))
