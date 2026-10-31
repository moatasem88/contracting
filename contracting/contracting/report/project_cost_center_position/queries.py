# Copyright (c) 2026, kazem and contributors
# For license information, please see license.txt

"""Source queries for the Project Cost Center Position report.

One builder per source. Each runs one parameterised SQL statement and
returns raw rows carrying the stored `cost_center`/`project` (not yet
resolved) plus its metrics:

- `group=True`  -> one row per stored (cost_center, project), metrics summed;
- `group=False` -> one row per source line, with voucher_type, voucher_no,
  posting_date and party, for the drill-down.

The report and the drill-down call the very same builder, so a dialog's
total always equals the cell clicked (Technical Design D6). Doctype names
put into SQL come only from INVOICE_DOCTYPES (NFR-05).
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, today
from frappe.utils.nestedset import get_descendants_of

from contracting.contracting.utils.project_position import BANK_CLEARANCE

INVOICE_DOCTYPES = {
	"Purchase Invoice": {"prefix": "pi", "party": "supplier"},
	"Sales Invoice": {"prefix": "si", "party": "customer"},
}

# Gross per line is the line's share of the header's taxes (D1); a zero net
# total would divide by zero, so its lines count gross = net.
GROSS_RATIO = "IF({h}.base_net_total = 0, 1, {h}.base_grand_total / {h}.base_net_total)"


def build_scope(filters):
	"""Validated filters plus everything the builders need, computed once."""
	filters = frappe._dict(filters or {})
	company = filters.get("company") or frappe.defaults.get_user_default("Company")
	to_date = getdate(filters.get("to_date") or today())
	from_date = getdate(filters.from_date) if filters.get("from_date") else None
	if from_date and from_date > to_date:
		frappe.throw(_("From Date cannot be after To Date"))

	settings = frappe.get_cached_doc("Project Position Settings")

	return frappe._dict(
		company=company,
		from_date=from_date,
		to_date=to_date,
		cost_centers=expand_cost_centers(filters.get("cost_center")),
		projects=set(as_list(filters.get("project"))) or None,
		allowed_cost_centers=allowed_values("Cost Center"),
		allowed_projects=allowed_values("Project"),
		guarantee_accounts=[row.account for row in settings.guarantee_accounts],
	)


def as_list(value):
	if not value:
		return []
	if isinstance(value, str):
		value = frappe.parse_json(value) if value.startswith("[") else [value]
	return [v for v in value if v]


def expand_cost_centers(value):
	"""A group Cost Center in the filter stands for all Cost Centers under it."""
	selected = as_list(value)
	if not selected:
		return None
	expanded = set(selected)
	for cc in selected:
		expanded.update(get_descendants_of("Cost Center", cc, ignore_permissions=True))
	return expanded


def allowed_values(doctype):
	"""User Permission values for doctype, or None when unrestricted."""
	from frappe.core.doctype.user_permission.user_permission import get_user_permissions

	entries = get_user_permissions(frappe.session.user).get(doctype) or []
	return {entry.get("doc") for entry in entries} or None


def date_condition(field, scope, use_from_date=True):
	condition = f"{field} <= %(to_date)s"
	if use_from_date and scope.from_date:
		condition += f" AND {field} >= %(from_date)s"
	return condition


def run(scope, sql_from, where, cost_center, project, metrics, detail, group, extra_params=None, keys=None):
	"""Shared SELECT for every builder - see the module docstring. `keys`
	adds grouping columns beyond the Cost Center/Project pair."""
	params = {"company": scope.company, "to_date": scope.to_date, "from_date": scope.from_date}
	params.update(extra_params or {})
	key_select = "".join(f", {expr} AS {name}" for name, expr in (keys or {}).items())
	group_by = ", ".join(str(i) for i in range(1, 3 + len(keys or {})))

	if group:
		select = ", ".join(f"SUM({expr}) AS {name}" for name, expr in metrics.items())
		return frappe.db.sql(
			f"""
			SELECT {cost_center} AS cost_center, {project} AS project{key_select}, {select}
			FROM {sql_from}
			WHERE {where}
			GROUP BY {group_by}
			""",
			params,
			as_dict=True,
		)

	select = ", ".join(f"{expr} AS {name}" for name, expr in {**detail, **metrics}.items())
	return frappe.db.sql(
		f"""
		SELECT {cost_center} AS cost_center, {project} AS project{key_select}, {select}
		FROM {sql_from}
		WHERE {where}
		""",
		params,
		as_dict=True,
	)


def invoices(scope, doctype, group=True):
	"""FR-04/05: submitted invoice lines, keyed by row then header. Returns
	and credit notes are summed as stored, negative (D2)."""
	if doctype not in INVOICE_DOCTYPES:
		frappe.throw(_("Unsupported doctype {0}").format(doctype))
	prefix = INVOICE_DOCTYPES[doctype]["prefix"]
	party = INVOICE_DOCTYPES[doctype]["party"]

	return run(
		scope,
		sql_from=f"`tab{doctype}` h JOIN `tab{doctype} Item` i ON i.parent = h.name AND i.parenttype = '{doctype}'",
		where=f"h.docstatus = 1 AND h.company = %(company)s AND {date_condition('h.posting_date', scope)}",
		cost_center="COALESCE(NULLIF(i.cost_center, ''), NULLIF(h.cost_center, ''))",
		project="COALESCE(NULLIF(i.project, ''), NULLIF(h.project, ''))",
		metrics={
			f"{prefix}_net": "i.base_net_amount",
			f"{prefix}_gross": "i.base_net_amount * " + GROSS_RATIO.format(h="h"),
		},
		detail={
			"voucher_type": f"'{doctype}'",
			"voucher_no": "h.name",
			"posting_date": "h.posting_date",
			"party": f"h.{party}",
		},
		group=group,
	)


def payments(scope, group=True):
	"""FR-06: Pay/Receive entries, header key. Bank Clearance entries are the
	cheque clearings cheques_management posts; counting them too would count
	every cleared cheque twice (same rule as utils/project_cost_billing.py).
	Rows carry the paying customer (`payer`), so apply_receipt_rule can keep
	only receipts from the project's own Sales Order customer."""
	return run(
		scope,
		sql_from="`tabPayment Entry` pe",
		where=f"""pe.docstatus = 1 AND pe.company = %(company)s
			AND pe.payment_type IN ('Pay', 'Receive')
			AND IFNULL(pe.mode_of_payment, '') != %(bank_clearance)s
			AND {date_condition('pe.posting_date', scope)}""",
		cost_center="NULLIF(pe.cost_center, '')",
		project="NULLIF(pe.project, '')",
		metrics={
			"paid": "IF(pe.payment_type = 'Pay', pe.base_paid_amount, 0)",
			"received": "IF(pe.payment_type = 'Receive', pe.base_received_amount, 0)",
			"other_receipts": "0",
		},
		detail={
			"voucher_type": "'Payment Entry'",
			"voucher_no": "pe.name",
			"posting_date": "pe.posting_date",
			"party": "pe.party",
		},
		group=group,
		extra_params={"bank_clearance": BANK_CLEARANCE},
		keys={"payer": "IF(pe.party_type = 'Customer', pe.party, NULL)"},
	)


