import frappe

# Contractor Invoice's old status/measured_by/approved_by columns are being
# replaced by workflow_state (create_contractor_invoice_workflow.py) -
# bench migrate's own schema sync drops any DB column whose field was
# removed from the doctype JSON, so this MUST run BEFORE `bench migrate`,
# while the old columns still physically exist, or their data is gone for
# good. Dumps a raw snapshot into a plain (non-DocType) table rather than
# a Custom Field, since the new legacy_* fields this backfill ultimately
# writes to (contractor_invoice.json) don't exist as DB columns yet either
# at this point - see backfill_contractor_invoice_legacy_status_post.py,
# which runs after migrate and reads this table back.
#
# Idempotent: CREATE TABLE IF NOT EXISTS + upsert: re-running before a
# second migrate just re-syncs rows; running it after the columns are
# already gone is a no-op.

BACKUP_TABLE = "_contracting_ci_legacy_status_backup"


def execute():
	if not frappe.db.has_column("Contractor Invoice", "status"):
		# Already migrated past this point (or a fresh site that never had
		# these fields) - nothing left to dump.
		return

	frappe.db.sql(f"""
		CREATE TABLE IF NOT EXISTS `{BACKUP_TABLE}` (
			name VARCHAR(140) PRIMARY KEY,
			status VARCHAR(140),
			measured_by VARCHAR(140),
			approved_by VARCHAR(140)
		)
	""")
	frappe.db.sql(f"""
		INSERT INTO `{BACKUP_TABLE}` (name, status, measured_by, approved_by)
		SELECT name, status, measured_by, approved_by FROM `tabContractor Invoice`
		ON DUPLICATE KEY UPDATE
			status = VALUES(status),
			measured_by = VALUES(measured_by),
			approved_by = VALUES(approved_by)
	""")
	frappe.db.commit()
