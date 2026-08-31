import frappe
from frappe import _


def create_retention_je_on_purchase_invoice_submit(doc, method):
	_create_retention_je(
		invoice_doc=doc,
		progress_doctype="Contractor Invoice",
		link_field="purchase_invoice",
		settings_field="retention_payable_account",
		party_account=doc.credit_to,
		party_type="Supplier",
		party=doc.supplier,
	)


def create_retention_je_on_sales_invoice_submit(doc, method):
	_create_retention_je(
		invoice_doc=doc,
		progress_doctype="Client Progress Invoice",
		link_field="sales_invoice",
		settings_field="retention_receivable_account",
		party_account=doc.debit_to,
		party_type="Customer",
		party=doc.customer,
	)


def _create_retention_je(invoice_doc, progress_doctype, link_field, settings_field, party_account, party_type, party):
	progress_name = frappe.db.get_value(progress_doctype, {link_field: invoice_doc.name}, "name")
	if not progress_name:
		return

	retention_amount = frappe.db.get_value(progress_doctype, progress_name, "total_retention_held") or 0
	if not retention_amount:
		return

	retention_account = frappe.db.get_single_value("Contracting Settings", settings_field)
	if not retention_account:
		frappe.msgprint(
			_(
				"Retention of {0} was withheld on {1} but no {2} is configured in Contracting "
				"Settings - no retention Journal Entry was created."
			).format(retention_amount, progress_name, settings_field.replace("_", " ").title()),
			indicator="orange",
			alert=True,
		)
		return

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = invoice_doc.company
	je.posting_date = invoice_doc.posting_date
	je.user_remark = _("Retention withheld on {0} against {1}").format(progress_name, invoice_doc.name)

	cost_center = invoice_doc.get("cost_center") or frappe.db.get_value(
		"Cost Center", {"company": invoice_doc.company, "is_group": 0}, "name"
	)

	party_row = {
		"account": party_account,
		"party_type": party_type,
		"party": party,
		"reference_type": invoice_doc.doctype,
		"reference_name": invoice_doc.name,
		"cost_center": cost_center,
	}

	if party_type == "Supplier":
		# We owe the supplier less right now (debit Payable), move the
		# held-back amount into Retention Payable (credit) instead.
		je.append("accounts", {**party_row, "debit_in_account_currency": retention_amount})
		je.append("accounts", {"account": retention_account, "cost_center": cost_center, "credit_in_account_currency": retention_amount})
	else:
		# The customer owes us less right now (credit Receivable), move
		# the held-back amount into Retention Receivable (debit) instead.
		je.append("accounts", {**party_row, "credit_in_account_currency": retention_amount})
		je.append("accounts", {"account": retention_account, "cost_center": cost_center, "debit_in_account_currency": retention_amount})

	je.insert(ignore_permissions=True)
	je.submit()

	frappe.db.set_value(progress_doctype, progress_name, "retention_journal_entry", je.name)