def sales_order_customers(company):
	"""Stored (cost_center, project, customer) of every submitted Sales Order,
	whatever its status or date: the clients a project's receipts may come from."""
	return frappe.db.sql(
		"""
		SELECT NULLIF(cost_center, '') AS cost_center, NULLIF(project, '') AS project, customer
		FROM `tabSales Order`
		WHERE docstatus = 1 AND company = %(company)s
		""",
		{"company": company},
		as_dict=True,
	)


def apply_receipt_rule(row, key, clients):
	"""A receipt counts as Payments Received only when the payer is a customer
	of a Sales Order under the same Cost Center and Project. Anything else
	(no party, a supplier refund, another project's client, a customer with no
	Sales Order) moves to other_receipts: still cash, so the GL Cash
	Difference still sees it, but not money the client paid for the project."""
	if not row.get("received") or row.get("payer") in clients.get(key, ()):
		return
	row["other_receipts"] = flt(row.get("other_receipts")) + flt(row["received"])
	row["received"] = 0


def uncleared_cheques(scope, group=True):
	"""FR-07: cheque rows not cleared as at To Date (D4) - the clearing entry
	is missing, not submitted, or dated after To Date. A position, so the
	From Date does not apply. Rejected/Returned cheques stay in."""
	amount = "cpe.allocated_amount * IF(pe.payment_type = 'Pay', pe.source_exchange_rate, pe.target_exchange_rate)"
	return run(
		scope,
		sql_from="""`tabCheque Payment Entry` cpe
			JOIN `tabCheque` c ON c.name = cpe.parent AND cpe.parenttype = 'Cheque'
			JOIN `tabPayment Entry` pe ON pe.name = cpe.payment_entry
			LEFT JOIN `tabPayment Entry` cl ON cl.name = cpe.clearing_payment_entry""",
		where="""c.docstatus = 1 AND c.status != 'Cancelled'
			AND pe.docstatus = 1 AND pe.company = %(company)s AND pe.posting_date <= %(to_date)s
			AND (cl.name IS NULL OR cl.docstatus != 1 OR cl.posting_date > %(to_date)s)""",
		cost_center="COALESCE(NULLIF(cpe.cost_center, ''), NULLIF(c.cost_center, ''))",
		project="COALESCE(NULLIF(cpe.project, ''), NULLIF(c.project, ''))",
		metrics={
			"uncleared_paid": f"IF(pe.payment_type = 'Pay', {amount}, 0)",
			"uncleared_received": f"IF(pe.payment_type = 'Receive', {amount}, 0)",
		},
		detail={
			"voucher_type": "'Cheque'",
			"voucher_no": "c.name",
			"posting_date": "pe.posting_date",
			"party": "pe.party",
		},
		group=group,
	)


