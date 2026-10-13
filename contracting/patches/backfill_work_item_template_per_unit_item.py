import frappe

BACKFILL_FLAG = "contracting_per_unit_item_backfilled"


def execute():
	"""Backfill Work Item Template Addition.per_unit_item and the split
	totals on Work Item Template V2 (2026-10-13 per-unit addition BRD).

	per_unit_item defaults to 1, matching how a template already treated
	every Fixed Amount (per unit). The UPDATE runs once only, guarded by a
	global flag - re-running must never re-tick a box a user deliberately
	unticked to mark a fixed amount as a lump sum.

	Totals are recomputed on every run (idempotent) and written through
	db_update rather than save(): save() would also run sync_work_item()
	and bump modified on every template for a pure derived-field fill.
	"""
	if not frappe.db.get_global(BACKFILL_FLAG):
		frappe.db.sql("update `tabWork Item Template Addition` set per_unit_item = 1")
		frappe.db.set_global(BACKFILL_FLAG, "1")
		print("Set per_unit_item = 1 on all existing Work Item Template Addition rows")

	names = frappe.get_all("Work Item Template V2", pluck="name")
	for name in names:
		doc = frappe.get_doc("Work Item Template V2", name)
		doc.calculate_totals()
		doc.db_update()

	frappe.db.commit()
	print("Recomputed totals on {0} Work Item Template V2 records".format(len(names)))
