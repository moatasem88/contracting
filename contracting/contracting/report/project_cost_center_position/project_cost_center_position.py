# Copyright (c) 2026, kazem and contributors
# For license information, please see license.txt

"""Project Cost Center Position (BRD 2026-10-30, rev 2).

Per Cost Center and Project: invoices, payments, uncleared cheques, Sales
Orders, guarantees, three balances and a check against the GL. Every source
line is resolved to one (cost_center, project) key by
utils.project_position.resolve; only leaves hold amounts, and subtotals are
sums of leaves. Source SQL lives in queries.py.
"""

import time
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt

from contracting.contracting.utils.project_position import (
	NO_COST_CENTER,
	NO_PROJECT,
	UNASSIGNED,
	cost_center_project_map,
	resolve,
)

from . import queries

REPORT_NAME = "Project Cost Center Position"
SLOW_RUN_SECONDS = 5
TOTAL_ROW_ID = "__total__"

COLUMNS = [
	("pi_net", _("PI Net")),
	("pi_gross", _("PI Gross")),
	("si_net", _("SI Net")),
	("si_gross", _("SI Gross")),
	("exp_purchase_invoice", _("Expenses: Purchase Invoices")),
	("exp_expense_claim", _("Expenses: Expense Claims")),
	("exp_stock", _("Expenses: Stock Issued")),
	("exp_journal_entry", _("Expenses: Journal Entries")),
	("exp_other", _("Expenses: Other")),
	("total_expenses", _("Total Expenses")),
	("paid", _("Payments Made")),
	("received", _("Payments Received")),
	("other_receipts", _("Receipts from Others")),
	("uncleared_paid", _("Uncleared Cheques Paid")),
	("uncleared_received", _("Uncleared Cheques Received")),
	("advances_outstanding", _("Employee Advances Outstanding")),
	("so_net", _("SO Net")),
	("so_gross", _("SO Gross")),
	("so_remaining", _("SO Remaining to Invoice")),
	("guarantees", _("Bank Guarantees (Active)")),
	("cash_balance", _("Cash Balance")),
	("accrual_net", _("Accrual Balance Net (SI − Total Expenses)")),
	("accrual_gross", _("Accrual Balance Gross")),
	("guarantee_adjusted_cash", _("Guarantee-adjusted Cash")),
	("gl_cash_difference", _("GL Cash Difference")),
]
AMOUNT_FIELDS = [fieldname for fieldname, _label in COLUMNS]

NOTES = _(
	"Amounts are in company currency as stored. A Payment Entry settling invoices of several projects "
	"follows its own header. Gross is pro-rata of each invoice's taxes, so deductions can make it lower "
	"than net. Total Expenses is every posting to an expense account (Purchase Invoices, Expense Claims, "
	"stock issued and reconciled, Journal Entries incl. depreciation, and other vouchers); stock and assets "
	"bought on a Purchase Invoice count when they are issued, not when invoiced. "
	"Payments Received counts only receipts from a customer of a Sales Order on the same Cost Center and "
	"Project; any other receipt shows in Receipts from Others. "
	"Uncleared cheques, Receipts from Others and Employee Advances Outstanding are information only and "
	"are not in any balance; "
	"an advance becomes an expense through its Expense Claim. "
	"GL Cash Difference = Cash Balance minus GL movement on Bank, Cash and cheque Notes accounts; "
	"a non-zero value is cash moved outside Payment Entries."
)


def execute(filters=None):
	started = time.monotonic()
	scope = queries.build_scope(filters)

	leaves = collect_leaves(scope)
	data = build_tree(leaves)

	elapsed = time.monotonic() - started
	if elapsed > SLOW_RUN_SECONDS:
		frappe.log_error(
			title=_("Slow report: {0}").format(REPORT_NAME),
			message=f"{elapsed:.1f}s with filters {frappe.as_json(filters)}",
		)

	return get_columns(), data, NOTES


def get_columns():
	columns = [{"label": _("Cost Center / Project"), "fieldname": "label", "fieldtype": "Data", "width": 280}]
	columns += [
		{"label": label, "fieldname": fieldname, "fieldtype": "Currency", "width": 150}
		for fieldname, label in COLUMNS
	]
	# Carried for the drill-down; not shown.
	columns += [
		{"label": "Cost Center", "fieldname": "cost_center", "fieldtype": "Data", "hidden": 1},
		{"label": "Project", "fieldname": "project", "fieldtype": "Data", "hidden": 1},
	]
	return columns


class KeyResolver:
	"""Resolves stored keys and applies the filters and User Permissions to
	the resolved key, so a derived Project is filtered like an explicit one.
	Receipt rows also get the client rule here (queries.apply_receipt_rule),
	so the report and the drill-down split them the same way."""

	def __init__(self, scope):
		self.scope = scope
		self.cc_map = cost_center_project_map()
		self.clients = defaultdict(set)
		for so in queries.sales_order_customers(scope.company):
			self.clients[resolve(so.cost_center, so.project, self.cc_map)].add(so.customer)

	def __call__(self, row):
		key = resolve(row.cost_center, row.project, self.cc_map)
		if not self.visible(*key):
			return None
		if "received" in row:
			queries.apply_receipt_rule(row, key, self.clients)
		return key

	def visible(self, cost_center, project):
		scope = self.scope
		is_project = project not in (UNASSIGNED, NO_PROJECT)
		if scope.cost_centers is not None and cost_center not in scope.cost_centers:
			return False
		if scope.projects is not None and project not in scope.projects:
			return False
		# Rows with no Cost Center / Project stay visible to restricted
		# users, as with Frappe's own (non-strict) User Permissions.
		if scope.allowed_cost_centers and cost_center != NO_COST_CENTER:
			if cost_center not in scope.allowed_cost_centers:
				return False
		if scope.allowed_projects and is_project and project not in scope.allowed_projects:
			return False
		return True


