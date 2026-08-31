import frappe
from frappe.utils import flt


def sync_tender_to_project_tenders(tender_doc):
	"""Tender -> Project Tender push (Part A). Writes tender_total via
	frappe.db.set_value (no event chain), then recomputes+saves each
	referencing Project Tender with skip_cross_sync so *that* save's own
	on_update doesn't re-enter here, and explicitly pushes back down to
	every other linked Tender (excluding the one that triggered this) -
	guaranteeing one-pass convergence (FR-12/NFR-03) instead of a loop.
	"""
	if tender_doc.flags.get("skip_cross_sync"):
		return

	rows = frappe.get_all(
		"Project Tender Direct Cost Detail",
		filters={"tender": tender_doc.name},
		fields=["name", "parent"],
	)
	if not rows:
		return

	frappe.db.set_value(
		"Project Tender Direct Cost Detail",
		{"tender": tender_doc.name},
		"tender_total",
		tender_doc.grand_total,
		update_modified=False,
	)

	for parent in {r.parent for r in rows}:
		pt = frappe.get_doc("Project Tender", parent)
		pt.flags.skip_cross_sync = True
		pt.save(ignore_permissions=True)
		frappe.publish_realtime(
			"doc_update",
			{"modified": pt.modified, "doctype": "Project Tender", "name": pt.name},
			doctype="Project Tender",
			docname=pt.name,
			after_commit=True,
		)
		sync_project_tender_to_tenders(pt, skip_tender=tender_doc.name)


def sync_project_tender_to_tenders(pt_doc, skip_tender=None):
	"""Project Tender -> Tender push (Part A, symmetric direction). Pushes
	total_addition_percent to every linked Tender except skip_tender (the
	Tender whose own save just triggered this Project Tender's recompute,
	already up to date), saving each with skip_cross_sync so its on_update
	doesn't push back and re-trigger this Project Tender.
	"""
	for row in pt_doc.direct_cost_details:
		if not row.tender or row.tender == skip_tender:
			continue

		t = frappe.get_doc("Tender", row.tender)
		if t.status == "Complete":
			# Complete tenders are locked (validate_not_complete_locked) and
			# stay linked in Direct Cost Detail until someone manually
			# re-points the row to a revised version - pushing into them
			# here would throw and abort the whole cascade, including the
			# unrelated Tender save that triggered it.
			continue
		if flt(t.propagated_addition_percent) == flt(pt_doc.total_addition_percent):
			continue

		t.propagated_addition_percent = pt_doc.total_addition_percent
		t.flags.skip_cross_sync = True
		t.save(ignore_permissions=True)
		frappe.publish_realtime(
			"doc_update",
			{"modified": t.modified, "doctype": "Tender", "name": t.name},
			doctype="Tender",
			docname=t.name,
			after_commit=True,
		)
