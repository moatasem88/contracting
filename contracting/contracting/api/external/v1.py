"""External Contracting App API, version 1 (BRD 2026-10-20).

The only entry point the external app - its screens and its AI assistant's
MCP tools alike - uses into the ERP:

	/api/method/contracting.contracting.api.external.v1.<method>

Every method runs as the calling user (the app forwards that user's own
session), so permissions, workflows, validation and the contracts
department's Tender rate hiding (FR-05) all apply exactly as in desk.
Views return {"data": ..., "meta": {doctype, name, modified, docstatus,
workflow_state, allowed_actions, can}}; writes take the `modified` the
caller last saw and fail with TimestampMismatchError if it is stale.

Methods named get_* / list_* / preview_* only read. Breaking changes go
into a new v2 module; this one stays until the app has moved over.
"""

import frappe

from contracting.contracting.api.external import (
	_addendum,
	_contract,
	_inbox,
	_invoice,
	_project_tender,
	_session,
	_tender,
)

# -- Project Tender ----------------------------------------------------------


@frappe.whitelist()
def list_project_tenders(filters=None, search=None, limit=None, start=None):
	return _project_tender.list_project_tenders(filters, search, limit, start)


@frappe.whitelist()
def get_project_tender_view(name):
	return _project_tender.get_project_tender_view(name)


@frappe.whitelist(methods=["POST"])
def update_project_tender_lines(name, modified, indirect_cost_details=None, overhead_deduction_details=None):
	return _project_tender.update_project_tender_lines(name, modified, indirect_cost_details,
													  overhead_deduction_details)


# -- Tender ------------------------------------------------------------------


@frappe.whitelist()
def list_tenders(project_tender=None, filters=None, search=None, limit=None, start=None):
	return _tender.list_tenders(project_tender, filters, search, limit, start)


@frappe.whitelist()
def get_tender_view(name):
	return _tender.get_tender_view(name)


@frappe.whitelist(methods=["POST"])
def attach_tender_file(tender, modified, filename, content):
	return _tender.attach_tender_file(tender, modified, filename, content)


@frappe.whitelist()
def list_project_tender_options(search=None, limit=None, start=None):
	return _tender.list_project_tender_options(search, limit, start)


@frappe.whitelist(methods=["POST"])
def create_tender(project_tender, tender_date=None, submission_deadline=None, tender_category=None,
				  consolidate_rates_by_item=None, safety_factor_percent=None):
	fields = {
		key: value for key, value in {
			"tender_date": tender_date, "submission_deadline": submission_deadline,
			"tender_category": tender_category, "consolidate_rates_by_item": consolidate_rates_by_item,
			"safety_factor_percent": safety_factor_percent,
		}.items() if value is not None
	}
	return _tender.create_tender(project_tender, **fields)


@frappe.whitelist()
def list_work_item_templates(search=None, limit=None, start=None):
	return _tender.list_work_item_templates(search, limit, start)


@frappe.whitelist(methods=["POST"])
def add_boq_item_from_template(tender, template, quantity, modified=None):
	return _tender.add_boq_item_from_template(tender, template, quantity, modified)


@frappe.whitelist(methods=["POST"])
def add_boq_lines(tender, lines, groups=None, modified=None):
	return _tender.add_boq_lines(tender, groups, lines, modified)


@frappe.whitelist(methods=["POST"])
def create_work_item_template_draft(template_name, uom, category=None, description=None):
	return _tender.create_work_item_template_draft(template_name, uom, category, description)


@frappe.whitelist(methods=["POST"])
def update_tender_boq_quantities(tender, modified, rows):
	return _tender.update_tender_boq_quantities(tender, modified, rows)


@frappe.whitelist(methods=["POST"])
def mark_tender_complete(tender, modified):
	return _tender.mark_tender_complete(tender, modified)


@frappe.whitelist(methods=["POST"])
def create_revised_version(tender, modified):
	return _tender.create_revised_version(tender, modified)


# -- Subcontractor Contract --------------------------------------------------


@frappe.whitelist()
def list_subcontractor_contracts(project=None, supplier=None, workflow_state=None, filters=None, search=None,
								 limit=None, start=None):
	return _contract.list_subcontractor_contracts(project, supplier, workflow_state, filters, search, limit, start)


@frappe.whitelist()
def get_subcontractor_contract_view(name):
	return _contract.get_subcontractor_contract_view(name)


@frappe.whitelist()
def list_project_options(search=None, limit=None, start=None):
	return _contract.list_project_options(search, limit, start)


@frappe.whitelist()
def list_subcontractor_options(search=None, limit=None, start=None):
	return _contract.list_subcontractor_options(search, limit, start)


@frappe.whitelist()
def get_contract_draft_options(project, contract_type, department=None):
	return _contract.get_contract_draft_options(project, contract_type, department)


@frappe.whitelist(methods=["POST"])
def create_subcontractor_contract_draft(project, supplier, contract_type, lines, contract_title=None,
										department=None, contract_date=None, price_exception_acknowledged=None):
	return _contract.create_subcontractor_contract_draft(
		project, supplier, contract_type, lines,
		**_given(contract_title=contract_title, department=department, contract_date=contract_date,
				 price_exception_acknowledged=price_exception_acknowledged),
	)


