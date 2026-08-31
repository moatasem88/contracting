import frappe
import erpnext
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from contracting.contracting.utils.progress_invoicing import (
	compute_condition_progress_invoice,
	compute_progress_invoice_items,
)


class ContractorInvoice(Document):
	def validate(self):
		self.set_retention_from_contract()
		if self.contractor_contract and frappe.db.exists(
			"Contractor Contract Payment Condition", {"parent": self.contractor_contract}
		):
			# FR-31/32: a contract with Payment Conditions is billed per
			# condition instead of per line - a separate code path, not a
			# tweak to the plain one, so Client Progress Invoice's own call
			# site (which never touches this field) needs no change.
			compute_condition_progress_invoice(self)
		else:
			compute_progress_invoice_items(
				self,
				"Contractor Invoice",
				source_doctype="Contractor Contract Item",
				source_parent=self.contractor_contract,
			)
		self.net_payable = self.total_this_period - self.total_retention_held

	def set_retention_from_contract(self):
		"""The contract's Retention charge row is the source of truth.

		Retention is agreed once, on the Subcontractor Contract's Additional
		Costs table (at most one Retention row is enforced there), so it is
		read from the contract rather than re-entered per invoice and left to
		drift. A flat-amount retention has no percentage to carry over, so
		that case is left alone for the invoice to handle on its own terms.

		The contract may legitimately carry no Retention row at all - Additional
		Costs is optional - in which case there is nothing to carry over and
		retention_percent is left as-is (0, i.e. nothing withheld).
		"""
		if not self.contractor_contract:
			return

		retention = frappe.db.get_value(
			"Contractor Contract Charge",
			{
				"parent": self.contractor_contract,
				"parenttype": "Subcontractor Contract",
				"charge_type": "Retention",
				"rate_type": "% of Net Total",
			},
			"rate_or_amount",
		)

		if retention is not None:
			self.retention_percent = retention

	def mark_as_measured(self):
		self.status = "Measured"
		self.measured_by = frappe.session.user
		self.save()


@frappe.whitelist()
def mark_as_measured(name):
	doc = frappe.get_doc("Contractor Invoice", name)
	doc.mark_as_measured()
	return doc.status


@frappe.whitelist()
def approve_and_invoice(name):
	"""Measured -> Approved -> generates a Draft Purchase Invoice ->
	Invoiced. Re-running on an already-Invoiced document is a no-op and
	just returns the existing Purchase Invoice - the natural
	duplicate-prevention mechanism (no separate guard needed)."""
	doc = frappe.get_doc("Contractor Invoice", name)

	if doc.status == "Invoiced":
		return doc.purchase_invoice

	doc.status = "Approved"
	doc.approved_by = frappe.session.user
	doc.save()

	pi = create_purchase_invoice(doc)

	doc.purchase_invoice = pi.name
	doc.status = "Invoiced"
	doc.save()
	return pi.name


def create_purchase_invoice(contractor_invoice):
	"""Draft Purchase Invoice for exactly this period's claimed qty per
	line, against the subcontractor (Supplier) on the linked
	Subcontractor Contract. Left as Draft so it still goes through the
	existing live Purchase Invoice approval Workflow on this bench - a
	separate, sequential financial-approval gate from this document's own
	measurement approval above."""
	contract = frappe.get_doc("Subcontractor Contract", contractor_invoice.contractor_contract)
	company = erpnext.get_default_company()

	pi = frappe.new_doc("Purchase Invoice")
	pi.supplier = contract.contractor_name
	pi.company = company
	pi.currency = erpnext.get_company_currency(company)
	pi.conversion_rate = 1
	pi.buying_price_list = frappe.db.get_value("Price List", {"buying": 1}, "name")
	pi.plc_conversion_rate = 1
	pi.due_date = today()
	pi.project = contract.project
	pi.cost_center = frappe.db.get_value("Company", company, "cost_center") or frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name"
	)

	# A BOQ work-item line generally has no item_code of its own (it's a
	# composite of Material/Labor/Equipment, not itself an Item - see the
	# Phase B.2 downstream note), so ERPNext can't auto-derive an expense
	# account for it the way it would for a real stocked/service Item.
	# Contracting Settings.expense_debit_account is the existing generic
	# fallback for exactly this kind of line.
	default_expense_account = frappe.db.get_single_value("Contracting Settings", "expense_debit_account")

	for row in contractor_invoice.items:
		if not row.this_period_qty:
			continue

		# A Contractor Contract Item carries no item_code of its own - it
		# allocates against a BOQ line, so the Item (when there is one) and
		# the UOM both come off that line.
		contract_item = frappe.db.get_value(
			"Contractor Contract Item", row.contract_item, ["work_item", "description", "uom"], as_dict=True
		) or frappe._dict()
		boq = frappe.db.get_value(
			"Tender BOQ Item", contract_item.work_item, ["item_code", "item_name", "uom"], as_dict=True
		) if contract_item.work_item else frappe._dict()

		item_code = boq.item_code
		item_name = (
			frappe.db.get_value("Item", item_code, "item_name")
			if item_code
			else contract_item.description or boq.item_name or row.contract_item
		)
		# custom_project_work_item is never map_fields-copied here (this PI
		# is built by hand, not via a Create From chain) - set explicitly so
		# the Project Cost & Billing rollup can trace this line back to its
		# Tender. Only resolvable when the contract line has a composite
		# work_item (Tender BOQ Item) - a labor/equipment-only line has no
		# Project BOQ Item counterpart (carry_boq_to_project only ever
		# creates one per Tender BOQ Item), so it's left blank and simply
		# excluded from per-Tender subcontracting totals.
		project_boq_item = (
			frappe.db.get_value(
				"Project BOQ Item", {"source_tender_boq_item": contract_item.work_item}, "name"
			)
			if contract_item.work_item
			else None
		)
		pi.append("items", {
			"item_code": item_code,
			"item_name": item_name,
			"description": contract_item.description or boq.item_name or item_name,
			"qty": row.this_period_qty,
			"rate": row.rate,
			"uom": contract_item.uom or boq.uom,
			"expense_account": default_expense_account,
			"cost_center": pi.cost_center,
			"custom_project_work_item": project_boq_item,
		})

	pi.run_method("set_missing_values")
	pi.insert(ignore_permissions=True)
	return pi