def collect_leaves(scope):
	"""{(cost_center, project): {metric: amount}} with balances filled in."""
	key_of = KeyResolver(scope)
	leaves = defaultdict(lambda: defaultdict(float))

	for builder in queries.BUILDERS.values():
		for row in builder(scope, group=True):
			key = key_of(row)
			if key is None:
				continue
			for metric in queries.SOURCE_METRICS:
				if metric in row:
					leaves[key][metric] += flt(row[metric])

	for amounts in leaves.values():
		for metric, signs in queries.DERIVED_METRICS.items():
			amounts[metric] = sum(sign * amounts[source] for source, sign in signs.items())
	return leaves


def project_sort_key(project):
	return (project in (UNASSIGNED, NO_PROJECT), project == NO_PROJECT, project)


def build_tree(leaves):
	"""Cost Center rows (indent 0) with Project leaves (indent 1), (No Cost
	Center) last, then the grand total (FR-03)."""
	by_cost_center = defaultdict(dict)
	for (cost_center, project), amounts in leaves.items():
		by_cost_center[cost_center][project] = amounts

	data = []
	grand_total = dict.fromkeys(AMOUNT_FIELDS, 0.0)
	for cost_center in sorted(by_cost_center, key=lambda cc: (cc == NO_COST_CENTER, cc)):
		projects = by_cost_center[cost_center]
		subtotal = {f: sum(flt(p.get(f)) for p in projects.values()) for f in AMOUNT_FIELDS}
		data.append({
			"row_id": cost_center, "parent_row": None, "indent": 0,
			"label": cost_center, "cost_center": cost_center, "project": None, **subtotal,
		})
		for project in sorted(projects, key=project_sort_key):
			data.append({
				"row_id": f"{cost_center}::{project}", "parent_row": cost_center, "indent": 1,
				"label": project, "cost_center": cost_center, "project": project,
				**{f: flt(projects[project].get(f)) for f in AMOUNT_FIELDS},
			})
		for f in AMOUNT_FIELDS:
			grand_total[f] += subtotal[f]

	if data:
		data.append({
			"row_id": TOTAL_ROW_ID, "parent_row": None, "indent": 0, "is_total_row": 1,
			"label": _("Total"), "cost_center": None, "project": None, **grand_total,
		})
	return data


@frappe.whitelist()
def get_drilldown(metric, filters, cost_center=None, project=None):
	"""FR-12: the source lines behind one cell, netted per voucher so the
	listed total equals the cell (a Payment Entry and its own GL cash rows
	cancel out of the GL Cash Difference, leaving Journal Entries etc).
	cost_center/project empty = a subtotal or the grand total."""
	if metric not in queries.METRICS:
		frappe.throw(_("Unknown metric {0}").format(frappe.bold(metric)))
	if not frappe.get_doc("Report", REPORT_NAME).is_permitted() or not frappe.has_permission(
		"Cost Center", "report"
	):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	scope = queries.build_scope(frappe.parse_json(filters))
	key_of = KeyResolver(scope)
	signs = queries.METRICS[metric]

	by_voucher = {}
	for builder_name in {queries.SOURCE_METRICS[source] for source in signs}:
		for row in queries.BUILDERS[builder_name](scope, group=False):
			key = key_of(row)
			if key is None or (cost_center and key[0] != cost_center) or (project and key[1] != project):
				continue
			amount = sum(sign * flt(row.get(source)) for source, sign in signs.items())
			voucher = (row.voucher_type, row.voucher_no)
			entry = by_voucher.setdefault(voucher, {
				"voucher_type": row.voucher_type, "voucher_no": row.voucher_no,
				"posting_date": row.posting_date, "party": row.party,
				"cost_center": key[0], "project": key[1], "amount": 0.0,
			})
			entry["amount"] += amount

	# Only float noise is dropped: sub-piaster residues are real (rounding
	# entries) and, across thousands of vouchers, add up to visible amounts.
	rows = [r for r in by_voucher.values() if round(r["amount"], 9)]
	rows.sort(key=lambda r: (r["posting_date"] or "", r["voucher_no"]))
	return {"rows": rows, "total": sum(r["amount"] for r in rows)}


@frappe.whitelist()
def get_project_options(txt=None):
	"""Project filter options. Accounts roles can't read Project on this
	site, so the standard Link search would return nothing for them."""
	if not frappe.has_permission("Cost Center", "report"):
		return []
	return frappe.db.sql(
		"""
		SELECT name AS value, project_name AS description
		FROM `tabProject`
		WHERE name LIKE %(txt)s OR project_name LIKE %(txt)s
		ORDER BY name
		LIMIT 50
		""",
		{"txt": f"%{txt or ''}%"},
		as_dict=True,
	)
