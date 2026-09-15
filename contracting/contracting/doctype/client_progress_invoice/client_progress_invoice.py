import frappe
import erpnext
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today

from contracting.contracting.utils.progress_invoicing import compute_progress_invoice_items


class ClientProgressInvoice(Document):
	def validate(self):
		if self.project:
			self.refresh_progress_sales_orders()
		source_doctype = self.validate_source()
		compute_progress_invoice_items(
			self,
			"Client Progress Invoice",
			source_doctype=source_doctype,
			# Project mode's own per-row Sales-Order check lives entirely in
			# validate_source() above - compute_progress_invoice_items only
			# supports checking rows against a single parent, and a
			# project-mode claim legitimately spans several.
			source_parent=None if self.project else (self.sales_order or self.contract_document),
			# Client Progress Invoice keeps its own independent retention
			# field - contracting.utils.progress_invoicing.get_contract_
			# retention_rate is Contractor-Invoice-only (there's no
			# Subcontractor Contract in this doctype's ancestry).
			retention_rate=self.retention_percent,
		)
		self.net_receivable = self.total_this_period - self.total_retention_held

	def refresh_progress_sales_orders(self):
		"""progress_sales_orders is system-managed, never hand-edited (the
		grid is locked client-side too, see client_progress_invoice.js) - it
		always mirrors the live Project/Invoice Category match. Recomputing
		here, not just in the JS handlers, means it can't drift regardless of
		how the document was saved (UI, API, data import)."""
		self.set("progress_sales_orders", [])
		if not self.invoice_category:
			return
		for row in get_project_sales_orders(self.project, self.invoice_category):
			self.append("progress_sales_orders", row)

	def validate_source(self):
		"""Claims are against exactly one of: a Project (spanning every Sales
		Order in progress_sales_orders), a single Sales Order, or - for
		invoices raised before that change - a single Contract Document.
		Every items row has to point at the matching side, or (for Project
		mode) at a Sales Order actually in progress_sales_orders - a parent
		on a Sales Order with rows still carrying tender_item, or a row
		claiming against a Sales Order that isn't part of this claim, would
		otherwise silently compute against the wrong document."""
		if self.project and (self.sales_order or self.contract_document):
			frappe.throw(_("Set Project or Sales Order, not both."))
		if self.sales_order and self.contract_document:
			frappe.throw(_("Set either Sales Order or Contract Document, not both."))
		if not self.project and not self.sales_order and not self.contract_document:
			frappe.throw(_("Sales Order is required."))

		if self.project:
			return self.validate_project_source()

		source_doctype = "Sales Order Item" if self.sales_order else "Tender Item"
		link_field, other_field = (
			("sales_order_item", "tender_item") if self.sales_order else ("tender_item", "sales_order_item")
		)

		for row in self.items:
			if not row.get(link_field):
				# These links point at child doctypes, which Frappe cannot
				# search from a link picker - rows are only ever filled by
				# the Get Items button, so say that rather than leaving the
				# user poking at an unfillable field.
				frappe.throw(
					_("Row #{0} is not linked to a line on {1}. Remove the row and use "
						"<b>Get Items from Sales Order</b> to fill this table.").format(
						row.idx, self.sales_order or self.contract_document
					)
					if self.sales_order
					else _("Row #{0}: Tender Item is required when claiming against {1}.").format(
						row.idx, self.contract_document
					)
				)
			if row.get(other_field):
				frappe.throw(
					_("Row #{0}: cannot set both Sales Order Item and Tender Item.").format(row.idx)
				)

		return source_doctype

	def validate_project_source(self):
		if not self.progress_sales_orders:
			frappe.throw(_("No Sales Orders found for this Project/Invoice Category."))

		allowed_sales_orders = {row.sales_order for row in self.progress_sales_orders}
		for row in self.items:
			if not row.get("sales_order_item"):
				frappe.throw(
					_("Row #{0} is not linked to a line on a Sales Order. Remove the row and use "
						"<b>Get Items from Sales Order(s)</b> to fill this table.").format(row.idx)
				)
			if row.get("tender_item"):
				frappe.throw(
					_("Row #{0}: cannot set both Sales Order Item and Tender Item.").format(row.idx)
				)
			row_sales_order = row.get("sales_order") or frappe.db.get_value(
				"Sales Order Item", row.sales_order_item, "parent"
			)
			if row_sales_order not in allowed_sales_orders:
				frappe.throw(
					_("Row #{0}: this line belongs to Sales Order {1}, which is not part of this "
						"claim's Sales Orders. Re-fetch the Sales Orders or remove the row.").format(
						row.idx, frappe.bold(row_sales_order)
					)
				)

		return "Sales Order Item"

	def mark_as_measured(self):
		self.status = "Measured"
		self.measured_by = frappe.session.user
		self.save()

	def on_submit(self):
		if self.status != "Measured":
			frappe.throw(_("Only a Measured Client Progress Invoice can be submitted."))
		# db_update() (the actual DB write) already ran by the time on_submit
		# fires (frappe/model/document.py: run_post_save_methods runs after
		# db_update) - plain attribute assignment here wouldn't persist,
		# same reasoning as ContractorInvoice.on_submit()'s own db_set call.
		self.db_set("status", "Approved")
		self.db_set("approved_by", frappe.session.user)

	def on_cancel(self):
		if self.delivery_note and frappe.db.get_value("Delivery Note", self.delivery_note, "docstatus") == 1:
			frappe.throw(
				_("Cannot cancel: Delivery Note {0} is still submitted.").format(frappe.bold(self.delivery_note))
			)
		# "Cancelled" isn't one of status's own Select options (trimmed to
		# Draft/Measured/Approved) - db_set writes straight to SQL and skips
		# that validation, exactly like CustomSalesInvoice.on_cancel()'s
		# identical self.db_set("status", "Cancelled").
		self.db_set("status", "Cancelled")


