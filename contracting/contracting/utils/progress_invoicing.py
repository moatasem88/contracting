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


# own_doctype -> the WHERE clause fragment that means "already committed" for
# that doctype. Both Contractor Invoice and Client Progress Invoice are now
# is_submittable (docstatus=1 is "committed") - Client Progress Invoice's own
# status field stays as a display-layer Select on top, no longer what this
# lookup keys off.
_PRIOR_PROGRESS_FILTER = {
	"Contractor Invoice": "parent_doc.docstatus = 1",
	"Client Progress Invoice": "parent_doc.docstatus = 1",
}


def _get_prior_item_progress(own_doctype, link_field, source_name, exclude_name):
	"""How much of `source_name` was already claimed by an earlier committed
	sibling of `own_doctype` - shared by compute_progress_invoice_items (at
	save time) and get_item_progress_context (the live client-side lookup),
	so the two can never drift on what "prior" means.

	What counts as "committed" differs by doctype (see _PRIOR_PROGRESS_FILTER)
	- Contractor Invoice reads docstatus, Client Progress Invoice still reads
	its own status field - so this branches per own_doctype rather than
	assuming a single shared column."""
	prior = frappe.db.sql(
		"""
		select max(item.cumulative_qty_complete) as max_qty,
		       coalesce(sum(item.this_period_amount), 0) as invoiced_amount
		from `tab{child_dt} Item` item
		inner join `tab{parent_dt}` parent_doc on parent_doc.name = item.parent
		where item.{link_field} = %s and {filter} and parent_doc.name != %s
		""".format(
			child_dt=own_doctype, parent_dt=own_doctype, link_field=link_field,
			filter=_PRIOR_PROGRESS_FILTER[own_doctype],
		),
		(source_name, exclude_name or ""),
		as_dict=True,
	)
	prior = prior[0] if prior else frappe._dict()
	return flt(prior.max_qty), flt(prior.invoiced_amount)


