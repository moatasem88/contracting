"""Project "Cost and Billing" rollup - Won Project Tender automation BRD
(2026-08-28).

Sales Order/Sales Invoice/Purchase Invoice's own update_project() methods
(erpnext core) recompute their one native field and then call
project.db_update() directly, bypassing validate()/on_update()/doc_events
entirely (frappe/model/base_document.py's db_update_all() docstring: "DOES
NOT VALIDATE AND CALL TRIGGERS"). A doc_events["Project"]["on_update"] hook
alone would therefore only fire on Won automation's own Project.insert()
and a manual form Save - never when a Sales Order/Sales Invoice/Purchase
Invoice/Purchase Receipt/Payment Entry is submitted or cancelled against
the project. Every hook target below is wired directly onto those source
doctypes' own on_submit/on_cancel (see hooks.py) so the section actually
stays live, not just on_project_update.

recompute_and_save() always persists explicitly (db_update() +
update_child_table()) rather than relying on an enclosing save() - there
usually isn't one, and even for the Project's own on_update case core has
already written the doc's pre-hook field values by the time on_update
fires.
"""

import frappe
from frappe.utils import flt


def compute_cost_and_billing(project_doc):
	"""Pure, in-memory. Never persists - callers decide how/when to save."""
	if not project_doc.project_tender:
		return

	direct_cost_rows = frappe.get_all(
		"Project Tender Direct Cost Detail",
		filters={"project": project_doc.name},
		fields=["tender", "sales_order", "tender_total"],
	)
	sales_orders = [r.sales_order for r in direct_cost_rows if r.sales_order]

	_compute_progress_invoiced(project_doc, sales_orders)
	_compute_header_fields(project_doc, sales_orders)
	_rebuild_cost_billing_details(project_doc, direct_cost_rows)
	_update_sales_order_details_rows(project_doc, sales_orders)


def recompute_and_save(project_name):
	if not project_name:
		return
	project_doc = frappe.get_doc("Project", project_name)
	if not project_doc.project_tender:
		return
	compute_cost_and_billing(project_doc)
	project_doc.db_update()
	project_doc.update_child_table("cost_billing_details")
	project_doc.update_child_table("sales_order_details")


# ---------- hook targets (contracting/hooks.py doc_events) ----------

def on_project_update(doc, method=None):
	recompute_and_save(doc.name)


def on_sales_transaction(doc, method=None):
	"""Sales Order / Sales Invoice on_submit and on_cancel."""
	recompute_and_save(doc.get("project"))


def on_purchase_transaction(doc, method=None):
	"""Purchase Invoice / Purchase Receipt on_submit and on_cancel."""
	recompute_and_save(doc.get("project"))


def on_payment_entry_transaction(doc, method=None):
	recompute_and_save(doc.get("project"))


def on_client_progress_invoice_update(doc, method=None):
	"""FR-09's progress-invoiced rollup reads Client Progress Invoice.status
	directly (not via its Sales Invoice) - marking one Invoiced changes the
	total immediately, even though create_sales_invoice() leaves that
	Sales Invoice as a Draft the accountant submits later on their own
	schedule (client_progress_invoice.py's own create_sales_invoice
	docstring). Sales Invoice on_submit alone would miss this."""
	if doc.project:
		# Project-mode claims already know their Project directly - no need
		# to go through the Direct Cost Detail join below, which keys off a
		# single sales_order that project-mode invoices leave blank.
		recompute_and_save(doc.project)
		return
	if not doc.sales_order:
		return
	project_name = frappe.db.get_value(
		"Project Tender Direct Cost Detail", {"sales_order": doc.sales_order}, "project"
	)
	recompute_and_save(project_name)


def on_employee_advance_update(doc, method=None):
	"""Also recomputes the *old* project when an advance is reassigned
	(FR-14 edge case) - both projects' totals need to move."""
	recompute_and_save(doc.get("project"))
	before = doc.get_doc_before_save()
	if before and before.get("project") and before.get("project") != doc.get("project"):
		recompute_and_save(before.get("project"))


