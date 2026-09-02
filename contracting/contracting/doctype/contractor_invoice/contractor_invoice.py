import frappe
import erpnext
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, today

from contracting.contracting.utils.progress_invoicing import (
	compute_condition_progress_invoice,
	compute_progress_invoice_items,
	get_contract_retention_rate,
)


class ContractorInvoice(Document):
	def validate(self):
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
				retention_rate=get_contract_retention_rate(self.contractor_contract),
			)
		self.rebuild_additional_costs()
		self.net_payable = self.total_this_period + self.total_additional_charges

	def rebuild_additional_costs(self):
		"""FR-08/09/10/12: additional_costs is fully rebuilt from the linked
		contract's own Additional Costs on every validate(), never held as a
		stale snapshot - every row, Retention included, is mirrored and
		reduced to this invoice's share of the contract:
		total_this_period / net_total. total_retention_held is then rolled
		up from whichever mirrored row(s) carry cost_category == "Retention",
		rather than being accumulated separately inside
		compute_progress_invoice_items/compute_condition_progress_invoice.

		The cascade below mirrors SubcontractorContract._compute_charge_amount
		but keyed to total_this_period instead of net_total as the base, so a
		charge type referencing an earlier row (On Previous Row Amount/Total)
		resolves against this invoice's own prorated copy of that row, not
		the contract's.
		"""
		self.set("additional_costs", [])
		self.total_additional_charges = 0.0
		self.total_retention_held = 0.0
		if not self.contractor_contract:
			return

		contract = frappe.get_doc("Subcontractor Contract", self.contractor_contract)
		net_total = flt(contract.net_total)
		ratio = flt(self.total_this_period) / net_total if net_total else 0.0

		source_rows = list(contract.additional_costs)
		by_idx = {r.idx: r for r in source_rows}
		computed = {}
		running_total = 0.0

		for row in source_rows:
			amount = self._prorated_charge_amount(row, ratio, contract, by_idx, computed)
			signed = -amount if row.add_deduct_tax == "Deduct" else amount
			running_total += signed
			computed[row.idx] = {"tax_amount": amount, "total": running_total}

			self.append("additional_costs", {
				"cost_category": row.cost_category,
				"charge_type": row.charge_type,
				"category": row.category,
				"account_head": row.account_head,
				"cost_center": row.cost_center,
				"rate": row.rate,
				"tax_amount": amount,
				"add_deduct_tax": row.add_deduct_tax,
				"total": running_total,
				"description": row.description,
				"source_charge": row.name,
			})

		self.total_additional_charges = running_total
		self.total_retention_held = sum(
			flt(r.tax_amount) for r in self.additional_costs if r.cost_category == "Retention"
		)

	def _prorated_charge_amount(self, row, ratio, contract, by_idx, computed):
		charge_type = row.charge_type or "On Net Total"

		if charge_type == "Actual":
			return flt(row.tax_amount) * ratio
		if charge_type == "On Net Total":
			return flt(self.total_this_period) * flt(row.rate) / 100.0
		if charge_type in ("On Previous Row Amount", "On Previous Row Total"):
			ref_idx = cint(row.row_id)
			if not ref_idx or ref_idx not in by_idx:
				frappe.throw(
					_("Row referencing charge #{0} ({1}): its Reference Row # {2} is not on this "
					  "invoice - Additional Costs rows must be ordered so a referencing row's target "
					  "is included and comes before it.").format(
						row.idx, row.description or row.cost_category, row.row_id
					)
				)
			ref = computed.get(ref_idx)
			if not ref:
				frappe.throw(
					_("Row {0}: Reference Row # {1} has not been computed yet - Additional Costs "
					  "rows must be ordered so a row only references an earlier one.").format(row.idx, row.row_id)
				)
			base = ref["tax_amount"] if charge_type == "On Previous Row Amount" else ref["total"]
			return base * flt(row.rate) / 100.0
		if charge_type == "On Item Quantity":
			total_qty = sum(flt(r.qty) for r in contract.contracted_items)
			return flt(row.rate) * total_qty * ratio

		frappe.throw(_("Row {0}: unrecognised Charge Type {1}.").format(row.idx, charge_type))

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

	# FR-11: Additional Costs already computed onto this invoice
	# (rebuild_additional_costs) carry straight into the PI's own native
	# taxes table - CustomPurchaseInvoice.make_tax_gl_entries already knows
	# how to post GL entries from exactly these field names.
	for row in contractor_invoice.additional_costs:
		pi.append("taxes", {
			"charge_type": row.charge_type,
			"account_head": row.account_head,
			"category": row.category,
			"add_deduct_tax": row.add_deduct_tax,
			"rate": row.rate,
			"tax_amount": row.tax_amount,
			"description": row.description,
			"cost_center": row.cost_center or pi.cost_center,
		})

	pi.run_method("set_missing_values")
	pi.insert(ignore_permissions=True)
	return pi
