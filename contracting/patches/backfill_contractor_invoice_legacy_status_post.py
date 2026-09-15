import frappe

# Second half of the Contractor Invoice status/measured_by/approved_by ->
# workflow_state backfill - see backfill_contractor_invoice_legacy_status_pre.py
# for why this is split in two around `bench migrate`. Run this AFTER
# migrate, once the new legacy_status/legacy_measured_by/legacy_approved_by
# fields (contractor_invoice.json) exist.
#
# workflow_state is deliberately set to "Draft" for every pre-existing row,
# never "Approved"/"Rejected"/any Pending-* state: every row here has
# docstatus 0 (Contractor Invoice wasn't submittable before this change),
# and Draft is the only workflow state whose own docstatus is 0 that
# doesn't assert a false approval/review milestone the row never actually
# reached under the new 7-stage chain. The original status ("Measured",
# "Approved", "Invoiced", ...) is preserved verbatim in legacy_status for
# reporting/audit rather than lost or guessed into a specific Pending-*
# stage - staff re-progress these through the real workflow from Draft.
#
# Idempotent: only touches rows with a blank workflow_state; safe to
# re-run (a no-op on rows already backfilled or freshly created since).

BACKUP_TABLE = "_contracting_ci_legacy_status_backup"


def execute():
	# Not a DocType table - frappe.db.table_exists() prefixes "tab" onto
	# whatever name it's given, which is wrong for this raw backup table.
	if not frappe.db.sql("SHOW TABLES LIKE %s", (BACKUP_TABLE,)):
		return

	rows = frappe.db.sql(
		f"SELECT name, status, measured_by, approved_by FROM `{BACKUP_TABLE}`", as_dict=True
	)
	for row in rows:
		if not frappe.db.exists("Contractor Invoice", row.name):
			continue
		if frappe.db.get_value("Contractor Invoice", row.name, "workflow_state"):
			continue
		frappe.db.set_value(
			"Contractor Invoice",
			row.name,
			{
				"legacy_status": row.status,
				"legacy_measured_by": row.measured_by,
				"legacy_approved_by": row.approved_by,
				"workflow_state": "Draft",
			},
			update_modified=False,
		)
	frappe.db.commit()