# ---------- FR-09: progress-invoiced header ----------

def _compute_progress_invoiced(project_doc, sales_orders):
	# Project-mode Client Progress Invoices carry project directly and leave
	# sales_order blank (enforced by validate_source()), so they're invisible
	# to the sales_order-keyed sum below - summed separately here instead.
	# The two filters are disjoint by row, so no double-counting risk.
	total = _sum(
		"Client Progress Invoice",
		{"sales_order": ["in", sales_orders], "docstatus": 1},
		"total_this_period",
	) if sales_orders else 0
	total += _sum(
		"Client Progress Invoice",
		{"project": project_doc.name, "docstatus": 1},
		"total_this_period",
	)
	project_doc.total_progress_client_invoiced_amount = total
	project_doc.total_progress_client_invoiced_percent = (
		total / flt(project_doc.total_sales_amount) * 100 if flt(project_doc.total_sales_amount) else 0
	)


# ---------- FR-10: Cost & Billing header ----------

def _compute_header_fields(project_doc, sales_orders):
	pt = frappe.db.get_value(
		"Project Tender",
		project_doc.project_tender,
		["total_direct_cost", "total_indirect_cost"],
		as_dict=True,
	) or frappe._dict()
	project_doc.total_estimated_direct_cost = flt(pt.total_direct_cost)
	project_doc.total_estimated_indirect_cost = flt(pt.total_indirect_cost)
	project_doc.targeted_profit_percent = flt(frappe.db.get_value(
		"Project Tender Cost Line",
		{
			"parent": project_doc.project_tender,
			"parentfield": "overhead_deduction_details",
			"cost_component": "Profit",
		},
		"rate",
	))

	project_doc.total_client_invoiced = flt(project_doc.total_billed_amount)
	project_doc.total_client_invoiced_percent = (
		project_doc.total_client_invoiced / flt(project_doc.total_sales_amount) * 100
		if flt(project_doc.total_sales_amount) else 0
	)

	payments_by_so = _payments_collected_by_sales_order(sales_orders)
	project_doc.flags._payments_by_so = payments_by_so
	project_doc.total_payments_collected = sum(payments_by_so.values()) if payments_by_so else 0
	project_doc.total_payments_collected_percent = (
		project_doc.total_payments_collected / project_doc.total_client_invoiced * 100
		if project_doc.total_client_invoiced
		else 0
	)

	subcontracting_pis = frappe.db.sql(
		"""
		select pi.name, pi.grand_total
		from `tabPurchase Invoice` pi
		where pi.project = %s and pi.docstatus = 1
			and exists (select 1 from `tabContractor Invoice` ci where ci.purchase_invoice = pi.name)
		""",
		project_doc.name,
		as_dict=True,
	)
	subcontracting_pi_names = [r.name for r in subcontracting_pis]
	project_doc.flags._subcontracting_pi_names = subcontracting_pi_names
	project_doc.total_subcontracting_invoices = sum(flt(r.grand_total) for r in subcontracting_pis)

	project_doc.total_paid_amount = _sum(
		"Payment Entry",
		{"project": project_doc.name, "docstatus": 1, "payment_type": "Pay"},
		"paid_amount",
	)

	if subcontracting_pi_names:
		project_doc.total_subcontractor_paid_amount = flt(frappe.db.sql(
			"""
			select sum(pe.paid_amount)
			from `tabPayment Entry` pe
			where pe.project = %(project)s and pe.docstatus = 1 and pe.payment_type = 'Pay'
				and pe.party_type = 'Supplier'
				and exists (
					select 1 from `tabPayment Entry Reference` per
					where per.parent = pe.name and per.reference_doctype = 'Purchase Invoice'
						and per.reference_name in %(pis)s
				)
			""",
			{"project": project_doc.name, "pis": subcontracting_pi_names},
		)[0][0])
	else:
		project_doc.total_subcontractor_paid_amount = 0

	project_doc.total_purchases_amount = _sum(
		"Purchase Receipt", {"project": project_doc.name, "docstatus": 1}, "grand_total"
	)
	project_doc.total_employee_advances_paid = _sum(
		"Employee Advance", {"project": project_doc.name}, "paid_amount"
	)
	project_doc.total_other_payments = (
		project_doc.total_paid_amount
		- project_doc.total_subcontractor_paid_amount
		- project_doc.total_employee_advances_paid
	)
	project_doc.total_liabilities = _sum(
		"Purchase Invoice", {"project": project_doc.name, "docstatus": 1}, "outstanding_amount"
	)

	paid = project_doc.total_paid_amount
	project_doc.total_subcontractor_paid_percent = (
		project_doc.total_subcontractor_paid_amount / paid * 100 if paid else 0
	)
	project_doc.total_purchases_percent = project_doc.total_purchases_amount / paid * 100 if paid else 0
	project_doc.total_employee_advances_percent = (
		project_doc.total_employee_advances_paid / paid * 100 if paid else 0
	)
	project_doc.total_other_payments_percent = (
		project_doc.total_other_payments / paid * 100 if paid else 0
	)