def _get_prior_condition_progress(contract_item, payment_condition, exclude_name):
	"""Condition Progress's own shape of the same lookup - grouped by
	(contract_item, payment_condition) instead of just contract_item.
	Contractor-Invoice-only (Client Progress Invoice has no condition_progress
	table), so the child doctype - and now the docstatus filter - are
	hardcoded rather than parametrized."""
	prior = frappe.db.sql(
		"""
		select max(item.cumulative_qty_complete) as max_qty,
		       coalesce(sum(item.this_period_amount), 0) as invoiced_amount
		from `tabContractor Invoice Condition Progress` item
		inner join `tabContractor Invoice` parent_doc on parent_doc.name = item.parent
		where item.contract_item = %s and item.payment_condition = %s
		  and parent_doc.docstatus = 1 and parent_doc.name != %s
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

	fields_to_fetch = ["qty", rate_field, "parent"]
	if own_doctype == "Contractor Invoice":
		# Contractor Invoice Item is the only source-fed table with a
		# description column (FR-10) - Tender Item / Sales Order Item
		# (Client Progress Invoice's own sources) are left untouched.
		fields_to_fetch.append("description")

	for row in doc.items:
		source_name = row.get(link_field)
		ti = frappe.db.get_value(source_doctype, source_name, fields_to_fetch, as_dict=True)
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
		if own_doctype == "Contractor Invoice":
			row.description = ti.get("description")

		total_this_period += row.this_period_amount
		total_retention += row.retention_amount

	doc.total_this_period = total_this_period
	doc.total_retention_held = total_retention
	return total_this_period, total_retention


def refresh_cpi_invoiced_tracking(doc, method=None):
	"""Sales Invoice on_submit/on_cancel hook (FR-08/09/10). For every item
	row on this invoice carrying so_detail, refreshes invoiced_qty/
	invoiced_percent/outstanding_qty on every Client Progress Invoice Item
	row (across every *submitted* CPI) sharing that sales_order_item - a
	direct field write via frappe.db.set_value, same technique as
	overrides/sales_invoice.py's update_billing_status_in_dn (the parent CPI
	may itself already be submitted, so a full doc.save() isn't an option).
	Recomputed fresh each time (not incrementally adjusted), so it stays
	correct through cancellations/amendments (FR-09)."""
	so_details = {d.so_detail for d in doc.items if d.so_detail}
	if not so_details:
		return

	for soi in so_details:
		invoiced_qty = flt(frappe.db.sql(
			"""
			select coalesce(sum(sii.qty), 0)
			from `tabSales Invoice Item` sii
			inner join `tabSales Invoice` si on si.name = sii.parent
			where sii.so_detail = %s and si.docstatus = 1
			""",
			soi,
		)[0][0])

		for row in frappe.get_all(
			"Client Progress Invoice Item",
			filters={"sales_order_item": soi},
			fields=["name", "parent", "qty_allocated", "cumulative_qty_complete"],
		):
			if frappe.db.get_value("Client Progress Invoice", row.parent, "docstatus") != 1:
				continue
			invoiced_percent = (invoiced_qty / row.qty_allocated * 100.0) if row.qty_allocated else 0.0
			outstanding_qty = max(0.0, flt(row.cumulative_qty_complete) - invoiced_qty)
			frappe.db.set_value(
				"Client Progress Invoice Item",
				row.name,
				{
					"invoiced_qty": invoiced_qty,
					"invoiced_percent": invoiced_percent,
					"outstanding_qty": outstanding_qty,
				},
				update_modified=False,
			)


@frappe.whitelist()
def get_sales_order_item_billing_context(sales_order_item, exclude_invoice=None):
	"""Ordered qty and already-invoiced qty (excluding this draft invoice's
	own not-yet-submitted rows) for one Sales Order Item - shared by
	CustomSalesInvoice.validate()'s percentage-billing ceiling check and
	sales_invoice.js's live qty/percent recompute, so server and client
	can't drift on what "available to invoice" means (the exact failure
	mode this module's own _get_prior_item_progress docstring already warns
	about for a different pair of callers)."""
	ordered_qty = flt(frappe.db.get_value("Sales Order Item", sales_order_item, "qty"))
	invoiced_qty = flt(frappe.db.sql(
		"""
		select coalesce(sum(sii.qty), 0)
		from `tabSales Invoice Item` sii
		inner join `tabSales Invoice` si on si.name = sii.parent
		where sii.so_detail = %s and si.docstatus = 1 and si.name != %s
		""",
		(sales_order_item, exclude_invoice or ""),
	)[0][0])
	return {
		"ordered_qty": ordered_qty,
		"invoiced_qty_excluding_this_draft": invoiced_qty,
	}


@frappe.whitelist()
def get_invoiced_qty_for_work_item(work_item):
	"""FR-07/FR-27: how much of one work item has already been invoiced,
	across every submitted (docstatus=1) Contractor Invoice - shown next to
	Available Qty in the "Select Work Item" dialog so a user isn't picking
	blind.
	"""
	return flt(frappe.db.sql(
		"""
		select coalesce(sum(cii.this_period_qty), 0)
		from `tabContractor Invoice Item` cii
		inner join `tabContractor Invoice` ci on ci.name = cii.parent
		inner join `tabContractor Contract Item` cc on cc.name = cii.contract_item
		where cc.work_item = %(work_item)s and ci.docstatus = 1
		""",
		{"work_item": work_item},
	)[0][0])


@frappe.whitelist()
def get_item_progress_context(contract_item, own_doctype, doc_name=None):
	"""Live client-side counterpart to compute_progress_invoice_items's
	per-row lookups: everything contractor_invoice.js needs to know before
	it can derive cumulative_qty_complete/percent_complete/this_period_qty
	from one another and recompute amounts, without waiting for a save."""
	ci = frappe.db.get_value(
		"Contractor Contract Item", contract_item, ["qty", "unit_price", "description"], as_dict=True
	)
	if not ci:
		frappe.throw(_("Invalid Contractor Contract Item reference: {0}").format(contract_item))

	prior_max_qty, prior_invoiced_amount = _get_prior_item_progress(
		own_doctype, SOURCE_LINK_FIELD["Contractor Contract Item"], contract_item, doc_name
	)
	return {
		"qty_allocated": ci.qty or 0,
		"rate": ci.unit_price or 0,
		"description": ci.description,
		"prior_max_qty": prior_max_qty,
		"prior_invoiced_amount": prior_invoiced_amount,
	}


@frappe.whitelist()
def get_condition_seed_rows(contractor_contract):
	"""FR-03/04: every (contract_item, payment_condition) pair for a contract
	that carries Payment Conditions - what contractor_invoice.js seeds
	condition_progress with the moment such a contract is chosen, so the user
	never has to hand-build the cross product themselves. [] when the
	contract carries no Payment Conditions at all (a plain contract).

	FR-21: each contract line uses its own work-item-scoped override
	Payment Condition set when one exists for it; otherwise it falls back to
	the contract-wide (work_item blank) rows. Without this, a contract that
	combines a contract-wide default with a per-work-item override would
	double-assign both sets to the overridden line."""
	if not contractor_contract:
		return []

	return frappe.db.sql(
		"""
		select cci.name as contract_item, ccpc.name as payment_condition
		from `tabContractor Contract Item` cci
		inner join `tabContractor Contract Payment Condition` ccpc
			on ccpc.parent = cci.parent
			and (
				ccpc.work_item = cci.work_item
				or (
					ifnull(ccpc.work_item, '') = ''
					and not exists (
						select 1 from `tabContractor Contract Payment Condition` override
						where override.parent = cci.parent and override.work_item = cci.work_item
					)
				)
			)
		where cci.parent = %(contract)s
		order by cci.idx, ccpc.idx
		""",
		{"contract": contractor_contract},
		as_dict=True,
	)


@frappe.whitelist()
def get_condition_progress_context(contract_item, payment_condition, doc_name=None):
	"""Condition Progress's counterpart to get_item_progress_context - the
	ceiling is the contract line's own full qty, shared independently by
	every condition on it (FR-01), not scaled by this condition's percent."""
	ci = frappe.db.get_value(
		"Contractor Contract Item", contract_item, ["qty", "unit_price", "description"], as_dict=True
	)
	if not ci:
		frappe.throw(_("Invalid Contractor Contract Item reference: {0}").format(contract_item))

	condition = frappe.db.get_value(
		"Contractor Contract Payment Condition", payment_condition, ["condition", "percent", "work_item"], as_dict=True
	)
	if not condition:
		frappe.throw(_("Invalid Payment Condition reference: {0}").format(payment_condition))

	prior_max_qty, prior_invoiced_amount = _get_prior_condition_progress(
		contract_item, payment_condition, doc_name
	)
	return {
		"qty_allocated": flt(ci.qty),
		"rate": ci.unit_price or 0,
		"description": ci.description,
		"condition_percent": flt(condition.percent),
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
			"Contractor Contract Item", row.contract_item,
			["qty", "unit_price", "parent", "work_item", "description"], as_dict=True
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
			["condition", "percent", "parent", "work_item"], as_dict=True
		)
		if not condition:
			frappe.throw(_("Row #{0}: invalid Payment Condition reference.").format(row.idx))
		if condition.parent != doc.contractor_contract:
			frappe.throw(
				_("Row #{0}: this Payment Condition belongs to {1}, not {2}.").format(
					row.idx, frappe.bold(condition.parent), frappe.bold(doc.contractor_contract)
				)
			)
		if condition.work_item and condition.work_item != ci.work_item:
			# FR-22: defense-in-depth - a work-item-scoped condition must
			# only ever be paired with a contract_item of that same work
			# item. get_condition_seed_rows (FR-21) never produces this
			# combination itself; this guards the API/manual-entry path.
			frappe.throw(
				_("Row #{0}: this Payment Condition belongs to work item {1}, not {2}.").format(
					row.idx, frappe.bold(condition.work_item), frappe.bold(ci.work_item)
				)
			)

		row.condition_label = condition.condition
		row.description = ci.description
		row.condition_percent = flt(condition.percent)
		row.rate = ci.unit_price or 0
		# FR-01/02: every condition on a line shares the same, full ceiling
		# (the contract line's own qty), independently of every other
		# condition on it - not scaled by this condition's own percent.
		row.qty_allocated = flt(ci.qty)

		if row.cumulative_qty_complete > row.qty_allocated + 1e-6:
			frappe.throw(
				_("Row #{0}: cumulative quantity ({1}) cannot exceed the contract line's contracted "
				  "quantity ({2}).").format(row.idx, row.cumulative_qty_complete, row.qty_allocated)
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
		# FR-03: the condition's percent scales the amount, not the ceiling.
		cumulative_amount = row.cumulative_qty_complete * row.rate * row.condition_percent / 100.0
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
			"description": ci.description,
		})
		acc["qty_allocated"] = flt(ci.qty)
		# FR-07: value-weighted equivalent qty - each condition's own
		# quantity weighted by its percent share, so the rollup stays within
		# [0, line_qty] regardless of how many independent conditions a line
		# carries (every line's conditions already sum to 100%, enforced by
		# SubcontractorContract.validate_payment_conditions).
		weight = flt(condition.percent) / 100.0
		acc["cumulative_qty_complete"] += row.cumulative_qty_complete * weight
		acc["this_period_qty"] += row.this_period_qty * weight
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
		item_row.description = acc["description"]
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