def sales_orders(scope, group=True):
	"""FR-08: open Sales Orders, header key (Sales Order Item has no
	cost_center/project). billed_amt is in order currency, hence the rate (D8)."""
	return run(
		scope,
		sql_from="`tabSales Order` so JOIN `tabSales Order Item` i ON i.parent = so.name AND i.parenttype = 'Sales Order'",
		where=f"""so.docstatus = 1 AND so.status != 'Closed' AND so.company = %(company)s
			AND {date_condition('so.transaction_date', scope)}""",
		cost_center="NULLIF(so.cost_center, '')",
		project="NULLIF(so.project, '')",
		metrics={
			"so_net": "i.base_net_amount",
			"so_gross": "i.base_net_amount * " + GROSS_RATIO.format(h="so"),
			"so_remaining": "GREATEST(i.base_net_amount - i.billed_amt * so.conversion_rate, 0)",
		},
		detail={
			"voucher_type": "'Sales Order'",
			"voucher_no": "so.name",
			"posting_date": "so.transaction_date",
			"party": "so.customer",
		},
		group=group,
	)


def guarantees(scope, group=True):
	"""FR-09: balance of the settings' guarantee accounts as at To Date. The
	From Date is ignored, a guarantee being a position (D3)."""
	if not scope.guarantee_accounts:
		return []
	return gl_balance(scope, "guarantees", scope.guarantee_accounts, use_from_date=False, group=group)


def gl_cash(scope, group=True):
	"""FR-11: GL movement on Bank/Cash accounts plus the cheque Notes
	accounts. Notes are in so that a cheque PE (to Notes) and its clearing
	(Notes to Bank) leave the check neutral."""
	return gl_balance(scope, "gl_cash", cash_accounts(scope.company), use_from_date=True, group=group)