def _sum(doctype, filters, fieldname):
	rows = frappe.get_all(doctype, filters=filters, fields=[f"sum({fieldname}) as total"])
	return flt(rows[0].total) if rows and rows[0].total else 0


# ---------- FR-11: payments collected, proportional split ----------

def _payments_collected_by_sales_order(sales_orders):
	"""One Sales Invoice can carry items from more than one Sales Order; a
	Payment Entry always references the whole invoice. Splits each
	invoice's allocated payments across its Sales Orders by each order's
	share of the invoice's grand_total (FR-11)."""
	if not sales_orders:
		return {}

	item_rows = frappe.db.sql(
		"""
		select sii.parent as sales_invoice, sii.sales_order, sum(sii.amount) as so_amount
		from `tabSales Invoice Item` sii
		inner join `tabSales Invoice` si on si.name = sii.parent
		where sii.sales_order in %(sos)s and si.docstatus = 1
		group by sii.parent, sii.sales_order
		""",
		{"sos": sales_orders},
		as_dict=True,
	)
	if not item_rows:
		return {}

	invoice_names = list({r.sales_invoice for r in item_rows})
	invoice_totals = {
		d.name: flt(d.grand_total)
		for d in frappe.get_all(
			"Sales Invoice", filters={"name": ["in", invoice_names]}, fields=["name", "grand_total"]
		)
	}
	allocated = {
		d.reference_name: flt(d.allocated)
		for d in frappe.db.sql(
			"""
			select per.reference_name, sum(per.allocated_amount) as allocated
			from `tabPayment Entry Reference` per
			inner join `tabPayment Entry` pe on pe.name = per.parent
			where per.reference_doctype = 'Sales Invoice'
				and per.reference_name in %(sis)s
				and pe.docstatus = 1
			group by per.reference_name
			""",
			{"sis": invoice_names},
			as_dict=True,
		)
	}

	result = {}
	for row in item_rows:
		invoice_total = invoice_totals.get(row.sales_invoice) or 0
		if not invoice_total:
			continue
		share = flt(row.so_amount) / invoice_total
		invoice_allocated = allocated.get(row.sales_invoice) or 0
		result[row.sales_order] = result.get(row.sales_order, 0) + invoice_allocated * share
	return result


def _invoiced_amount_by_sales_order(sales_orders):
	if not sales_orders:
		return {}
	rows = frappe.db.sql(
		"""
		select sii.sales_order, sum(sii.amount) as amount
		from `tabSales Invoice Item` sii
		inner join `tabSales Invoice` si on si.name = sii.parent
		where sii.sales_order in %(sos)s and si.docstatus = 1
		group by sii.sales_order
		""",
		{"sos": sales_orders},
		as_dict=True,
	)
	return {r.sales_order: flt(r.amount) for r in rows}


