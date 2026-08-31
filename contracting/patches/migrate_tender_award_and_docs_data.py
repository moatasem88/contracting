import frappe

# Tender's status field is being narrowed to Draft/In Progress/Complete
# (the award pipeline moves to Project Tender). Under Review/Submitted have
# no live rows on this site today - mapped forward anyway so future data
# in that shape doesn't get stranded. Won/Lost/Cancelled/Superseded all
# collapse to Complete: the historical Won-vs-Lost distinction isn't lost,
# it's relocated onto the Project Tender that award data gets carried to
# below (or, for orphaned Tenders with no Project Tender link, flagged for
# manual review rather than silently discarded).
STATUS_REMAP = {
	"Draft": "Draft",
	"Under Review": "In Progress",
	"Submitted": "In Progress",
	"Won": "Complete",
	"Lost": "Complete",
	"Cancelled": "Complete",
	"Superseded": "Complete",
}


def execute():
	tenders = frappe.get_all(
		"Tender",
		fields=[
			"name", "status", "project_tender", "project", "sales_order",
			"contract_document", "notes", "attachments",
		],
	)

	attachments_migrated = 0
	linked_award_data_copied = 0
	orphans_logged = 0

	for tender in tenders:
		if tender.notes or tender.attachments:
			# Direct child-row insert, not doc.save() on the parent - saving
			# the Tender would re-run on_update()/handle_won_automation()
			# against live Won tenders, which is exactly what this patch
			# must not trigger.
			frappe.get_doc({
				"doctype": "Tender Attachment",
				"parent": tender.name,
				"parenttype": "Tender",
				"parentfield": "attachment_details",
				"idx": 1,
				"attach_type": "Other",
				"description": tender.notes,
				"file": tender.attachments,
			}).insert(ignore_permissions=True)
			attachments_migrated += 1

		has_link_data = bool(tender.project or tender.sales_order or tender.contract_document)

		if tender.project_tender:
			row_name = frappe.db.get_value(
				"Project Tender Direct Cost Detail",
				{"parent": tender.project_tender, "tender": tender.name},
				"name",
			)
			if row_name and has_link_data:
				frappe.db.set_value(
					"Project Tender Direct Cost Detail",
					row_name,
					{
						"project": tender.project,
						"sales_order": tender.sales_order,
						"contract_document": tender.contract_document,
					},
					update_modified=False,
				)
				linked_award_data_copied += 1
		elif has_link_data:
			# Not auto-migrated (per explicit decision): these predate the
			# project_tender field and can't be safely guessed into a new
			# Project Tender - some even share one Project/Sales Order
			# across multiple Tenders, which the new per-Tender automation
			# isn't designed to represent. Logged for manual linking.
			frappe.log_error(
				title="Tender award data orphaned by Project Tender migration",
				message=(
					f"Tender {tender.name}: status={tender.status}, project={tender.project}, "
					f"sales_order={tender.sales_order}, contract_document={tender.contract_document}, "
					"but no Project Tender link. Not auto-migrated - create/choose a Project Tender "
					"and add a Direct Cost Detail row pointing at this Tender if this award data "
					"should be preserved there."
				),
			)
			orphans_logged += 1

		new_status = STATUS_REMAP.get(tender.status, "Draft")
		if new_status != tender.status:
			frappe.db.set_value("Tender", tender.name, "status", new_status, update_modified=False)

	print(
		f"migrate_tender_award_and_docs_data: {attachments_migrated} attachments migrated, "
		f"{linked_award_data_copied} linked Tenders' award data copied to Project Tender, "
		f"{orphans_logged} orphaned Tenders logged for manual review (see Error Log)."
	)
