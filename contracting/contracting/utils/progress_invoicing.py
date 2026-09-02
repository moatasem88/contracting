import frappe
from frappe import _
from frappe.utils import flt


# Which link field on our own row points at the claimed-against line.
# Values are looked up from this dict, never taken from user input - they
# are interpolated into SQL below.
SOURCE_LINK_FIELD = {
	"Contractor Contract Item": "contract_item",
	"Tender Item": "tender_item",
	"Sales Order Item": "sales_order_item",
}

# Contractor Contract Item calls the unit price unit_price; the other two
# call it rate. Everything else - qty, parent - lines up across all three.
SOURCE_RATE_FIELD = {
	"Contractor Contract Item": "unit_price",
}


def _get_prior_item_progress(own_doctype, link_field, source_name, exclude_name):
	"""How much of `source_name` was already claimed by an earlier *Invoiced*
	sibling of `own_doctype` - shared by compute_progress_invoice_items (at
	save time) and get_item_progress_context (the live client-side lookup),
	so the two can never drift on what "prior" means."""
	prior = frappe.db.sql(
		"""
		select max(item.cumulative_qty_complete) as max_qty,
		       coalesce(sum(item.this_period_amount), 0) as invoiced_amount
		from `tab{child_dt} Item` item
		inner join `tab{parent_dt}` parent_doc on parent_doc.name = item.parent
		where item.{link_field} = %s and parent_doc.status = 'Invoiced' and parent_doc.name != %s
		""".format(child_dt=own_doctype, parent_dt=own_doctype, link_field=link_field),
		(source_name, exclude_name or ""),
		as_dict=True,
	)
	prior = prior[0] if prior else frappe._dict()
	return flt(prior.max_qty), flt(prior.invoiced_amount)


def _get_prior_condition_progress(contract_item, payment_condition, exclude_name):
	"""Condition Progress's own shape of the same lookup - grouped by
	(contract_item, payment_condition) instead of just contract_item.
	Contractor-Invoice-only (Client Progress Invoice has no condition_progress
	table), so the child doctype is hardcoded rather than parametrized."""
	prior = frappe.db.sql(
		"""
		select max(item.cumulative_qty_complete) as max_qty,
		       coalesce(sum(item.this_period_amount), 0) as invoiced_amount
		from `tabContractor Invoice Condition Progress` item
		inner join `tabContractor Invoice` parent_doc on parent_doc.name = item.parent
		where item.contract_item = %s and item.payment_condition = %s
		  and parent_doc.status = 'Invoiced' and parent_doc.name != %s
		""",
		(contract_item, payment_condition, exclude_name or ""),
		as_dict=True,
	)
	prior = prior[0] if prior else frappe._dict()
	return flt(prior.max_qty), flt(prior.invoiced_amount)


@frappe.whitelist()
def get_contract_retention_rate(contract_name):
	"""The contract's Retention charge row's rate, or 0 if it has none (or
	no contract is linked yet) - the single source of truth every retention
	figure (header total, per-line informational columns, live client-side
	preview) now reads from, in place of the deleted
	ContractorInvoice.retention_percent field."""
	if not contract_name:
		return 0.0

	rate = frappe.db.get_value(
		"Contractor Contract Charge",
		{
			"parent": contract_name,
			"parenttype": "Subcontractor Contract",
			"cost_category": "Retention",
			"charge_type": "On Net Total",
		},
		"rate",
	)
	return flt(rate)