def _progress_by_sales_order(sales_orders):
	"""FR-05 numerator: per Sales Order Item, its latest Invoiced Client
	Progress Invoice's cumulative_qty_complete (cumulative -> max is
	latest, same reasoning client_progress_invoice.get_sales_order_items()
	already relies on) times rate, summed per Sales Order."""
	if not sales_orders:
		return {}
	rows = frappe.db.sql(
		"""
		select soi.parent as sales_order, soi.rate as rate,
			max(cpii.cumulative_qty_complete) as cum_qty
		from `tabSales Order Item` soi
		inner join `tabClient Progress Invoice Item` cpii on cpii.sales_order_item = soi.name
		inner join `tabClient Progress Invoice` cpi on cpi.name = cpii.parent
		where soi.parent in %(sos)s and cpi.docstatus = 1
		group by soi.name, soi.parent, soi.rate
		""",
		{"sos": sales_orders},
		as_dict=True,
	)
	result = {}
	for row in rows:
		result[row.sales_order] = result.get(row.sales_order, 0) + flt(row.cum_qty) * flt(row.rate)
	return result


# ---------- FR-04/05/06: Project Sales Order Detail (value fields only) ----------

def _update_sales_order_details_rows(project_doc, sales_orders):
	"""Only updates value fields on existing rows, matched by
	row.sales_order - row creation/identity is Project Tender's own job
	(FR-04, at Won-automation time), never this rollup's."""
	progress_by_so = _progress_by_sales_order(sales_orders)
	invoiced_by_so = _invoiced_amount_by_sales_order(sales_orders)
	payments_by_so = project_doc.flags.get("_payments_by_so") or {}

	for row in project_doc.sales_order_details:
		so = row.sales_order
		if not so:
			continue
		so_grand_total = flt(frappe.db.get_value("Sales Order", so, "grand_total"))
		progress_amount = flt(progress_by_so.get(so))
		invoiced_amount = flt(invoiced_by_so.get(so))
		row.progress_amount = progress_amount
		row.progress_percentage = progress_amount / so_grand_total * 100 if so_grand_total else 0
		row.invoiced_amount = invoiced_amount
		row.invoiced_percentage = invoiced_amount / so_grand_total * 100 if so_grand_total else 0
		row.payments_collected = flt(payments_by_so.get(so))


# ---------- FR-13: Project Cost Billing Detail (full rebuild) ----------

def _tender_by_project_boq_item(project_name):
	"""Project BOQ Item autonames via "autoincrement" - frappe.db.sql
	returns its `name` as a Python int, while a Link field value pointing
	at it (custom_project_work_item) always comes back as str. Keys are
	normalized to str on both sides so lookups actually match."""
	rows = frappe.db.sql(
		"""
		select pbi.name as project_boq_item, tbi.parent as tender
		from `tabProject BOQ Item` pbi
		inner join `tabTender BOQ Item` tbi on tbi.name = pbi.source_tender_boq_item
		where pbi.boq_proj = %s
		""",
		project_name,
		as_dict=True,
	)
	return {str(r.project_boq_item): r.tender for r in rows}


def _purchases_by_tender(project_name, boq_item_to_tender):
	if not boq_item_to_tender:
		return {}
	rows = frappe.db.sql(
		"""
		select pri.custom_project_work_item as boq_item, sum(pri.amount) as amount
		from `tabPurchase Receipt Item` pri
		inner join `tabPurchase Receipt` pr on pr.name = pri.parent
		where pr.project = %(project)s and pr.docstatus = 1
			and pri.custom_project_work_item in %(boq_items)s
		group by pri.custom_project_work_item
		""",
		{"project": project_name, "boq_items": list(boq_item_to_tender)},
		as_dict=True,
	)
	result = {}
	for row in rows:
		tender = boq_item_to_tender.get(row.boq_item)
		if tender:
			result[tender] = result.get(tender, 0) + flt(row.amount)
	return result