@frappe.whitelist(methods=["POST"])
def update_subcontractor_contract_lines(name, modified, rows=None, add=None, remove=None, contract_title=None,
										contract_date=None, price_exception_acknowledged=None, resources=None):
	return _contract.update_subcontractor_contract_lines(
		name, modified, rows, add, remove, resources,
		**_given(contract_title=contract_title, contract_date=contract_date,
				 price_exception_acknowledged=price_exception_acknowledged),
	)


@frappe.whitelist()
def preview_subcontractor_contract_action(name, action):
	return _contract.preview_subcontractor_contract_action(name, action)


@frappe.whitelist(methods=["POST"])
def apply_subcontractor_contract_action(name, modified, action):
	return _contract.apply_subcontractor_contract_action(name, modified, action)


@frappe.whitelist(methods=["POST"])
def attach_subcontractor_contract_file(name, fieldname, modified, filename, content):
	return _contract.attach_subcontractor_contract_file(name, fieldname, modified, filename, content)


# -- Contractor Invoice ------------------------------------------------------


@frappe.whitelist()
def list_contractor_invoices(subcontractor_contract=None, workflow_state=None, filters=None, search=None,
							 limit=None, start=None):
	return _invoice.list_contractor_invoices(subcontractor_contract, workflow_state, filters, search, limit, start)


@frappe.whitelist()
def get_contractor_invoice_view(name):
	return _invoice.get_contractor_invoice_view(name)


@frappe.whitelist()
def list_invoiceable_contracts(project, search=None, limit=None, start=None):
	return _invoice.list_invoiceable_contracts(project, search, limit, start)


@frappe.whitelist()
def list_invoiceable_lines(subcontractor_contract):
	return _invoice.list_invoiceable_lines(subcontractor_contract)


@frappe.whitelist(methods=["POST"])
def create_contractor_invoice_draft(subcontractor_contract, invoice_date=None):
	return _invoice.create_contractor_invoice_draft(subcontractor_contract, invoice_date)


@frappe.whitelist(methods=["POST"])
def add_contractor_invoice_lines(name, modified, contract_items):
	return _invoice.add_contractor_invoice_lines(name, modified, contract_items)


@frappe.whitelist(methods=["POST"])
def remove_contractor_invoice_lines(name, modified, row_names):
	return _invoice.remove_contractor_invoice_lines(name, modified, row_names)


@frappe.whitelist(methods=["POST"])
def update_contractor_invoice_progress(name, modified, rows):
	return _invoice.update_contractor_invoice_progress(name, modified, rows)


@frappe.whitelist()
def preview_contractor_invoice_action(name, action):
	return _invoice.preview_contractor_invoice_action(name, action)


@frappe.whitelist(methods=["POST"])
def apply_contractor_invoice_action(name, modified, action, billing_accounts=None):
	return _invoice.apply_contractor_invoice_action(name, modified, action, billing_accounts)


@frappe.whitelist(methods=["POST"])
def attach_contractor_invoice_file(name, fieldname, modified, filename, content):
	return _invoice.attach_contractor_invoice_file(name, fieldname, modified, filename, content)


# -- Contractor Contract Addendum --------------------------------------------


@frappe.whitelist()
def list_addenda(subcontractor_contract=None, workflow_state=None, filters=None, search=None, limit=None,
				 start=None):
	return _addendum.list_addenda(subcontractor_contract, workflow_state, filters, search, limit, start)


@frappe.whitelist()
def get_addendum_view(name):
	return _addendum.get_addendum_view(name)


@frappe.whitelist()
def get_addendum_draft_options(subcontractor_contract):
	return _addendum.get_addendum_draft_options(subcontractor_contract)


@frappe.whitelist()
def get_addendum_add_options(subcontractor_contract, work_item):
	return _addendum.get_addendum_add_options(subcontractor_contract, work_item)


@frappe.whitelist(methods=["POST"])
def create_addendum_draft(subcontractor_contract, items=None, payment_conditions=None, addendum_date=None):
	return _addendum.create_addendum_draft(subcontractor_contract, items, payment_conditions, addendum_date)


@frappe.whitelist(methods=["POST"])
def update_addendum_draft(name, modified, items=None, payment_conditions=None, addendum_date=None):
	return _addendum.update_addendum_draft(name, modified, items, payment_conditions, addendum_date)


@frappe.whitelist()
def preview_addendum_action(name, action):
	return _addendum.preview_addendum_action(name, action)


@frappe.whitelist(methods=["POST"])
def apply_addendum_action(name, modified, action):
	return _addendum.apply_addendum_action(name, modified, action)


@frappe.whitelist(methods=["POST"])
def attach_addendum_file(name, fieldname, modified, filename, content):
	return _addendum.attach_addendum_file(name, fieldname, modified, filename, content)


# -- Session / timeline ------------------------------------------------------


@frappe.whitelist()
def get_session_info():
	return _session.get_session_info()


@frappe.whitelist()
def get_document_timeline(doctype, name):
	return _session.get_document_timeline(doctype, name)


# -- Inbox -------------------------------------------------------------------


@frappe.whitelist()
def list_my_pending_approvals(limit=50, start=0):
	return _inbox.list_my_pending_approvals(limit, start)


def _given(**kwargs):
	return {key: value for key, value in kwargs.items() if value is not None}
