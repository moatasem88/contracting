# Copyright (c) 2026, kazem and contributors
# For license information, please see license.txt

"""Project from Cost Center (Project & Cost Center Position BRD, 2026-10-30).

Project is empty on almost every transaction, but Cost Center is reliable
and `Project.cost_center` says which Project a Cost Center stands for. One
rule, used by the report (FR-02), the form default (FR-15) and the
mandatory-Project submit check (FR-14):

- exactly one Project on the Cost Center -> that Project;
- more than one -> UNASSIGNED (shared Cost Center, someone must pick);
- none -> NO_PROJECT (an overhead Cost Center, never blocked).

Accounts roles have no read permission on Project on this site (Custom
DocPerm), so everything here reads tabProject with raw SQL and only ever
hands back Project names.
"""

import frappe
from frappe import _
from frappe.utils import cint

UNASSIGNED = "(Unassigned)"
NO_PROJECT = "(No Project)"
NO_COST_CENTER = "(No Cost Center)"

# Payment Entries that never belong to a project: bank-to-bank moves, and
# the clearing entries cheques_management posts when a cheque clears (the
# original cheque PE already carries the project).
EXEMPT_PAYMENT_TYPES = ("Internal Transfer",)
BANK_CLEARANCE = "Bank Clearance"

ROW_CHECKED_DOCTYPES = ("Purchase Invoice", "Sales Invoice")


def cost_center_project_map(cost_centers=None):
	"""{cost_center: project name, or UNASSIGNED when shared}. Cost Centers
	no Project points to are absent, so `.get(cc, NO_PROJECT)` reads right.
	Projects of any status count."""
	if cost_centers is not None:
		cost_centers = [cc for cc in set(cost_centers) if cc]
		if not cost_centers:
			return {}

	condition = "AND cost_center IN %(cost_centers)s" if cost_centers else ""
	rows = frappe.db.sql(
		f"""
		SELECT cost_center, COUNT(*), MIN(name)
		FROM `tabProject`
		WHERE IFNULL(cost_center, '') != '' {condition}
		GROUP BY cost_center
		""",
		{"cost_centers": cost_centers},
	)
	return {cc: (project if count == 1 else UNASSIGNED) for cc, count, project in rows}


def resolve(cost_center, project, cc_map):
	"""The (cost_center, project) key of one source line. An explicit
	Project always wins, even if its own Cost Center differs (FR-02.3)."""
	if not cost_center:
		return NO_COST_CENTER, project or NO_PROJECT
	return cost_center, project or cc_map.get(cost_center, NO_PROJECT)


@frappe.whitelist()
def project_for_cost_center(cost_center):
	"""FR-15 form default: the Project only when the mapping is unique."""
	if not cost_center:
		return None
	project = cost_center_project_map([cost_center]).get(cost_center)
	return None if project in (None, UNASSIGNED) else project


def is_enforced():
	value = frappe.db.get_single_value("Project Position Settings", "enforce_mandatory_project")
	# Never saved yet: the field's default (on) applies, as after migrate.
	return True if value is None else bool(cint(value))


def validate_project_mandatory(doc, method=None):
	"""before_submit on PI, SI, PE and SO (FR-14). Only a submit is checked,
	so documents already submitted are never touched again (cheques_management
	and the payment schedule both write to submitted Payment Entries)."""
	if not is_enforced():
		return
	if doc.doctype == "Payment Entry" and (
		doc.payment_type in EXEMPT_PAYMENT_TYPES or doc.mode_of_payment == BANK_CLEARANCE
	):
		return

	rows = (doc.get("items") or []) if doc.doctype in ROW_CHECKED_DOCTYPES else []
	cc_map = cost_center_project_map([doc.get("cost_center")] + [row.get("cost_center") for row in rows])
	if not cc_map:
		return

	errors = []
	if doc.get("cost_center") in cc_map and not doc.get("project"):
		errors.append(_("header (Cost Center {0})").format(doc.cost_center))

	header_project_cc = None
	if doc.get("project"):
		header_project_cc = frappe.db.get_value("Project", doc.project, "cost_center")

	for row in rows:
		if row.get("project") or row.get("cost_center") not in cc_map:
			continue
		if not doc.get("project"):
			errors.append(_("row {0} (Cost Center {1})").format(row.idx, row.cost_center))
		elif header_project_cc != row.cost_center:
			# The line would be booked to the header's Project although its
			# Cost Center belongs to another one.
			errors.append(
				_("row {0} (Cost Center {1}, header Project {2} belongs to {3})").format(
					row.idx, row.cost_center, doc.project, header_project_cc or _("no Cost Center")
				)
			)

	if errors:
		frappe.throw(_("Project is required: {0}.").format("; ".join(errors)), title=_("Project Required"))
