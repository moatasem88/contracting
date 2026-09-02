import frappe

RETENTION_ACCOUNT_NAME = "Account Subcontractor Retention Primary"

# The 4 live contracts found carrying a Retention row when this was
# grounded (2026-09-01): -00008/-00009 have add_deduct_tax = "Add" (should
# be "Deduct" - a Retention charge withholds money, it doesn't add it), and
# -00004/-00006/-00008 have no account_head. -00009 already has one
# (215020001, the same account this patch assigns to the other three) -
# confirmed by the user as the correct default for all of them.
SIGN_FIX_CONTRACTS = ("Subcontractor-CONTRACT-2026-00008", "Subcontractor-CONTRACT-2026-00009")
ACCOUNT_FIX_CONTRACTS = (
	"Subcontractor-CONTRACT-2026-00004",
	"Subcontractor-CONTRACT-2026-00006",
	"Subcontractor-CONTRACT-2026-00008",
)


def execute():
	"""Idempotent: only ever touches rows still in the wrong state, so a
	re-run (or a run against a site where these were already hand-fixed) is
	a no-op. Must run before the code shipping FR-17's new
	validate_retention_rows() sign check, or -00008/-00009 become
	unsaveable with no prior warning."""
	if not frappe.db.table_exists("Contractor Contract Charge"):
		return

	rows = frappe.get_all(
		"Contractor Contract Charge",
		filters={
			"parenttype": "Subcontractor Contract",
			"parent": ["in", SIGN_FIX_CONTRACTS + ACCOUNT_FIX_CONTRACTS],
			"cost_category": "Retention",
		},
		fields=["name", "parent", "add_deduct_tax", "account_head"],
	)
	by_parent = {r.parent: r for r in rows}

	fixed = 0
	for parent in SIGN_FIX_CONTRACTS:
		row = by_parent.get(parent)
		if row and row.add_deduct_tax != "Deduct":
			frappe.db.set_value(
				"Contractor Contract Charge", row.name, "add_deduct_tax", "Deduct", update_modified=False
			)
			fixed += 1

	# account_name on this site carries a bilingual suffix
	# ("...Primary - حساب...") rather than being an exact match.
	account = frappe.db.get_value(
		"Account", {"account_name": ["like", f"{RETENTION_ACCOUNT_NAME}%"]}, "name"
	)
	if account:
		for parent in ACCOUNT_FIX_CONTRACTS:
			row = by_parent.get(parent)
			if row and not row.account_head:
				frappe.db.set_value(
					"Contractor Contract Charge", row.name, "account_head", account, update_modified=False
				)
				fixed += 1
	elif any(not by_parent[p].account_head for p in ACCOUNT_FIX_CONTRACTS if p in by_parent):
		frappe.log_error(
			title="fix_retention_row_sign_and_account_head",
			message=f"Account '{RETENTION_ACCOUNT_NAME}' not found - account_head rows left unfixed.",
		)

	for parent in set(SIGN_FIX_CONTRACTS) | set(ACCOUNT_FIX_CONTRACTS):
		frappe.clear_document_cache("Subcontractor Contract", parent)

	frappe.db.commit()
	print(f"fix_retention_row_sign_and_account_head: fixed {fixed} field(s)")