@frappe.whitelist()
def get_project_sales_orders(project, invoice_category):
	"""Every submitted Sales Order on this Project whose originating Tender
	is in this trade category (FR-01) - the live match progress_sales_orders
	is kept in sync with, both from the JS handlers and from validate()."""
	return frappe.db.sql(
		"""
		select so.name as sales_order, so.tender as tender,
			t.tender_category as tender_category, so.grand_total as grand_total
		from `tabSales Order` so
		inner join `tabTender` t on t.name = so.tender
		where so.project = %(project)s and t.tender_category = %(invoice_category)s
			and so.docstatus = 1
		order by so.name
		""",
		{"project": project, "invoice_category": invoice_category},
		as_dict=True,
	)


@frappe.whitelist()
def get_sales_order_items(sales_order, client_progress_invoice=None):
	"""Rows for the Get Items button. Sales Order Item names are opaque
	hashes, so without this a user has no workable way to fill the table.
	cumulative_qty_complete is seeded from whatever was last claimed, since
	the claim is cumulative and can never regress."""
	rows = []
	for item in frappe.get_all(
		"Sales Order Item",
		filters={"parent": sales_order, "docstatus": 1},
		fields=["name", "item_code", "item_name", "description", "qty", "rate"],
		order_by="idx",
	):
		prior = frappe.db.sql(
			"""
			select max(item.cumulative_qty_complete)
			from `tabClient Progress Invoice Item` item
			inner join `tabClient Progress Invoice` parent_doc on parent_doc.name = item.parent
			where item.sales_order_item = %s and parent_doc.docstatus = 1 and parent_doc.name != %s
			""",
			(item.name, client_progress_invoice or ""),
		)
		rows.append({
			"sales_order_item": item.name,
			# frm.add_child() appends server-supplied dicts directly, which
			# never fires a fetch_from - the calling JS never sets this field
			# via a user-driven link change event, so it has to be set here.
			"sales_order": sales_order,
			"item_name": item.item_name,
			"qty_allocated": item.qty,
			"rate": item.rate,
			"cumulative_qty_complete": (prior and prior[0][0]) or 0,
		})
	return rows


def allow_cancel_when_linked_from_cpi(doc, method=None):
	"""Delivery Note on_cancel hook (not before_cancel - core Delivery
	Note.on_cancel() unconditionally overwrites self.ignore_linked_doctypes
	with its own GL Entry/Stock Ledger Entry/Repost Item Valuation tuple, so
	setting it any earlier just gets clobbered; doc_events hooks run *after*
	the class's own on_cancel within the same run_method() call - see
	Document.hook()'s compose() - and check_no_back_links_exist() only fires
	once run_method("on_cancel") fully returns, so appending here still lands
	before that check). Frappe's generic check_if_doc_is_linked() blocks
	cancelling any document a *submitted* document still Links to
	(frappe/model/delete_doc.py) - Client Progress Invoice.delivery_note is
	exactly such a field, which would otherwise deadlock against CPI's own
	on_cancel() guard (CPI won't cancel while its DN is still submitted, and
	without this, the DN won't cancel while its CPI is still submitted
	either). Exempting only this one doctype preserves the generic
	protection against every other linking doctype."""
	ignored = list(doc.get("ignore_linked_doctypes") or [])
	if "Client Progress Invoice" not in ignored:
		ignored.append("Client Progress Invoice")
	doc.ignore_linked_doctypes = ignored


