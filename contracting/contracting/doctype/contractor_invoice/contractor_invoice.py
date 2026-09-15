import frappe
import erpnext
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, today
from frappe.utils.file_manager import save_file

from contracting.contracting.utils.progress_invoicing import (
	compute_condition_progress_invoice,
	compute_progress_invoice_items,
	get_contract_retention_rate,
)

# Attach fields whose edit window is narrower than base write permission now
# allows (all 7 workflow roles have base `write` - see hooks.py/the doctype
# JSON - because every one of them needs it to execute their own workflow
# transition, not just to edit these two fields). The per-state `allow_edit`
# on the Workflow doctype is UI-only, so this is the real enforcement.
_ATTACHMENT_WINDOWS = {
	"invoice_attachment": ("", "Draft"),
	"checklist_attachment": ("Pending Cost Control Engineer Review",),
}


class ContractorInvoice(Document):
	def before_insert(self):
		"""FR-18/19, NFR-02: per-contract sequential numbering, computed once
		and never recomputed (see the Reopen-in-place workflow design - a
		rejected invoice is reopened in place, not re-inserted, so there is
		never a second before_insert for the "same" invoice). Locks the
		contract row first, mirroring allocation.lock_work_items's pattern,
		so two concurrent inserts against the same contract serialise instead
		of computing the same number."""
		frappe.db.sql(
			"select name from `tabSubcontractor Contract` where name=%s for update",
			(self.contractor_contract,),
		)
		self.invoice_number = frappe.db.count(
			"Contractor Invoice", {"contractor_contract": self.contractor_contract, "docstatus": 1}
		) + 1

	def validate(self):
		self._enforce_attachment_windows()

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

	def on_submit(self):
		"""FR-27: fires when the Accounts Manager's "Approve" transition
		takes this invoice from Pending Accounts Manager Approval to
		Approved - the one docstatus 0->1 hop in the workflow."""
		pi = create_purchase_invoice(self)
		self.db_set("purchase_invoice", pi.name)

	def _enforce_attachment_windows(self):
		"""FR-22/24: invoice_attachment only changes in Draft, checklist_
		attachment only in Pending Cost Control Engineer Review - mirrors
		SubcontractorContract.enforce_frozen_figures's get_doc_before_save
		pattern. Needed because base DocPerm write is granted broadly (every
		workflow role needs it to save its own transition), so without this
		any role could set either attachment at any stage via the API."""
		before = self.get_doc_before_save()
		if not before:
			return

		state = before.workflow_state or "Draft"
		for fieldname, allowed_states in _ATTACHMENT_WINDOWS.items():
			if self.get(fieldname) != before.get(fieldname) and state not in allowed_states:
				frappe.throw(
					_("{0} can only be changed while this invoice is in state {1}.").format(
						_(self.meta.get_label(fieldname)), frappe.bold(state)
					)
				)

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


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def contract_query(doctype, txt, searchfield, start, page_len, filters):
	"""FR-05/06: contract picker, scoped to the chosen Project (FR-02: blank
	project -> no results) and excluding closed / non-submitted / fully-
	invoiced contracts - AND logic across all four conditions, a contract
	failing any one is excluded. Returns contract_title/contractor_name/
	project as description columns (FR-06)."""
	filters = filters or {}
	project = filters.get("project")
	if not project:
		return []

	return frappe.db.sql(
		"""
		select distinct sc.name, sc.contract_title, sc.contractor_name, sc.project
		from `tabSubcontractor Contract` sc
		inner join `tabContractor Contract Item` cci on cci.parent = sc.name
		left join (
			select cii.contract_item, coalesce(sum(cii.this_period_qty), 0) as qty
			from `tabContractor Invoice Item` cii
			inner join `tabContractor Invoice` ci on ci.name = cii.parent
			where ci.docstatus = 1
			group by cii.contract_item
		) invoiced on invoiced.contract_item = cci.name
		where sc.project = %(project)s
		  and sc.docstatus = 1
		  and sc.is_closed = 0
		  and cci.qty > coalesce(invoiced.qty, 0)
		  and (sc.name like %(txt)s or ifnull(sc.contract_title, '') like %(txt)s
		       or ifnull(sc.contractor_name, '') like %(txt)s)
		order by sc.name
		limit %(start)s, %(page_len)s
		""",
		{"project": project, "txt": f"%{txt}%", "start": start, "page_len": page_len},
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def contract_item_query(doctype, txt, searchfield, start, page_len, filters):
	"""FR-08/09: within the chosen contract, excludes Contractor Contract
	Item rows already 100% invoiced by *submitted* Contractor Invoices only -
	a pending entry on the current in-progress draft never self-excludes,
	since only docstatus=1 invoices are counted here."""
	filters = filters or {}
	contract = filters.get("parent")
	if not contract:
		return []

	return frappe.db.sql(
		"""
		select cci.name, cci.description, cci.qty, cci.uom
		from `tabContractor Contract Item` cci
		left join (
			select cii.contract_item, coalesce(sum(cii.this_period_qty), 0) as qty
			from `tabContractor Invoice Item` cii
			inner join `tabContractor Invoice` ci on ci.name = cii.parent
			where ci.docstatus = 1
			group by cii.contract_item
		) invoiced on invoiced.contract_item = cci.name
		where cci.parent = %(contract)s
		  and cci.qty > coalesce(invoiced.qty, 0)
		  and (cci.name like %(txt)s or ifnull(cci.description, '') like %(txt)s)
		order by cci.idx
		limit %(start)s, %(page_len)s
		""",
		{"contract": contract, "txt": f"%{txt}%", "start": start, "page_len": page_len},
	)


@frappe.whitelist()
def get_totals_preview(doc):
	"""FR-15/16/17, NFR-01: live header-totals preview for an in-progress
	(possibly unsaved) document, run through the exact same code path
	validate() uses - compute_progress_invoice_items/
	compute_condition_progress_invoice/rebuild_additional_costs - so the
	preview can never drift from what save() would persist for the same row
	data. doc is the client's full frm.doc, as JSON."""
	doc = frappe.parse_json(doc) if isinstance(doc, str) else doc
	invoice = frappe.get_doc(doc)
	invoice.flags.ignore_permissions = True

	if invoice.contractor_contract and frappe.db.exists(
		"Contractor Contract Payment Condition", {"parent": invoice.contractor_contract}
	):
		compute_condition_progress_invoice(invoice)
	else:
		compute_progress_invoice_items(
			invoice,
			"Contractor Invoice",
			source_doctype="Contractor Contract Item",
			source_parent=invoice.contractor_contract,
			retention_rate=get_contract_retention_rate(invoice.contractor_contract),
		)
	invoice.rebuild_additional_costs()
	invoice.net_payable = invoice.total_this_period + invoice.total_additional_charges

	return {
		"total_this_period": invoice.total_this_period,
		"total_retention_held": invoice.total_retention_held,
		"total_additional_charges": invoice.total_additional_charges,
		"net_payable": invoice.net_payable,
		"additional_costs": [row.as_dict() for row in invoice.additional_costs],
	}


def _resolve_work_item_template(contract_item_name):
	"""Contractor Contract Item -> Tender BOQ Item -> Work Item Template V2,
	or None at any missing hop (a labor/equipment-only line has no work_item;
	a manually-entered BOQ line may have no work_item_template)."""
	work_item = frappe.db.get_value("Contractor Contract Item", contract_item_name, "work_item")
	if not work_item:
		return None
	return frappe.db.get_value("Tender BOQ Item", work_item, "work_item_template")


def _billing_rows(contractor_invoice):
	"""FR-03: the exact rows create_purchase_invoice() bills this period -
	condition_progress rows if the contract is billed per Payment Condition,
	otherwise items rows - both filtered to this_period_qty > 0. Shared by
	get_billing_account_rows() and create_purchase_invoice() so the two can
	never disagree on which rows are actually being billed."""
	if contractor_invoice.condition_progress:
		return [
			("Contractor Invoice Condition Progress", row)
			for row in contractor_invoice.condition_progress
			if row.this_period_qty
		]
	return [
		("Contractor Invoice Item", row)
		for row in contractor_invoice.items
		if row.this_period_qty
	]


_BILLING_ACCOUNT_ROLES = {"Accounts Manager", "System Manager"}


def _enforce_billing_account_access(contractor_invoice):
	"""Server-side, not merely UI visibility: the popup's own whitelisted
	endpoints are reachable by any authenticated user via the API, and
	confirm_billing_accounts writes to Work Item Template V2, which base
	DocPerm otherwise restricts to System Manager only. Mirrors
	SubcontractorContract's own guard for Material Conflict Override
	(subcontractor_contract.py) - a role check inside the whitelisted method
	itself, not just a hidden button."""
	if _BILLING_ACCOUNT_ROLES.isdisjoint(frappe.get_roles(frappe.session.user)):
		frappe.throw(_("Only an Accounts Manager may confirm billing accounts."))
	if contractor_invoice.workflow_state != "Pending Accounts Manager Approval":
		frappe.throw(_("Billing accounts can only be reviewed while this invoice is Pending Accounts Manager Approval."))


@frappe.whitelist()
def get_billing_account_rows(contractor_invoice):
	"""FR-03: backs the pre-approval confirmation popup - one row per line
	create_purchase_invoice would actually bill, each pre-filled with the
	account that would be used today (the row's own account, else its
	resolved Work Item Template's account, else blank)."""
	doc = frappe.get_doc("Contractor Invoice", contractor_invoice)
	_enforce_billing_account_access(doc)

	rows = []
	for row_doctype, row in _billing_rows(doc):
		contract_item = frappe.db.get_value(
			"Contractor Contract Item", row.contract_item, ["work_item", "description"], as_dict=True
		) or frappe._dict()
		template = _resolve_work_item_template(row.contract_item)
		template_account = frappe.db.get_value("Work Item Template V2", template, "account") if template else None

		rows.append({
			"doctype": row_doctype,
			"name": row.name,
			"work_item": contract_item.work_item,
			"description": row.description or contract_item.description,
			"work_item_template": template,
			"account": row.account or template_account,
		})
	return rows


@frappe.whitelist()
def confirm_billing_accounts(contractor_invoice, accounts):
	"""FR-07/08: writes the confirmed account onto each billed row, and onto
	its resolved Work Item Template (always overwriting), rejecting the
	whole confirm - no partial writes - if two rows sharing one template
	were given different accounts."""
	doc = frappe.get_doc("Contractor Invoice", contractor_invoice)
	_enforce_billing_account_access(doc)

	accounts = frappe.parse_json(accounts) if isinstance(accounts, str) else accounts

	template_accounts = {}
	for entry in accounts:
		if not entry.get("account"):
			frappe.throw(_("Row {0}: Account is required.").format(entry.get("name")))
		template = entry.get("work_item_template")
		if not template:
			continue
		if template in template_accounts and template_accounts[template] != entry["account"]:
			frappe.throw(
				_("Work Item Template {0} is billed with two different accounts in this invoice - "
				  "make them consistent before confirming.").format(template)
			)
		template_accounts[template] = entry["account"]

	for entry in accounts:
		frappe.db.set_value(entry["doctype"], entry["name"], "account", entry["account"])
		if entry.get("work_item_template"):
			frappe.db.set_value("Work Item Template V2", entry["work_item_template"], "account", entry["account"])


def _append_pi_item(pi, contract_item_name, qty, rate, payout_rate, payment_condition, row_account, fallback_expense_account):
	"""One PI item row for one contract line's claimed qty this period -
	shared by both create_purchase_invoice() branches below (FR-14/15), since
	they only ever differ in which table they iterate and what qty/rate/
	payout_rate/payment_condition they pass in; the lookup itself only needs
	contract_item_name, which both Contractor Invoice Item and Contractor
	Invoice Condition Progress rows expose identically.

	FR-10: expense_account resolves row_account (this invoice's own
	confirmed account for this line) -> the resolved Work Item Template's
	account -> fallback_expense_account (Contracting Settings.expense_debit_
	account, today's pre-feature behavior), in that order."""
	# A Contractor Contract Item carries no item_code of its own - it
	# allocates against a BOQ line, so the Item (when there is one) and
	# the UOM both come off that line.
	contract_item = frappe.db.get_value(
		"Contractor Contract Item", contract_item_name, ["work_item", "description", "uom"], as_dict=True
	) or frappe._dict()
	boq = frappe.db.get_value(
		"Tender BOQ Item", contract_item.work_item, ["item_code", "item_name", "uom", "work_item_template"], as_dict=True
	) if contract_item.work_item else frappe._dict()

	template_account = (
		frappe.db.get_value("Work Item Template V2", boq.work_item_template, "account")
		if boq.get("work_item_template") else None
	)
	expense_account = row_account or template_account or fallback_expense_account

	item_code = boq.item_code
	item_name = (
		frappe.db.get_value("Item", item_code, "item_name")
		if item_code
		else contract_item.description or boq.item_name or contract_item_name
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
		"qty": qty,
		"rate": rate,
		"uom": contract_item.uom or boq.uom,
		"expense_account": expense_account,
		"cost_center": pi.cost_center,
		"custom_project_work_item": project_boq_item,
		"custom_contract_item": contract_item_name,
		"custom_payment_condition": payment_condition,
		"custom_payout_rate": payout_rate,
	})


