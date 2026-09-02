import frappe

# CIVIL/ELEC/MECH/ELEV were the Select field's stored codes; these are the
# live Tender Category record names (Tender Category autonames on
# category_name) they map onto now that department is a Link.
CODE_TO_CATEGORY = {
	"CIVIL": "Civil",
	"ELEC": "Electrical",
	"MECH": "Mechanical",
	"ELEV": "Elevators",
}


def execute():
	"""department: Select -> Link (FR-SC-05). Direct frappe.db.set_value,
	not doc.save() - 3 of the 5 affected contracts are already submitted
	and a save would re-trigger validate()/the approval workflow.

	Idempotency guard: a row whose current value already names a live
	Tender Category (i.e. this patch already ran) is left alone.
	"""
	live_categories = set(frappe.get_all("Tender Category", pluck="name"))

	contracts = frappe.db.sql(
		"""
		select name, department
		from `tabSubcontractor Contract`
		where ifnull(department, '') != ''
		""",
		as_dict=True,
	)

	converted = 0
	for contract in contracts:
		if contract.department in live_categories:
			continue

		new_value = CODE_TO_CATEGORY.get(contract.department)
		if not new_value:
			frappe.log_error(
				title="convert_subcontractor_contract_department_to_link",
				message=f"{contract.name}: unrecognised department code {contract.department!r}, left unchanged.",
			)
			continue

		frappe.db.set_value("Subcontractor Contract", contract.name, "department", new_value)
		converted += 1

	frappe.db.commit()
	print(f"convert_subcontractor_contract_department_to_link: converted {converted} row(s)")
