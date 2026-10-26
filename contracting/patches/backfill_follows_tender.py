import frappe

from contracting.contracting.doctype.subcontractor_contract.subcontractor_contract import (
	EDITABLE_STATES,
	get_project_tenders,
)

RESOURCE_TABLES = {
	"Contractor Contract Material Item": ("material_item", "Tender Material Item"),
	"Contractor Contract Labor Item": ("labor_item", "Tender Labor Item"),
	"Contractor Contract Equipment Item": ("equipment_item", "Tender Equipment Item"),
}


def execute():
	"""Follows Tender BRD (2026-10-13) backfill for contracts that predate it.

	Every existing contract is a Tender contract: every line has a work_item
	and every resource row a Tender resource link. Written through frappe.db
	only - no .save() - so neither the workflow nor set_ceo_approval_flag
	fires, and no contract's routing changes (TC-25).

	follows_tender / out_of_tender_scope already got their column defaults
	from migrate; what's left is the resource rows' source, and the Tender
	reference figures on contracts still in Draft (frozen ones are left
	alone, NFR-02). Idempotent: rerunning finds nothing left to fill.

	Contracts whose project no longer resolves to any Tender are reported,
	not touched (OQ-1): their next save will ask for the lines to be removed
	before the mode can change.
	"""
	for doctype, (link_fieldname, tender_doctype) in RESOURCE_TABLES.items():
		frappe.db.sql(
			f"""
			update `tab{doctype}`
			set source = 'Tender'
			where ifnull(source, '') = '' and ifnull({link_fieldname}, '') != ''
			"""
		)
		# The grid's single Item column labels every row; Tender rows get it
		# from their Tender resource row (validate() does the same on save).
		frappe.db.sql(
			f"""
			update `tab{doctype}` row
			inner join `tab{tender_doctype}` res on res.name = row.{link_fieldname}
			set row.item = res.item
			where ifnull(row.item, '') = ''
			"""
		)

	frappe.db.sql(
		"""
		update `tabContractor Contract Item`
		set follows_tender = 1
		where ifnull(work_item, '') != '' and follows_tender = 0
		"""
	)

	stuck = []
	for name, project, workflow_state, department in frappe.db.sql(
		"""select name, project, workflow_state, department
		from `tabSubcontractor Contract` where docstatus = 0""",
	):
		if not get_project_tenders(project, department):
			# A no-Tender contract is fine on its own (template lines); it is
			# only stuck when it still holds Tender lines from before.
			if frappe.db.exists("Contractor Contract Item", {"parent": name, "work_item": ["is", "set"]}):
				stuck.append(name)
			continue
		if (workflow_state or "") not in EDITABLE_STATES:
			continue

		doc = frappe.get_doc("Subcontractor Contract", name)
		doc.compute_tender_reference()
		doc.compute_out_of_tender_scope()
		for row in doc.contracted_items:
			row.db_set("tender_unit_cost", row.tender_unit_cost, update_modified=False)
			row.db_set("out_of_tender_scope", row.out_of_tender_scope, update_modified=False)
		for fieldname in ("contract_material_items", "contract_labor_items", "contract_equipment_items"):
			for row in doc.get(fieldname):
				row.db_set("tender_rate", row.tender_rate, update_modified=False)
				row.db_set("out_of_tender_scope", row.out_of_tender_scope, update_modified=False)
		doc.db_set("has_out_of_tender_items", doc.has_out_of_tender_items, update_modified=False)

	frappe.db.commit()

	if stuck:
		print(
			"Follows Tender backfill: these Draft contracts' projects no longer resolve to a Tender "
			"and were left unchanged: " + ", ".join(stuck)
		)