def _apply_advance_reconciliation(pi, contract_name):
	"""FR-11: pull unreconciled Subcontractor Contract advance(s) onto this
	PI before submit, oldest Payment Entry first. Each submitted Payment
	Entry Reference row against this contract already reflects only what's
	still unreconciled - core's update_reference_in_payment_entry() (called
	from reconcile_against_document(), during a prior PI's own submit)
	reduces the original row's allocated_amount in place every time a
	reconciliation happens, so no separate "already moved to an earlier PI"
	bookkeeping is needed here.

	Populating pi.advances is all this does - CustomPurchaseInvoice.on_submit()
	already calls update_against_document_in_jv() unconditionally before
	make_gl_entries(), which is core's own allocation-move/GL-repost engine
	(the same one a Purchase Order advance uses today). Relies on
	erpnext_advance_patch (contracting/__init__.py) so that engine accepts
	a Subcontractor Contract-sourced reference row.
	"""
	rows = frappe.db.sql(
		"""
		select per.name as reference_row, per.parent as payment_entry, per.allocated_amount
		from `tabPayment Entry Reference` per
		inner join `tabPayment Entry` pe on pe.name = per.parent
		where per.reference_doctype = 'Subcontractor Contract'
		  and per.reference_name = %(contract)s
		  and pe.docstatus = 1
		  and per.allocated_amount > 0
		order by pe.posting_date, pe.creation
		""",
		{"contract": contract_name},
		as_dict=True,
	)

	remaining = flt(pi.grand_total)
	applied = False
	for row in rows:
		if remaining <= 0:
			break
		take = min(flt(row.allocated_amount), remaining)
		pi.append("advances", {
			"reference_type": "Payment Entry",
			"reference_name": row.payment_entry,
			"reference_row": row.reference_row,
			"advance_amount": row.allocated_amount,
			"allocated_amount": take,
		})
		remaining -= take
		applied = True
	return applied


