import frappe
import erpnext
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

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
			where item.sales_order_item = %s and parent_doc.status = 'Invoiced' and parent_doc.name != %s
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


@frappe.whitelist()
def mark_as_measured(name):
	doc = frappe.get_doc("Client Progress Invoice", name)
	doc.mark_as_measured()
	return doc.status


@frappe.whitelist()
def approve_and_invoice(name):
	"""Measured -> Approved -> generates a Draft Sales Invoice ->
	Invoiced. Mirrors Contractor Invoice's approve_and_invoice; re-running
	on an already-Invoiced document is a no-op."""
	doc = frappe.get_doc("Client Progress Invoice", name)

	if doc.status == "Invoiced":
		return doc.sales_invoice

	doc.status = "Approved"
	doc.approved_by = frappe.session.user
	doc.save()

	si = create_sales_invoice(doc)

	doc.sales_invoice = si.name
	doc.status = "Invoiced"
	doc.save()
	return si.name


def create_sales_invoice(client_progress_invoice):
	"""Draft Sales Invoice for exactly this period's claimed qty per
	line. Left as Draft, same reasoning as Contractor Invoice's Purchase
	Invoice generation."""
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
	si.cost_center = frappe.db.get_value("Company", si.company, "cost_center") or frappe.db.get_value(
		"Cost Center", {"company": si.company, "is_group": 0}, "name"
	)

	for row in client_progress_invoice.items:
		if not row.this_period_qty:
			continue

		# Project mode rows key off sales_order_item exactly like single-SO
		# mode does - client_progress_invoice.sales_order alone can't be the
		# switch here, since it's blank in project mode.
		if client_progress_invoice.sales_order or client_progress_invoice.project:
			soi = frappe.db.get_value(
				"Sales Order Item",
				row.sales_order_item,
				["item_code", "item_name", "description", "uom", "conversion_factor", "parent"],
				as_dict=True,
			)
			si.append("items", {
				"item_code": soi.item_code,
				"item_name": soi.item_name,
				# Carries the tender's own scope wording all the way through.
				"description": soi.description,
				"qty": row.this_period_qty,
				"uom": soi.uom,
				"conversion_factor": soi.conversion_factor or 1,
				"rate": row.rate,
				"cost_center": si.cost_center,
				# These two are what advance the Sales Order's per_billed.
				"sales_order": soi.parent,
				"so_detail": row.sales_order_item,
			})
		else:
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
