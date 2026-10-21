import frappe
from frappe import _
from frappe.utils import flt

from contracting.contracting.api.external._common import (
	attach,
	check_modified,
	find_action,
	list_view,
	load,
	parse,
	positive,
	prepare_save,
	preview_action,
	require_draft,
	rows_by_name,
	run_action,
	view,
)
from contracting.contracting.doctype.contractor_invoice import contractor_invoice as controller
from contracting.contracting.utils import progress_invoicing

DOCTYPE = "Contractor Invoice"
CONTRACT_DOCTYPE = "Subcontractor Contract"
LIST_FIELDS = (
	"name", "contractor_contract", "project", "subcontractor", "invoice_number", "invoice_date", "workflow_state",
	"docstatus", "total_this_period", "net_payable", "modified",
)
PROGRESS_FIELDS = ("cumulative_qty_complete", "percent_complete", "this_period_qty")

# The one transition that submits the invoice (on_submit creates its Purchase
# Invoice); desk asks the Accounts Manager to confirm billing accounts first.
BILLING_STATE, BILLING_ACTION = "Pending Accounts Manager Approval", "Approve"


def list_contractor_invoices(subcontractor_contract=None, workflow_state=None, filters=None, search=None,
							 limit=None, start=None):
	filters = parse(filters, {}) or {}
	if subcontractor_contract:
		filters["contractor_contract"] = subcontractor_contract
	if workflow_state:
		filters["workflow_state"] = workflow_state
	return list_view(DOCTYPE, LIST_FIELDS, filters, search, ("name", "contractor_contract", "subcontractor"),
					 limit, start)


def get_contractor_invoice_view(name):
	return _view(load(DOCTYPE, name))


def list_invoiceable_contracts(project, search=None, limit=None, start=None):
	"""The desk contract picker (contract_query): Approved, open, with
	something left to invoice. Site Engineers raise invoices without read
	access to the contract itself, exactly as in desk."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	rows = controller.contract_query(DOCTYPE, search or "", "name", int(start or 0), min(int(limit or 20), 100),
									 {"project": project})
	return [
		{"name": r[0], "contract_title": r[1], "contractor_name": r[2], "project": r[3]}
		for r in rows
	]


def list_invoiceable_lines(subcontractor_contract):
	"""The desk line picker (contract_item_query) for plain contracts."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	return [
		{"contract_item": r[0], "description": r[1], "qty": r[2], "uom": r[3]}
		for r in controller.contract_item_query(DOCTYPE, "", "name", 0, 1000, {"parent": subcontractor_contract})
	]


def create_contractor_invoice_draft(subcontractor_contract, invoice_date=None):
	frappe.has_permission(DOCTYPE, "create", throw=True)
	contract = frappe.db.get_value(
		CONTRACT_DOCTYPE, subcontractor_contract, ["name", "project", "docstatus", "is_closed"], as_dict=True
	)
	if not contract:
		frappe.throw(_("Subcontractor Contract {0} not found").format(subcontractor_contract), frappe.DoesNotExistError)
	if contract.docstatus != 1:
		frappe.throw(_("Contract must be Approved before invoicing."))
	if contract.is_closed:
		frappe.throw(_("Contract {0} is closed.").format(contract.name))
	if not list_invoiceable_lines(contract.name):
		frappe.throw(_("Contract {0} has nothing left to invoice.").format(contract.name))

	doc = frappe.new_doc(DOCTYPE)
	doc.update({
		"project": contract.project,
		"contractor_contract": contract.name,
		"invoice_date": invoice_date,
	})
	# Contracts with Payment Conditions are billed per condition: seed every
	# (line, condition) pair plus one rollup item row per line, as
	# contractor_invoice.js does. A plain contract starts empty - the user
	# adds lines (add_contractor_invoice_lines), confirmed 2026-10-20.
	pairs = progress_invoicing.get_condition_seed_rows(contract.name)
	seen = []
	for pair in pairs:
		context = progress_invoicing.get_condition_progress_context(pair.contract_item, pair.payment_condition)
		doc.append("condition_progress", {
			"contract_item": pair.contract_item,
			"payment_condition": pair.payment_condition,
			"cumulative_qty_complete": context["prior_max_qty"],
		})
		if pair.contract_item not in seen:
			seen.append(pair.contract_item)
	for contract_item in seen:
		doc.append("items", {"contract_item": contract_item})
	doc.insert()
	return _view(doc)


def add_contractor_invoice_lines(name, modified, contract_items):
	doc = _editable(name, modified)
	if _uses_conditions(doc):
		frappe.throw(_("This contract is billed by Payment Condition; its lines are seeded automatically."))
	contract_items = parse(contract_items, []) or []
	eligible = {row["contract_item"] for row in list_invoiceable_lines(doc.contractor_contract)}
	present = {row.contract_item for row in doc.items}
	for contract_item in contract_items:
		if contract_item not in eligible:
			frappe.throw(_("{0} is not an open line of contract {1}.").format(contract_item, doc.contractor_contract))
		if contract_item in present:
			frappe.throw(_("{0} is already on this invoice.").format(contract_item))
	if not contract_items:
		frappe.throw(_("Nothing to add."))
	for contract_item in contract_items:
		context = progress_invoicing.get_item_progress_context(contract_item, DOCTYPE, doc.name)
		doc.append("items", {"contract_item": contract_item, "cumulative_qty_complete": context["prior_max_qty"]})
		present.add(contract_item)
	doc.save()
	return _view(doc)