@frappe.whitelist()
def mark_as_measured(name):
	doc = frappe.get_doc("Client Progress Invoice", name)
	doc.mark_as_measured()
	return doc.status


@frappe.whitelist()
def create_delivery_note(name):
	"""Create -> Delivery Note (FR-15/16/17): one Draft Delivery Note from
	exactly this CPI's own items, qty = this_period_qty, regardless of
	whether the underlying Item is stock-tracked. One-shot - guarded by
	doc.delivery_note so it can't be generated twice from the same claim."""
	doc = frappe.get_doc("Client Progress Invoice", name)

	if doc.docstatus != 1:
		frappe.throw(_("Only a submitted Client Progress Invoice can generate a Delivery Note."))
	if doc.delivery_note:
		frappe.throw(_("A Delivery Note has already been created from this document."))

	rows = [row for row in doc.items if row.this_period_qty]
	if not rows:
		frappe.msgprint(_("Nothing to deliver this period - every row's quantity is 0."))
		return None

	company = erpnext.get_default_company()
	dn = frappe.new_doc("Delivery Note")

	# Same project/sales_order/contract_document source resolution (and the
	# same currency/price-list fields) as create_sales_invoice() below -
	# Delivery Note is SellingController-based too and needs them just as
	# much, or set_missing_values() below fails resolving a price/exchange
	# rate instead of leaving them off.
	if doc.project:
		source = frappe.get_doc("Sales Order", doc.progress_sales_orders[0].sales_order)
		dn.customer = source.customer
		dn.company = source.company
		dn.currency = source.currency
		dn.selling_price_list = source.selling_price_list
		dn.project = doc.project
	elif doc.sales_order:
		source = frappe.get_doc("Sales Order", doc.sales_order)
		dn.customer = source.customer
		dn.company = source.company
		dn.currency = source.currency
		dn.selling_price_list = source.selling_price_list
		dn.project = source.project
	else:
		source = frappe.get_doc("Contract Document", doc.contract_document)
		dn.customer = source.customer
		dn.company = company
		dn.currency = erpnext.get_company_currency(company)
		dn.selling_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
		dn.project = source.project

	dn.conversion_rate = 1
	dn.plc_conversion_rate = 1

	# FR-20 precedent (contract_document.py's make_delivery_note update_item):
	# Project cost center takes priority over the Company/Cost Center default.
	cost_center = (
		(dn.project and frappe.db.get_value("Project", dn.project, "cost_center"))
		or frappe.db.get_value("Company", dn.company, "cost_center")
		or frappe.db.get_value("Cost Center", {"company": dn.company, "is_group": 0}, "name")
	)

	for row in rows:
		if row.get("sales_order_item"):
			soi = frappe.db.get_value(
				"Sales Order Item",
				row.sales_order_item,
				["item_code", "item_name", "description", "uom", "conversion_factor", "parent"],
				as_dict=True,
			)
			dn.append("items", {
				"item_code": soi.item_code,
				"item_name": soi.item_name,
				"description": soi.description,
				"qty": row.this_period_qty,
				"uom": soi.uom,
				"conversion_factor": soi.conversion_factor or 1,
				"rate": row.rate,
				"cost_center": cost_center,
				"against_sales_order": soi.parent,
				"so_detail": row.sales_order_item,
			})
		else:
			item_code = frappe.db.get_value("Tender Item", row.tender_item, "contracting_item")
			item_name = frappe.db.get_value("Item", item_code, "item_name") if item_code else row.tender_item
			dn.append("items", {
				"item_code": item_code,
				"item_name": item_name,
				"description": item_name,
				"qty": row.this_period_qty,
				"rate": row.rate,
				"uom": frappe.db.get_value("Tender Item", row.tender_item, "uom"),
				"cost_center": cost_center,
			})

	dn.run_method("set_missing_values")
	dn.insert(ignore_permissions=True)

	doc.db_set("delivery_note", dn.name)
	return dn.name