def compute_progress_invoice_items(doc, own_doctype, source_doctype, source_parent=None, retention_rate=None):
	"""Shared validate() logic for Contractor Invoice and Client Progress
	Invoice: both are a cumulative-percent-complete claim against the rows
	of a source document - Subcontractor Contract's contracted_items
	(Contractor Contract Item) for Contractor Invoice, and for Client
	Progress Invoice either a Sales Order's items or, on invoices predating
	that change, a Contract Document's. own_doctype scopes the "what was
	already invoiced" lookup to sibling documents of the same type.

	source_parent is the document being claimed against. Every row has to be
	a line of it: the link fields point at child rows, whose names are opaque
	hashes shared across every contract on the site, so a row from an
	unrelated document is indistinguishable by eye and would otherwise be
	billed against this one at that other document's qty and rate.

	retention_rate is supplied by the caller rather than read off doc
	directly: Contractor Invoice has no retention field of its own anymore
	(it reads get_contract_retention_rate(contract) instead) while Client
	Progress Invoice still has its own independent retention_percent field -
	this function stays agnostic to which so the two callers can't drift
	into reading the wrong source."""

	link_field = SOURCE_LINK_FIELD[source_doctype]
	rate_field = SOURCE_RATE_FIELD.get(source_doctype, "rate")
	retention_rate = flt(retention_rate) if retention_rate is not None else 0.0
	total_this_period = 0.0
	total_retention = 0.0

	for row in doc.items:
		source_name = row.get(link_field)
		ti = frappe.db.get_value(source_doctype, source_name, ["qty", rate_field, "parent"], as_dict=True)
		if not ti:
			frappe.throw(_("Row #{0}: invalid {1} reference.").format(row.idx, _(source_doctype)))

		if source_parent and ti.parent != source_parent:
			frappe.throw(
				_("Row #{0}: this {1} belongs to {2}, not {3}.").format(
					row.idx, _(source_doctype), frappe.bold(ti.parent), frappe.bold(source_parent)
				)
			)

		row.qty_allocated = ti.qty or 0
		row.rate = ti.get(rate_field) or 0

		if row.cumulative_qty_complete > (ti.qty or 0) + 1e-6:
			frappe.throw(
				_("Row #{0}: cumulative quantity ({1}) cannot exceed the allocated quantity ({2}).").format(
					row.idx, row.cumulative_qty_complete, ti.qty
				)
			)

		prior_max_qty, prior_invoiced_amount = _get_prior_item_progress(
			own_doctype, link_field, source_name, doc.name
		)

		if row.cumulative_qty_complete + 1e-6 < prior_max_qty:
			frappe.throw(
				_(
					"Row #{0}: cumulative quantity ({1}) cannot be less than the previous invoice's "
					"cumulative quantity ({2}) - progress cannot regress."
				).format(row.idx, row.cumulative_qty_complete, prior_max_qty)
			)

		row.percent_complete = (row.cumulative_qty_complete / ti.qty * 100.0) if ti.qty else 0.0
		cumulative_amount = row.cumulative_qty_complete * row.rate
		row.cumulative_amount = cumulative_amount
		row.this_period_qty = row.cumulative_qty_complete - prior_max_qty
		row.previously_invoiced_amount = prior_invoiced_amount
		row.this_period_amount = cumulative_amount - prior_invoiced_amount
		row.retention_amount = row.this_period_amount * retention_rate / 100.0
		row.net_amount = row.this_period_amount - row.retention_amount

		total_this_period += row.this_period_amount
		total_retention += row.retention_amount

	doc.total_this_period = total_this_period
	doc.total_retention_held = total_retention
	return total_this_period, total_retention


@frappe.whitelist()
def get_invoiced_qty_for_work_item(work_item):
	"""FR-07: how much of one work item has already been invoiced, across
	every Invoiced Contractor Invoice - shown next to Available Qty in the
	"Select Work Item" dialog so a user isn't picking blind.
	"""
	return flt(frappe.db.sql(
		"""
		select coalesce(sum(cii.this_period_qty), 0)
		from `tabContractor Invoice Item` cii
		inner join `tabContractor Invoice` ci on ci.name = cii.parent
		inner join `tabContractor Contract Item` cc on cc.name = cii.contract_item
		where cc.work_item = %(work_item)s and ci.status = 'Invoiced'
		""",
		{"work_item": work_item},
	)[0][0])