def gl_balance(scope, metric, accounts, use_from_date, group):
	if not accounts:
		return []
	return run(
		scope,
		sql_from="`tabGL Entry` gl",
		where=f"""gl.is_cancelled = 0 AND gl.company = %(company)s AND gl.account IN %(accounts)s
			AND {date_condition('gl.posting_date', scope, use_from_date)}""",
		cost_center="NULLIF(gl.cost_center, '')",
		project="NULLIF(gl.project, '')",
		metrics={metric: "gl.debit - gl.credit"},
		detail={
			"voucher_type": "gl.voucher_type",
			"voucher_no": "gl.voucher_no",
			"posting_date": "gl.posting_date",
			"party": "gl.party",
		},
		group=group,
		extra_params={"accounts": tuple(accounts)},
	)


def expenses(scope, group=True):
	"""Total spent: every GL entry on an Expense-root account, split by the
	voucher that posted it. The PI columns alone miss Expense Claims, stock
	issued, Journal Entries (incl. depreciation) etc., and include stock and
	asset purchases that only become an expense when issued - so spending is
	read from the ledger, where each amount is expensed exactly once.
	Period Closing Vouchers are left out: they zero the expense accounts."""
	voucher = "gl.voucher_type"
	stock = "('Stock Entry', 'Stock Reconciliation', 'Purchase Receipt', 'Delivery Note')"
	known = "('Purchase Invoice', 'Expense Claim', 'Journal Entry', " + stock[1:]
	amount = "(gl.debit - gl.credit)"
	accounts = frappe.get_all("Account", filters={"company": scope.company, "root_type": "Expense", "is_group": 0}, pluck="name")
	if not accounts:
		return []
	return run(
		scope,
		sql_from="`tabGL Entry` gl",
		where=f"""gl.is_cancelled = 0 AND gl.company = %(company)s AND gl.account IN %(accounts)s
			AND gl.voucher_type != 'Period Closing Voucher'
			AND {date_condition('gl.posting_date', scope)}""",
		cost_center="NULLIF(gl.cost_center, '')",
		project="NULLIF(gl.project, '')",
		metrics={
			"exp_purchase_invoice": f"IF({voucher} = 'Purchase Invoice', {amount}, 0)",
			"exp_expense_claim": f"IF({voucher} = 'Expense Claim', {amount}, 0)",
			"exp_stock": f"IF({voucher} IN {stock}, {amount}, 0)",
			"exp_journal_entry": f"IF({voucher} = 'Journal Entry', {amount}, 0)",
			"exp_other": f"IF({voucher} NOT IN {known}, {amount}, 0)",
		},
		detail={
			"voucher_type": "gl.voucher_type",
			"voucher_no": "gl.voucher_no",
			"posting_date": "gl.posting_date",
			"party": "gl.party",
		},
		group=group,
		extra_params={"accounts": tuple(accounts)},
	)