@frappe.whitelist()
def create_sales_invoice_from_backlog(name):
	"""Create -> Sales Invoice (FR-11/12): computes, per distinct Sales
	Order Item referenced by *this* CPI's own rows, the backlog across every
	submitted CPI touching that item - max(cumulative_qty_complete) across
	every submitted CPI row for it, minus invoiced_qty live-recomputed (not
	the possibly-stale stored value) - and bills whatever's still > 0 in one
	Draft Sales Invoice. Sweeps up backlog from any earlier submitted-but-
	never-billed CPI touching the same lines, not just this one (TC-05)."""
	doc = frappe.get_doc("Client Progress Invoice", name)

	if doc.docstatus != 1:
		frappe.throw(_("Only a submitted Client Progress Invoice can generate a Sales Invoice."))
	if not (doc.sales_order or doc.project):
		frappe.throw(_("Create → Sales Invoice is only available for Sales-Order-sourced claims."))

	backlog_rows = []
	seen = set()
	for row in doc.items:
		soi = row.get("sales_order_item")
		if not soi or soi in seen:
			continue
		seen.add(soi)

		max_cumulative = flt(frappe.db.sql(
			"""
			select max(item.cumulative_qty_complete)
			from `tabClient Progress Invoice Item` item
			inner join `tabClient Progress Invoice` parent_doc on parent_doc.name = item.parent
			where item.sales_order_item = %s and parent_doc.docstatus = 1
			""",
			soi,
		)[0][0])
		invoiced_qty = flt(frappe.db.sql(
			"""
			select coalesce(sum(sii.qty), 0)
			from `tabSales Invoice Item` sii
			inner join `tabSales Invoice` si on si.name = sii.parent
			where sii.so_detail = %s and si.docstatus = 1
			""",
			soi,
		)[0][0])
		outstanding = max(0.0, max_cumulative - invoiced_qty)
		if outstanding > 0:
			backlog_rows.append({"sales_order_item": soi, "qty": outstanding})

	if not backlog_rows:
		frappe.msgprint(_("Nothing outstanding to invoice — everything approved has already been billed."))
		return None

	si = create_sales_invoice(doc, backlog_rows=backlog_rows)
	return si.name


