"""Contractor Contract Item progress/invoiced/paid percentage tracking.

Three amount-based percentages per contracted line, all against the same
base (Contractor Contract Item.amount = qty * unit_price):

    progress_percentage - claimed via submitted Contractor Invoices
    invoiced_percentage - reached a submitted Purchase Invoice
    paid_percentage     - actually paid via submitted Payment Entries

Mirrors project_cost_billing.py's own shape (compute_* pure in-memory,
recompute_and_save() persists, thin hook targets at the bottom) - see that
module for why every hook here is wired directly onto the source
doctype's own on_submit/on_cancel (hooks.py) rather than relying on this
doctype's own on_update: core's various update_project()-style shortcuts
and this app's own controllers already call db_set()/db_update() without
necessarily running through a path that would fire a generic on_update.

Every query below filters docstatus = 1 - a cancelled Contractor
Invoice/Purchase Invoice/Payment Entry simply drops out of the sum on the
next recompute, so on_cancel needs no separate "undo" logic, same
convention progress_invoicing.py's prior-progress lookups already use.
"""

import frappe
from frappe.utils import flt


def _progress_by_contract_item(contract_name):
	"""FR: Progress % numerator - this_period_amount already reflects the
	correct running total per contract_item regardless of whether the
	source contract bills plain or per Payment Condition (both paths feed
	the same Contractor Invoice Item.this_period_amount field - see
	compute_progress_invoice_items/compute_condition_progress_invoice in
	progress_invoicing.py)."""
	rows = frappe.db.sql(
		"""
		select cii.contract_item, sum(cii.this_period_amount) as amount
		from `tabContractor Invoice Item` cii
		inner join `tabContractor Invoice` ci on ci.name = cii.parent
		where ci.contractor_contract = %(contract)s and ci.docstatus = 1
		group by cii.contract_item
		""",
		{"contract": contract_name},
		as_dict=True,
	)
	return {r.contract_item: flt(r.amount) for r in rows}


def _invoiced_by_contract_item(contract_name):
	"""Invoiced % numerator - Purchase Invoice Item.amount summed per
	contract_item, via the custom_contract_item back-link
	create_purchase_invoice() sets at generation time."""
	rows = frappe.db.sql(
		"""
		select pii.custom_contract_item as contract_item, sum(pii.amount) as amount
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent
		inner join `tabContractor Contract Item` cci on cci.name = pii.custom_contract_item
		where cci.parent = %(contract)s and pi.docstatus = 1
		group by pii.custom_contract_item
		""",
		{"contract": contract_name},
		as_dict=True,
	)
	return {r.contract_item: flt(r.amount) for r in rows}


def _paid_by_contract_item(contract_name):
	"""Paid % numerator. A Payment Entry Reference is invoice-level
	(reference_name = the whole Purchase Invoice, one allocated_amount) -
	proration across that invoice's own contract-item lines by each line's
	share of the invoice's grand_total is the same math
	project_cost_billing._liabilities_by_tender and
	._payments_collected_by_sales_order already use for this identical
	"one invoice, many lines, one payment" shape."""
	line_rows = frappe.db.sql(
		"""
		select pii.custom_contract_item as contract_item, pii.parent as invoice,
			sum(pii.amount) as line_amount
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent
		inner join `tabContractor Contract Item` cci on cci.name = pii.custom_contract_item
		where cci.parent = %(contract)s and pi.docstatus = 1
		group by pii.custom_contract_item, pii.parent
		""",
		{"contract": contract_name},
		as_dict=True,
	)
	if not line_rows:
		return {}

	invoice_names = list({r.invoice for r in line_rows})
	invoice_totals = {
		d.name: flt(d.grand_total)
		for d in frappe.get_all(
			"Purchase Invoice", filters={"name": ["in", invoice_names]}, fields=["name", "grand_total"]
		)
	}
	allocated = {
		d.reference_name: flt(d.allocated)
		for d in frappe.db.sql(
			"""
			select per.reference_name, sum(per.allocated_amount) as allocated
			from `tabPayment Entry Reference` per
			inner join `tabPayment Entry` pe on pe.name = per.parent
			where per.reference_doctype = 'Purchase Invoice'
				and per.reference_name in %(pis)s
				and pe.docstatus = 1 and pe.party_type = 'Supplier'
			group by per.reference_name
			""",
			{"pis": invoice_names},
			as_dict=True,
		)
	}

	result = {}
	for row in line_rows:
		invoice_total = invoice_totals.get(row.invoice) or 0
		invoice_allocated = allocated.get(row.invoice) or 0
		if not invoice_total or not invoice_allocated:
			continue
		share = flt(row.line_amount) / invoice_total
		result[row.contract_item] = result.get(row.contract_item, 0) + invoice_allocated * share
	return result


def compute_contract_item_progress(contract_doc):
	"""Pure, in-memory. Never persists - callers decide how/when to save."""
	progress_by_item = _progress_by_contract_item(contract_doc.name)
	invoiced_by_item = _invoiced_by_contract_item(contract_doc.name)
	paid_by_item = _paid_by_contract_item(contract_doc.name)

	for row in contract_doc.contracted_items:
		base = flt(row.amount)
		row.progress_percentage = flt(progress_by_item.get(row.name)) / base * 100 if base else 0
		row.invoiced_percentage = flt(invoiced_by_item.get(row.name)) / base * 100 if base else 0
		row.paid_percentage = flt(paid_by_item.get(row.name)) / base * 100 if base else 0


def recompute_and_save(contract_name):
	if not contract_name:
		return
	contract_doc = frappe.get_doc("Subcontractor Contract", contract_name)
	compute_contract_item_progress(contract_doc)
	contract_doc.update_child_table("contracted_items")


# ---------- hook targets (contracting/hooks.py doc_events) ----------

def on_contractor_invoice_transaction(doc, method=None):
	"""Contractor Invoice on_submit and on_cancel."""
	recompute_and_save(doc.get("contractor_contract"))


def _contracts_for_purchase_invoice(pi_name):
	return frappe.db.sql(
		"""
		select distinct cci.parent as contract
		from `tabPurchase Invoice Item` pii
		inner join `tabContractor Contract Item` cci on cci.name = pii.custom_contract_item
		where pii.parent = %(pi)s
		""",
		{"pi": pi_name},
		pluck="contract",
	)


def on_purchase_invoice_transaction(doc, method=None):
	"""Purchase Invoice on_submit and on_cancel - only relevant when this PI
	carries at least one subcontracting line (custom_contract_item set);
	a purely material/general PI resolves to no contracts and is a no-op."""
	for contract_name in _contracts_for_purchase_invoice(doc.name):
		recompute_and_save(contract_name)


def on_payment_entry_transaction(doc, method=None):
	"""Payment Entry on_submit and on_cancel."""
	if doc.payment_type != "Pay" or doc.party_type != "Supplier":
		return

	pi_names = frappe.db.sql(
		"""
		select distinct reference_name from `tabPayment Entry Reference`
		where parent = %(pe)s and reference_doctype = 'Purchase Invoice'
		""",
		{"pe": doc.name},
		pluck="reference_name",
	)
	contracts = set()
	for pi_name in pi_names:
		contracts.update(_contracts_for_purchase_invoice(pi_name))
	for contract_name in contracts:
		recompute_and_save(contract_name)