def remove_contractor_invoice_lines(name, modified, row_names):
	doc = _editable(name, modified)
	if _uses_conditions(doc):
		frappe.throw(_("This contract is billed by Payment Condition; its lines can't be removed."))
	row_names = set(parse(row_names, []) or [])
	unknown = row_names - {row.name for row in doc.items}
	if unknown or not row_names:
		frappe.throw(_("Row(s) {0} are not rows of {1}.").format(", ".join(sorted(unknown)) or "-", doc.name))
	doc.set("items", [row for row in doc.items if row.name not in row_names])
	doc.save()
	return _view(doc)


def update_contractor_invoice_progress(name, modified, rows):
	"""FR-27. The server's input is cumulative_qty_complete
	(compute_progress_invoice_items / compute_condition_progress_invoice
	derive the other two and enforce the ceiling and no-regression rules);
	percent_complete or this_period_qty are accepted and converted here, the
	same way the form's live 3-way calculator converts them."""
	doc = _editable(name, modified)
	conditions = _uses_conditions(doc)
	table = "condition_progress" if conditions else "items"

	changes = []
	for row, entry in rows_by_name(doc, table, rows, PROGRESS_FIELDS, _("Progress")):
		given = [field for field in PROGRESS_FIELDS if field in entry]
		if len(given) != 1:
			frappe.throw(_("Row {0}: send exactly one of {1}.").format(row.idx, ", ".join(PROGRESS_FIELDS)))
		field = given[0]
		value = positive(entry[field], _("Row {0} {1}").format(row.idx, field), allow_zero=True)
		if conditions:
			context = progress_invoicing.get_condition_progress_context(row.contract_item, row.payment_condition,
																		  doc.name)
		else:
			context = progress_invoicing.get_item_progress_context(row.contract_item, DOCTYPE, doc.name)
		if field == "percent_complete":
			value = value / 100.0 * flt(context["qty_allocated"])
		elif field == "this_period_qty":
			value = flt(context["prior_max_qty"]) + value
		changes.append((row, value))

	if not changes:
		frappe.throw(_("Nothing to change."))
	for row, cumulative in changes:
		row.cumulative_qty_complete = cumulative
	doc.save()
	return _view(doc)


def preview_contractor_invoice_action(name, action):
	doc = load(DOCTYPE, name)
	extra = {}
	if _needs_billing_accounts(doc, action):
		extra = {"requires_billing_accounts": True, "billing_accounts": controller.get_billing_account_rows(doc.name)}
	return preview_action(doc, action, **extra)


def apply_contractor_invoice_action(name, modified, action, billing_accounts=None):
	"""FR-28. The final Approve needs the billing accounts desk asks the
	Accounts Manager to confirm; both go through in this one request, so a
	failed approval also undoes the confirmation."""
	doc = load(DOCTYPE, name)
	if _needs_billing_accounts(doc, action):
		check_modified(doc, modified)
		find_action(doc, action)
		controller.confirm_billing_accounts(doc.name, _billing_entries(doc, billing_accounts))
	return _view(run_action(doc, modified, action))


def attach_contractor_invoice_file(name, fieldname, modified, filename, content):
	return _view(attach(DOCTYPE, name, fieldname, modified, filename, content))


def _billing_entries(doc, billing_accounts):
	"""[{name, account}] from the app, resolved against the rows desk's
	popup would show for this invoice. confirm_billing_accounts writes
	entry["doctype"]/entry["name"] straight to the database, so those - and
	the Work Item Template each row bills under - always come from here,
	never from the caller."""
	given = {entry.get("name"): entry.get("account") for entry in parse(billing_accounts, []) or []}
	if not given:
		frappe.throw(_("Confirm the billing accounts (billing_accounts) to approve this invoice."))
	rows = controller.get_billing_account_rows(doc.name)
	unknown = set(given) - {row["name"] for row in rows}
	if unknown:
		frappe.throw(_("Row(s) {0} are not billed by {1}.").format(", ".join(sorted(map(str, unknown))), doc.name))
	missing = [row["name"] for row in rows if not given.get(row["name"])]
	if missing:
		frappe.throw(_("Set an account for every billed row (missing: {0}).").format(", ".join(missing)))
	for row in rows:
		frappe.get_cached_doc("Account", given[row["name"]]).check_permission("read")
	return [
		{"doctype": row["doctype"], "name": row["name"], "work_item_template": row["work_item_template"],
		 "account": given[row["name"]]}
		for row in rows
	]


def _needs_billing_accounts(doc, action):
	return doc.workflow_state == BILLING_STATE and action == BILLING_ACTION


def _editable(name, modified):
	doc = load(DOCTYPE, name, "write")
	require_draft(doc)
	prepare_save(doc, modified)
	return doc


def _uses_conditions(doc):
	return bool(doc.condition_progress) or frappe.db.exists(
		"Contractor Contract Payment Condition", {"parent": doc.contractor_contract}
	)


def _view(doc):
	return view(doc, extra_meta={"entry_table": "condition_progress" if _uses_conditions(doc) else "items"},
				extra_can={"edit_progress": bool(doc.has_permission("write")) and doc.docstatus == 0
						   and (doc.workflow_state or "") in ("", "Draft")})