def create_sales_invoice(client_progress_invoice, backlog_rows=None):
	"""Draft Sales Invoice. For the Sales-Order-sourced branches (single-SO
	and Project mode), `backlog_rows` (from create_sales_invoice_from_backlog,
	a list of {"sales_order_item", "qty"}) is the FR-11/12 cross-CPI backlog
	- when omitted, falls back to this CPI's own this_period_qty per row.
	The legacy contract_document branch has no sales_order_item to track a
	backlog ceiling against (see Non-Goals - invoiced_qty tracking never
	populates for those rows), so it always keeps the plain this_period_qty
	behavior, unaffected by backlog_rows. Left as Draft, same reasoning as
	Contractor Invoice's Purchase Invoice generation."""
	company = erpnext.get_default_company()
	si = frappe.new_doc("Sales Invoice")

	if client_progress_invoice.project:
		# Every Sales Order in progress_sales_orders shares one Project (and,
		# by construction, one customer/company/currency/price list) - any
		# one of them is a valid source for the invoice-level fields.
		source = frappe.get_doc("Sales Order", client_progress_invoice.progress_sales_orders[0].sales_order)
		si.customer = source.customer
		si.company = source.company
		si.currency = source.currency
		si.selling_price_list = source.selling_price_list
		si.project = client_progress_invoice.project
	elif client_progress_invoice.sales_order:
		source = frappe.get_doc("Sales Order", client_progress_invoice.sales_order)
		si.customer = source.customer
		si.company = source.company
		si.currency = source.currency
		si.selling_price_list = source.selling_price_list
		si.project = source.project
	else:
		source = frappe.get_doc("Contract Document", client_progress_invoice.contract_document)
		si.customer = source.customer
		si.company = company
		si.currency = erpnext.get_company_currency(company)
		# Not frappe.db.get_value("Price List", {"selling": 1}) - no order_by,
		# so it resolves to the most recently modified selling price list.
		si.selling_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
		si.project = source.project
		si.contract_document = source.name

	si.conversion_rate = 1
	si.plc_conversion_rate = 1
	si.due_date = today()
	# FR-20: the Project's own cost center takes priority over the Company
	# default, mirroring contract_document.py's make_delivery_note/
	# make_sales_invoice update_item precedent.
	si.cost_center = (
		(si.project and frappe.db.get_value("Project", si.project, "cost_center"))
		or frappe.db.get_value("Company", si.company, "cost_center")
		or frappe.db.get_value("Cost Center", {"company": si.company, "is_group": 0}, "name")
	)

	if client_progress_invoice.sales_order or client_progress_invoice.project:
		# Project mode rows key off sales_order_item exactly like single-SO
		# mode does - client_progress_invoice.sales_order alone can't be the
		# switch here, since it's blank in project mode.
		source_rows = backlog_rows if backlog_rows is not None else [
			{"sales_order_item": row.sales_order_item, "qty": row.this_period_qty}
			for row in client_progress_invoice.items if row.this_period_qty
		]
		for row in source_rows:
			soi = frappe.db.get_value(
				"Sales Order Item",
				row["sales_order_item"],
				["item_code", "item_name", "description", "uom", "conversion_factor", "parent", "rate"],
				as_dict=True,
			)
			si.append("items", {
				"item_code": soi.item_code,
				"item_name": soi.item_name,
				# Carries the tender's own scope wording all the way through.
				"description": soi.description,
				"qty": row["qty"],
				"uom": soi.uom,
				"conversion_factor": soi.conversion_factor or 1,
				"rate": soi.rate,
				"cost_center": si.cost_center,
				# These two are what advance the Sales Order's per_billed.
				"sales_order": soi.parent,
				"so_detail": row["sales_order_item"],
			})
	else:
		# Legacy Contract Document path - no backlog concept applies (no
		# sales_order_item to track a cross-CPI ceiling against), so this
		# stays keyed off this_period_qty exactly as before.
		for row in client_progress_invoice.items:
			if not row.this_period_qty:
				continue
			item_code = frappe.db.get_value("Tender Item", row.tender_item, "contracting_item")
			item_name = frappe.db.get_value("Item", item_code, "item_name") if item_code else row.tender_item
			si.append("items", {
				"item_code": item_code,
				"item_name": item_name,
				"description": item_name,
				"qty": row.this_period_qty,
				"rate": row.rate,
				"uom": frappe.db.get_value("Tender Item", row.tender_item, "uom"),
				# A Contract Document line had no real Item to derive an
				# account from, hence the blanket fallback here.
				"income_account": frappe.db.get_single_value("Contracting Settings", "revenue_account"),
				"cost_center": si.cost_center,
			})

	# Only the Sales-Order-sourced branches (single-SO and Project mode)
	# have a taxes table to copy from - Contract Document has none (FR-17).
	if client_progress_invoice.sales_order or client_progress_invoice.project:
		if client_progress_invoice.project:
			# source is progress_sales_orders[0] (set above); check every
			# *other* order in the group for matching taxes before copying
			# source's onto the generated invoice - a Client Progress
			# Invoice against several Sales Orders can only carry one set
			# of tax rows.
			compare_fields = ["charge_type", "account_head", "rate", "included_in_print_rate"]
			source_taxes = [{f: row.get(f) for f in compare_fields} for row in source.taxes]
			for progress_row in client_progress_invoice.progress_sales_orders[1:]:
				other_taxes = frappe.get_all(
					"Sales Taxes and Charges",
					filters={"parent": progress_row.sales_order, "parenttype": "Sales Order"},
					fields=compare_fields,
					order_by="idx",
				)
				if other_taxes != source_taxes:
					frappe.throw(
						_("Sales Order {0}'s taxes and charges don't match {1}'s - align them before generating this invoice.")
						.format(progress_row.sales_order, source.name)
					)

		for row in source.taxes:
			si.append("taxes", {
				"charge_type": row.charge_type,
				"account_head": row.account_head,
				"description": row.description,
				"rate": row.rate,
				"included_in_print_rate": row.included_in_print_rate,
				"cost_center": row.cost_center or si.cost_center,
			})

	si.run_method("set_missing_values")

	# Work items carry an income account via their Item Group default, so
	# set_missing_values normally resolves a real contracting revenue
	# account - only patch the ones it couldn't.
	fallback_income_account = frappe.db.get_single_value("Contracting Settings", "revenue_account")
	for item in si.items:
		if not item.income_account:
			item.income_account = fallback_income_account

	si.insert(ignore_permissions=True)
	return si
