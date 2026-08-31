import frappe


# Pre-seeded per the Project Tender Consolidation BRD, §13. category_name /
# component_name are the naming fields, so frappe.db.exists is enough to
# make this idempotent - a re-run after a record was renamed just re-creates
# it under the original name rather than erroring.
TENDER_CATEGORIES = ("Civil", "Electrical", "Mechanical", "Elevators")

# (component_name, section, default_calculation_type, default_rate)
TENDER_COST_COMPONENTS = (
	("Site Management & Supervision", "Indirect Cost", "Fixed Amount", None),
	("Temporary Site Facilities", "Indirect Cost", "Fixed Amount", None),
	("Transportation for Workers", "Indirect Cost", "Fixed Amount", None),
	("Accommodation", "Indirect Cost", "Fixed Amount", None),
	("Safety Requirement", "Indirect Cost", "Fixed Amount", None),
	("Equipment & Instrument", "Indirect Cost", "Fixed Amount", None),
	("Others", "Indirect Cost", "Fixed Amount", None),
	("Financial Requirement", "Indirect Cost", "Percentage of Direct Cost", 1),
	("Social Insurance", "Indirect Cost", "Percentage of Direct Cost", 4.92),
	("Project Insurance", "Indirect Cost", "Percentage of Direct Cost", 0.69),
	("Syndicates Stamp", "Indirect Cost", "Fixed Amount", None),
	("Taxes", "Indirect Cost", "Fixed Amount", None),
	("Risk", "Indirect Cost", "Percentage of Direct Cost", 2),
	("Profit", "Overhead & Profit", "Percentage of Direct Cost", 10),
	("Head Office Overhead", "Overhead & Profit", "Percentage of Direct Cost", 2.5),
	("Other Deductions", "Deduction", "Fixed Amount", None),
	("VAT", "Deduction", "Percentage of Total Price", 14),
)


def execute():
	for category_name in TENDER_CATEGORIES:
		if frappe.db.exists("Tender Category", category_name):
			continue
		frappe.get_doc({
			"doctype": "Tender Category",
			"category_name": category_name,
		}).insert(ignore_permissions=True)

	for component_name, section, calculation_type, default_rate in TENDER_COST_COMPONENTS:
		if frappe.db.exists("Tender Cost Component", component_name):
			continue
		frappe.get_doc({
			"doctype": "Tender Cost Component",
			"component_name": component_name,
			"section": section,
			"default_calculation_type": calculation_type,
			"default_rate": default_rate,
		}).insert(ignore_permissions=True)