_ATTACHMENT_TARGET_FIELD = {
	"invoice_attachment": "custom_invoice_attachment",
	"checklist_attachment": "custom_checklist_attachment",
}


def _copy_attachments_to_purchase_invoice(contractor_invoice, pi):
	"""FR-19/20, NFR-02: best-effort copy of each attachment the source
	Contractor Invoice has - a genuinely independent File document (new
	File row, own attached_to_* pointers), not a shared URL, so the PI's
	copy is unaffected if the source is later replaced or deleted. A
	missing source attachment is a silent no-op, never an error."""
	for source_field, target_field in _ATTACHMENT_TARGET_FIELD.items():
		if not contractor_invoice.get(source_field):
			continue

		file_name = frappe.db.get_value(
			"File",
			{
				"attached_to_doctype": "Contractor Invoice",
				"attached_to_name": contractor_invoice.name,
				"attached_to_field": source_field,
			},
			"name",
		)
		if not file_name:
			continue

		source_file = frappe.get_doc("File", file_name)
		new_file = save_file(
			source_file.file_name, source_file.get_content(), "Purchase Invoice", pi.name,
			is_private=source_file.is_private, df=target_field,
		)
		pi.db_set(target_field, new_file.file_url)


def create_purchase_invoice(contractor_invoice):
	"""Purchase Invoice for exactly this period's claimed qty per line,
	against the subcontractor (Supplier) on the linked Subcontractor
	Contract. Submitted immediately, right after generation - the source
	Contractor Invoice has already gone through its own 7-stage approval
	workflow to reach this point, so there is no separate PI-side approval
	step left for a Draft to wait on; submitting immediately keeps the two
	documents' status consistent instead of leaving a second, redundant
	manual step.

	FR-14/15: when the source invoice was billed per Payment Condition
	(condition_progress has rows), this emits one PI line per condition
	actually billed that period - qty/rate/payout_rate come off that
	condition's own share, not the items rollup - so the PI shows the real
	payment-condition split rather than a blended line. A plain contract
	(no condition_progress rows) keeps today's one-row-per-items-row
	behavior, with payout_rate always 100."""
	if not _billing_rows(contractor_invoice):
		# Without this, pi.insert() below builds a Purchase Invoice with zero
		# item rows (every this_period_qty is 0), and core's own
		# calculate_taxes_and_totals() short-circuits on an empty items table
		# (taxes_and_totals.py: `if not len(self._items): return`) - leaving
		# base_grand_total as None instead of 0 and crashing set_payment_schedule()
		# with an opaque TypeError instead of a usable message.
		frappe.throw(_(
			"Nothing to bill this period - every row's This Period Qty is 0. "
			"Enter a claimed quantity before submitting this invoice."
		))
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
	pi.custom_contractor_invoice = contractor_invoice.name
	pi.subcontractor_contract = contract.name

	# The Subcontractor-series/Payable-account pairing is a manual convention
	# accounting staff already follow for subcontractor invoices (confirmed
	# against real records - a Payment Entry's own validation compares its
	# party_account against the PI's own credit_to and throws on a mismatch).
	# Contracting Settings.subcontractor_payable_account makes this generated
	# invoice follow that same convention automatically; left unset, this
	# falls back to today's behavior (default Supplier series/account).
	subcontractor_payable_account = frappe.db.get_single_value(
		"Contracting Settings", "subcontractor_payable_account"
	)
	if subcontractor_payable_account:
		pi.naming_series = "ACC-PINV-Subcontractor-.YYYY.-"
		pi.credit_to = subcontractor_payable_account
	# FR-19: the Project's own cost center takes priority over the Company
	# default, mirroring contract_document.py's make_delivery_note/
	# make_sales_invoice update_item precedent.
	pi.cost_center = (
		(contract.project and frappe.db.get_value("Project", contract.project, "cost_center"))
		or frappe.db.get_value("Company", company, "cost_center")
		or frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
	)

	# A BOQ work-item line generally has no item_code of its own (it's a
	# composite of Material/Labor/Equipment, not itself an Item - see the
	# Phase B.2 downstream note), so ERPNext can't auto-derive an expense
	# account for it the way it would for a real stocked/service Item.
	# Contracting Settings.expense_debit_account is the existing generic
	# fallback for exactly this kind of line.
	default_expense_account = frappe.db.get_single_value("Contracting Settings", "expense_debit_account")

	for row_doctype, row in _billing_rows(contractor_invoice):
		if row_doctype == "Contractor Invoice Condition Progress":
			_append_pi_item(
				pi, row.contract_item, row.this_period_qty,
				flt(row.rate) * flt(row.condition_percent) / 100.0, row.condition_percent,
				row.condition_label, row.account, default_expense_account,
			)
		else:
			_append_pi_item(
				pi, row.contract_item, row.this_period_qty, row.rate, 100, None, row.account, default_expense_account
			)

	# FR-11: Additional Costs already computed onto this invoice
	# (rebuild_additional_costs) carry straight into the PI's own native
	# taxes table - CustomPurchaseInvoice.make_tax_gl_entries already knows
	# how to post GL entries from exactly these field names. Every row gets
	# party_type/party set to the contract's own subcontractor - harmless for
	# a non-Payable/Receivable account_head (make_tax_gl_entries only reads
	# them when account_type requires a party), and required for one that is
	# (e.g. a Retention account configured as Payable), matching the same
	# Supplier already set as pi.supplier.
	for row in contractor_invoice.additional_costs:
		pi.append("taxes", {
			"charge_type": row.charge_type,
			"account_head": row.account_head,
			"category": row.category,
			"add_deduct_tax": row.add_deduct_tax,
			"rate": row.rate,
			"tax_amount": row.tax_amount,
			# FR-30: Purchase Taxes and Charges.description is mandatory:
			# Retention rows are rarely given free text, so fall through to
			# cost_category, then charge_type, rather than ever inserting blank.
			"description": row.description or row.cost_category or row.charge_type,
			"cost_center": row.cost_center or pi.cost_center,
			"party_type": "Supplier",
			"party": pi.supplier,
		})

	pi.run_method("set_missing_values")
	pi.insert(ignore_permissions=True)
	_copy_attachments_to_purchase_invoice(contractor_invoice, pi)
	# After insert, not before: grand_total isn't computed until the
	# controller's validate() runs (inside insert()), and
	# _apply_advance_reconciliation needs it to cap allocated_amount.
	# Appending to pi.advances now and letting submit() persist it mirrors
	# how "Get Advances Paid" works interactively on a Draft PI.
	advance_applied = _apply_advance_reconciliation(pi, contract.name)
	pi.submit()
	if advance_applied:
		# FR-06/FR-09: update_reference_in_payment_entry() only auto-calls
		# set_total_advance_paid() for Sales Order/Purchase Order reference
		# rows (accounts_controller.py) - Subcontractor Contract isn't in
		# that list, so this would otherwise go stale the moment any of it
		# is reconciled into an invoice.
		contract.set_total_advance_paid()
	return pi
