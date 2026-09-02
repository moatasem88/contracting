import frappe

CHARGE_TYPE_MAP = {"% of Net Total": "On Net Total", "Flat Amount": "Actual"}


def execute():
	"""FR-05/06 field-rename onto the new Purchase-Taxes-and-Charges-parity
	shape, for the pre-existing Contractor Contract Charge rows.

	MUST run immediately after the `bench migrate` that ships the new
	doctype JSON, before any user re-saves one of the affected contracts:
	`charge_type`'s fieldname is reused for a new meaning (old
	classification -> new calc-type), which is safe because MySQL's
	ALTER TABLE MODIFY COLUMN does not rewrite existing row values (only
	future-insert defaults) - so the OLD string ("Tax"/"Retention"/...) is
	still sitting in that column until this patch overwrites it. A save in
	between would hit the new calculate_totals() cascade with that stale
	string as if it were the new charge_type - a loud, safe throw (not a
	valid option), not silent corruption, but best avoided.

	account_head is deliberately left blank on every migrated row - the
	explicit "leave blank, block later use" decision: the next normal save
	of the contract, or a new Contractor Invoice against it, throws asking
	for it.

	Idempotent: skips a row whose cost_category is already populated.
	"""
	if not frappe.db.table_exists("Contractor Contract Charge"):
		return

	rows = frappe.db.sql(
		"""
		select name, charge_type as old_charge_type, rate_type, rate_or_amount,
		       computed_amount, add_or_deduct
		from `tabContractor Contract Charge`
		where ifnull(cost_category, '') = ''
		""",
		as_dict=True,
	)

	migrated = 0
	for row in rows:
		new_charge_type = CHARGE_TYPE_MAP.get(row.rate_type, "On Net Total")
		values = {
			"cost_category": row.old_charge_type,
			"charge_type": new_charge_type,
			"category": "Total",
			"add_deduct_tax": row.add_or_deduct,
			"total": row.computed_amount,
		}
		if new_charge_type == "Actual":
			values["tax_amount"] = row.rate_or_amount
		else:
			values["rate"] = row.rate_or_amount

		frappe.db.set_value("Contractor Contract Charge", row.name, values, update_modified=False)
		migrated += 1

	frappe.db.commit()
	print(f"restructure_additional_costs_ptc_parity: migrated {migrated} row(s)")