def employee_advances(scope, group=True):
	"""Employee Advances not yet claimed or returned as at To Date
	(information, like uncleared cheques). An advance is cash, not expense:
	the expense arrives with the Expense Claim, already in expenses(). The
	advance has no Cost Center, so each paying Payment Entry carries its share,
	keyed by the advance's Project, else the PE header. Claims count as at
	To Date; returns are taken as they stand today (return_amount)."""
	open_share = "GREATEST(1 - (IFNULL(cl.claimed, 0) + IFNULL(ea.return_amount, 0)) / ea.paid_amount, 0)"
	return run(
		scope,
		sql_from="""`tabPayment Entry Reference` per
			JOIN `tabPayment Entry` pe ON pe.name = per.parent
			JOIN `tabEmployee Advance` ea ON ea.name = per.reference_name
			LEFT JOIN (
				SELECT eca.employee_advance, SUM(eca.allocated_amount) AS claimed
				FROM `tabExpense Claim Advance` eca
				JOIN `tabExpense Claim` ec ON ec.name = eca.parent
				WHERE ec.docstatus = 1 AND ec.posting_date <= %(to_date)s
				GROUP BY eca.employee_advance
			) cl ON cl.employee_advance = ea.name""",
		where="""per.reference_doctype = 'Employee Advance' AND pe.docstatus = 1 AND ea.docstatus = 1
			AND ea.paid_amount > 0 AND pe.company = %(company)s AND pe.posting_date <= %(to_date)s""",
		cost_center="NULLIF(pe.cost_center, '')",
		project="COALESCE(NULLIF(ea.project, ''), NULLIF(pe.project, ''))",
		metrics={
			"advances_outstanding": f"per.allocated_amount * IFNULL(NULLIF(ea.exchange_rate, 0), 1) * {open_share}",
		},
		detail={
			"voucher_type": "'Employee Advance'",
			"voucher_no": "ea.name",
			"posting_date": "pe.posting_date",
			"party": "ea.employee",
		},
		group=group,
	)


def cash_accounts(company):
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 0, "account_type": ["in", ["Bank", "Cash"]]},
		pluck="name",
	)
	# Cheques Settings is not a Single on this site (issingle = 0), so its
	# Notes rows are read per company record.
	settings = frappe.get_all("Cheques Settings", filters={"company": company}, pluck="name")
	if settings:
		accounts += frappe.get_all(
			"Cheque Notes Account",
			filters={"parenttype": "Cheques Settings", "parent": ["in", settings]},
			pluck="account",
		)
	return sorted(set(accounts))


# metric -> builder. Each builder fills only its own metrics.
BUILDERS = {
	"pi": lambda scope, group=True: invoices(scope, "Purchase Invoice", group),
	"si": lambda scope, group=True: invoices(scope, "Sales Invoice", group),
	"payments": payments,
	"cheques": uncleared_cheques,
	"sales_orders": sales_orders,
	"guarantees": guarantees,
	"gl_cash": gl_cash,
	"expenses": expenses,
	"employee_advances": employee_advances,
}

SOURCE_METRICS = {
	"pi_net": "pi",
	"pi_gross": "pi",
	"si_net": "si",
	"si_gross": "si",
	"paid": "payments",
	"received": "payments",
	"other_receipts": "payments",
	"uncleared_paid": "cheques",
	"uncleared_received": "cheques",
	"so_net": "sales_orders",
	"so_gross": "sales_orders",
	"so_remaining": "sales_orders",
	"guarantees": "guarantees",
	"gl_cash": "gl_cash",
	"exp_purchase_invoice": "expenses",
	"exp_expense_claim": "expenses",
	"exp_stock": "expenses",
	"exp_journal_entry": "expenses",
	"exp_other": "expenses",
	"advances_outstanding": "employee_advances",
}

EXPENSE_METRICS = ("exp_purchase_invoice", "exp_expense_claim", "exp_stock", "exp_journal_entry", "exp_other")

# FR-10/11: balances are linear in the source metrics, so per-leaf values,
# subtotals and drill-downs all follow from these signs.
DERIVED_METRICS = {
	"total_expenses": dict.fromkeys(EXPENSE_METRICS, 1),
	"cash_balance": {"received": 1, "paid": -1},
	# Sales against everything spent, not against Purchase Invoices only.
	"accrual_net": {"si_net": 1, **dict.fromkeys(EXPENSE_METRICS, -1)},
	"accrual_gross": {"si_gross": 1, "pi_gross": -1},
	"guarantee_adjusted_cash": {"received": 1, "paid": -1, "guarantees": -1},
	"gl_cash_difference": {"received": 1, "paid": -1, "gl_cash": -1},
}

METRICS = {**{m: {m: 1} for m in SOURCE_METRICS}, **DERIVED_METRICS}