@frappe.whitelist()
def get_item_progress_context(contract_item, own_doctype, doc_name=None):
	"""Live client-side counterpart to compute_progress_invoice_items's
	per-row lookups: everything contractor_invoice.js needs to know before
	it can derive cumulative_qty_complete/percent_complete/this_period_qty
	from one another and recompute amounts, without waiting for a save."""
	ci = frappe.db.get_value("Contractor Contract Item", contract_item, ["qty", "unit_price"], as_dict=True)
	if not ci:
		frappe.throw(_("Invalid Contractor Contract Item reference: {0}").format(contract_item))

	prior_max_qty, prior_invoiced_amount = _get_prior_item_progress(
		own_doctype, SOURCE_LINK_FIELD["Contractor Contract Item"], contract_item, doc_name
	)
	return {
		"qty_allocated": ci.qty or 0,
		"rate": ci.unit_price or 0,
		"prior_max_qty": prior_max_qty,
		"prior_invoiced_amount": prior_invoiced_amount,
	}


@frappe.whitelist()
def get_condition_progress_context(contract_item, payment_condition, doc_name=None):
	"""Condition Progress's counterpart to get_item_progress_context - the
	ceiling is this condition's percent share of the line's allocated qty,
	not the full line qty (mirrors compute_condition_progress_invoice)."""
	ci = frappe.db.get_value("Contractor Contract Item", contract_item, ["qty", "unit_price"], as_dict=True)
	if not ci:
		frappe.throw(_("Invalid Contractor Contract Item reference: {0}").format(contract_item))

	condition = frappe.db.get_value(
		"Contractor Contract Payment Condition", payment_condition, ["condition", "percent"], as_dict=True
	)
	if not condition:
		frappe.throw(_("Invalid Payment Condition reference: {0}").format(payment_condition))

	qty_allocated = flt(ci.qty) * flt(condition.percent) / 100.0
	prior_max_qty, prior_invoiced_amount = _get_prior_condition_progress(
		contract_item, payment_condition, doc_name
	)
	return {
		"qty_allocated": qty_allocated,
		"rate": ci.unit_price or 0,
		"prior_max_qty": prior_max_qty,
		"prior_invoiced_amount": prior_invoiced_amount,
		"condition_label": condition.condition,
	}