def _subcontracting_by_tender(boq_item_to_tender, subcontracting_pi_names):
	if not boq_item_to_tender or not subcontracting_pi_names:
		return {}
	rows = frappe.db.sql(
		"""
		select pii.custom_project_work_item as boq_item, sum(pii.amount) as amount
		from `tabPurchase Invoice Item` pii
		where pii.parent in %(pis)s
			and pii.custom_project_work_item in %(boq_items)s
		group by pii.custom_project_work_item
		""",
		{"pis": subcontracting_pi_names, "boq_items": list(boq_item_to_tender)},
		as_dict=True,
	)
	result = {}
	for row in rows:
		tender = boq_item_to_tender.get(row.boq_item)
		if tender:
			result[tender] = result.get(tender, 0) + flt(row.amount)
	return result


def _liabilities_by_tender(project_name, boq_item_to_tender):
	"""Per-invoice proration of a genuinely invoice-level field
	(outstanding_amount) across its own lines by each line's share of the
	invoice's grand_total - not a fabricated project-wide split. Covers
	both material and subcontracting invoices (both are Purchase Invoice
	Item rows), matching the header total_liabilities formula's own
	"material + subcontracting" scope."""
	if not boq_item_to_tender:
		return {}
	rows = frappe.db.sql(
		"""
		select pii.custom_project_work_item as boq_item, pii.parent as invoice,
			sum(pii.amount) as line_amount
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent
		where pi.project = %(project)s and pi.docstatus = 1
			and pii.custom_project_work_item in %(boq_items)s
		group by pii.custom_project_work_item, pii.parent
		""",
		{"project": project_name, "boq_items": list(boq_item_to_tender)},
		as_dict=True,
	)
	if not rows:
		return {}
	invoice_names = list({r.invoice for r in rows})
	invoice_info = {
		d.name: d
		for d in frappe.get_all(
			"Purchase Invoice",
			filters={"name": ["in", invoice_names]},
			fields=["name", "grand_total", "outstanding_amount"],
		)
	}
	result = {}
	for row in rows:
		inv = invoice_info.get(row.invoice)
		if not inv or not flt(inv.grand_total):
			continue
		share = flt(row.line_amount) / flt(inv.grand_total)
		tender = boq_item_to_tender.get(row.boq_item)
		if tender:
			result[tender] = result.get(tender, 0) + share * flt(inv.outstanding_amount)
	return result


def _rebuild_cost_billing_details(project_doc, direct_cost_rows):
	"""No user-editable fields on this table (FR-13) - full rebuild each
	call avoids stale/duplicate rows."""
	boq_item_to_tender = _tender_by_project_boq_item(project_doc.name)
	subcontracting_pi_names = project_doc.flags.get("_subcontracting_pi_names") or []
	purchases_by_tender = _purchases_by_tender(project_doc.name, boq_item_to_tender)
	subcontracting_by_tender = _subcontracting_by_tender(boq_item_to_tender, subcontracting_pi_names)
	liabilities_by_tender = _liabilities_by_tender(project_doc.name, boq_item_to_tender)
	payments_by_so = project_doc.flags.get("_payments_by_so") or {}
	invoiced_by_so = _invoiced_amount_by_sales_order(
		[r.sales_order for r in direct_cost_rows if r.sales_order]
	)

	project_doc.set("cost_billing_details", [])
	for row in direct_cost_rows:
		if not row.sales_order:
			continue
		so_grand_total = flt(frappe.db.get_value("Sales Order", row.sales_order, "grand_total"))
		invoiced_amount = flt(invoiced_by_so.get(row.sales_order))
		payments_collected = flt(payments_by_so.get(row.sales_order))
		project_doc.append("cost_billing_details", {
			"tender": row.tender,
			"sales_order": row.sales_order,
			"total_direct_cost": flt(row.tender_total),
			"total_sales_amount": so_grand_total,
			"invoiced_amount": invoiced_amount,
			"invoiced_percentage": invoiced_amount / so_grand_total * 100 if so_grand_total else 0,
			"payments_collected": payments_collected,
			"payments_collected_percentage": (
				payments_collected / invoiced_amount * 100 if invoiced_amount else 0
			),
			"total_purchases_amount": flt(purchases_by_tender.get(row.tender)),
			"total_subcontracting_amount": flt(subcontracting_by_tender.get(row.tender)),
			"liabilities": flt(liabilities_by_tender.get(row.tender)),
		})