def compute_condition_progress_invoice(doc):
	"""FR-25-33: condition-based progress invoicing.

	Reproduces compute_progress_invoice_items's ceiling and no-regression
	guards, but per Payment Condition rather than per line: a condition's
	ceiling is its percent share of the line's own allocated qty (Contract
	Item qty x Condition percent), and no-regression is grouped by
	(contract_item, payment_condition) instead of just contract_item.

	doc.items (Contractor Invoice Item) becomes a pure rollup of the
	matching condition_progress rows per contract_item (FR-29) - it is not
	independently validated here, since the source of truth has moved to
	condition_progress.

	Contractor-Invoice-only (Client Progress Invoice has no condition_progress
	table), so - unlike compute_progress_invoice_items - this resolves its
	own retention rate directly rather than taking it as a parameter; there
	is no second caller for a parameter to disambiguate between.
	"""
	retention_rate = get_contract_retention_rate(doc.contractor_contract)
	total_this_period = 0.0
	total_retention = 0.0
	rollups = {}

	for row in doc.condition_progress:
		ci = frappe.db.get_value(
			"Contractor Contract Item", row.contract_item, ["qty", "unit_price", "parent"], as_dict=True
		)
		if not ci:
			frappe.throw(_("Row #{0}: invalid Contractor Contract Item reference.").format(row.idx))
		if ci.parent != doc.contractor_contract:
			frappe.throw(
				_("Row #{0}: this Contractor Contract Item belongs to {1}, not {2}.").format(
					row.idx, frappe.bold(ci.parent), frappe.bold(doc.contractor_contract)
				)
			)

		condition = frappe.db.get_value(
			"Contractor Contract Payment Condition", row.payment_condition,
			["condition", "percent", "parent"], as_dict=True
		)
		if not condition:
			frappe.throw(_("Row #{0}: invalid Payment Condition reference.").format(row.idx))
		if condition.parent != doc.contractor_contract:
			frappe.throw(
				_("Row #{0}: this Payment Condition belongs to {1}, not {2}.").format(
					row.idx, frappe.bold(condition.parent), frappe.bold(doc.contractor_contract)
				)
			)

		row.condition_label = condition.condition
		row.rate = ci.unit_price or 0
		row.qty_allocated = flt(ci.qty) * flt(condition.percent) / 100.0

		if row.cumulative_qty_complete > row.qty_allocated + 1e-6:
			frappe.throw(
				_("Row #{0}: cumulative quantity ({1}) cannot exceed this condition's share of the "
				  "allocated quantity ({2}).").format(row.idx, row.cumulative_qty_complete, row.qty_allocated)
			)

		prior_max_qty, prior_invoiced_amount = _get_prior_condition_progress(
			row.contract_item, row.payment_condition, doc.name
		)

		if row.cumulative_qty_complete + 1e-6 < prior_max_qty:
			frappe.throw(
				_(
					"Row #{0}: cumulative quantity ({1}) cannot be less than the previous invoice's "
					"cumulative quantity ({2}) - progress cannot regress."
				).format(row.idx, row.cumulative_qty_complete, prior_max_qty)
			)

		row.percent_complete = (
			row.cumulative_qty_complete / row.qty_allocated * 100.0
		) if row.qty_allocated else 0.0
		cumulative_amount = row.cumulative_qty_complete * row.rate
		row.this_period_qty = row.cumulative_qty_complete - prior_max_qty
		row.previously_invoiced_amount = prior_invoiced_amount
		row.this_period_amount = cumulative_amount - prior_invoiced_amount
		row.retention_amount = row.this_period_amount * retention_rate / 100.0
		row.net_amount = row.this_period_amount - row.retention_amount
		row.cumulative_amount = cumulative_amount

		total_this_period += row.this_period_amount
		total_retention += row.retention_amount

		acc = rollups.setdefault(row.contract_item, {
			"qty_allocated": 0.0, "cumulative_qty_complete": 0.0, "this_period_qty": 0.0,
			"previously_invoiced_amount": 0.0, "this_period_amount": 0.0,
			"retention_amount": 0.0, "net_amount": 0.0, "rate": row.rate,
		})
		acc["qty_allocated"] = flt(ci.qty)
		acc["cumulative_qty_complete"] += row.cumulative_qty_complete
		acc["this_period_qty"] += row.this_period_qty
		acc["previously_invoiced_amount"] += row.previously_invoiced_amount
		acc["this_period_amount"] += row.this_period_amount
		acc["retention_amount"] += row.retention_amount
		acc["net_amount"] += row.net_amount

	for item_row in doc.items:
		acc = rollups.get(item_row.contract_item)
		if not acc:
			continue
		item_row.qty_allocated = acc["qty_allocated"]
		item_row.rate = acc["rate"]
		item_row.cumulative_qty_complete = acc["cumulative_qty_complete"]
		item_row.this_period_qty = acc["this_period_qty"]
		item_row.percent_complete = (
			acc["cumulative_qty_complete"] / acc["qty_allocated"] * 100.0
		) if acc["qty_allocated"] else 0.0
		item_row.previously_invoiced_amount = acc["previously_invoiced_amount"]
		item_row.this_period_amount = acc["this_period_amount"]
		item_row.retention_amount = acc["retention_amount"]
		item_row.net_amount = acc["net_amount"]

	doc.total_this_period = total_this_period
	doc.total_retention_held = total_retention
	return total_this_period, total_retention
