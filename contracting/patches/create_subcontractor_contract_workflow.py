import frappe

# STATES/TRANSITIONS below reflect the chain as rebuilt by the 2026-09-27
# BRD (see rebuild_subcontractor_contract_approval_chain.py, which is what
# actually mutates the live Workflow doc - this file's own execute() is a
# historical one-shot, guarded below, that already ran and is now inert on
# this site). hooks.py imports these constants to decide which Workflow
# State / Workflow Action Master docs get fixture-exported, so they're kept
# current here even though execute() itself won't re-run.

WORKFLOW_NAME = "Subcontractor Contract Approval"
DOCTYPE = "Subcontractor Contract"

CONTRACTS_MANAGER = "Contracts Manager"
CONTRACTS_ENGINEER = "Contracts Engineer"
PROJECTS_DIRECTOR = "Projects Director"
COST_CONTROL_ENGINEER = "Cost Control Engineer"
COST_CONTROL = "cost control manager"
PLANNING_DIRECTOR = "Planning and Cost Control Director"
CEO = "Company CEO"

DRAFT = "Draft"
PENDING_CM = "Pending Contracts Manager Approval"
PENDING_PD = "Pending Projects Director Approval"
PENDING_CCE = "Pending Cost Control Engineer Review"
PENDING_CC = "Pending Cost Control Approval"
PENDING_CCD = "Pending Cost Control Director Approval"
PENDING_CEO = "Pending CEO Approval"
APPROVED = "Approved"
REJECTED = "Rejected"
CANCELLED = "Cancelled"

# (state, docstatus, role allowed to edit in it, style)
#
# Draft appears twice (Contracts Manager, Contracts Engineer) - allow_edit
# is a single-value Role Link, so multi-role editability for one state is
# expressed as two rows sharing that state name, not one row with two
# roles (see rebuild_subcontractor_contract_approval_chain.py's module
# docstring for why this is safe).
STATES = [
	(DRAFT, 0, CONTRACTS_MANAGER, ""),
	(DRAFT, 0, CONTRACTS_ENGINEER, ""),
	(PENDING_CM, 0, CONTRACTS_MANAGER, "Warning"),
	(PENDING_PD, 0, PROJECTS_DIRECTOR, "Warning"),
	(PENDING_CCE, 0, COST_CONTROL_ENGINEER, "Warning"),
	(PENDING_CC, 0, COST_CONTROL, "Warning"),
	(PENDING_CCD, 0, PLANNING_DIRECTOR, "Warning"),
	(PENDING_CEO, 0, CEO, "Warning"),
	(APPROVED, 1, CONTRACTS_MANAGER, "Success"),
	(REJECTED, 0, CONTRACTS_MANAGER, "Danger"),
	(CANCELLED, 2, CONTRACTS_MANAGER, "Danger"),
]

# (from, action, to, allowed role, condition)
#
# The two transitions out of Pending Cost Control Director Approval are what
# make the CEO step conditional: requires_ceo_approval is precomputed on the
# document by set_ceo_approval_flag() (per-work-item 250k/first-time rule or
# an acknowledged price exception - no longer Contracting Settings' whole-
# contract threshold), because Frappe evaluates workflow conditions through
# safe_eval, which cannot read a Single doctype.
TRANSITIONS = [
	(DRAFT, "Submit for Approval", PENDING_CM, CONTRACTS_MANAGER, ""),
	(DRAFT, "Submit for Approval", PENDING_CM, CONTRACTS_ENGINEER, ""),
	(PENDING_CM, "Approve", PENDING_PD, CONTRACTS_MANAGER, ""),
	(PENDING_CM, "Reject", REJECTED, CONTRACTS_MANAGER, ""),
	(PENDING_PD, "Approve", PENDING_CCE, PROJECTS_DIRECTOR, ""),
	(PENDING_PD, "Reject", REJECTED, PROJECTS_DIRECTOR, ""),
	(PENDING_CCE, "Approve", PENDING_CC, COST_CONTROL_ENGINEER, "doc.checklist_attachment"),
	(PENDING_CCE, "Reject", REJECTED, COST_CONTROL_ENGINEER, ""),
	(PENDING_CC, "Approve", PENDING_CCD, COST_CONTROL, ""),
	(PENDING_CC, "Reject", REJECTED, COST_CONTROL, ""),
	(PENDING_CCD, "Approve", APPROVED, PLANNING_DIRECTOR, "doc.requires_ceo_approval == 0"),
	(PENDING_CCD, "Approve", PENDING_CEO, PLANNING_DIRECTOR, "doc.requires_ceo_approval == 1"),
	(PENDING_CCD, "Reject", REJECTED, PLANNING_DIRECTOR, ""),
	(PENDING_CEO, "Approve", APPROVED, CEO, ""),
	(PENDING_CEO, "Reject", REJECTED, CEO, ""),
	# Reopening restarts the chain from the beginning rather than resuming
	# partway through (FR-20 / TC-26).
	(REJECTED, "Reopen", DRAFT, CONTRACTS_MANAGER, ""),
	# TC-06 needs cancellation to free committed quantity, which requires
	# docstatus 2 - hence an explicit Cancelled state.
	(APPROVED, "Cancel", CANCELLED, CONTRACTS_MANAGER, ""),
]


def execute():
	"""Create the Subcontractor Contract approval chain.

	Not re-asserted on every migrate: workflows are commonly tweaked in the
	UI (thresholds, extra approvers), and overwriting those edits on each
	deploy would be worse than leaving this one-shot. On this site the
	Workflow already exists, so this is inert - the 2026-09-27 rebuild is
	applied by rebuild_subcontractor_contract_approval_chain.py instead.
	"""
	if frappe.db.exists("Workflow", WORKFLOW_NAME):
		return

	for state, _docstatus, _role, style in STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc({
				"doctype": "Workflow State", "workflow_state_name": state, "style": style,
			}).insert(ignore_permissions=True)

	for action in sorted({t[1] for t in TRANSITIONS}):
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc({
				"doctype": "Workflow Action Master", "workflow_action_name": action,
			}).insert(ignore_permissions=True)

	workflow = frappe.get_doc({
		"doctype": "Workflow",
		"workflow_name": WORKFLOW_NAME,
		"document_type": DOCTYPE,
		"workflow_state_field": "workflow_state",
		"is_active": 1,
		"send_email_alert": 0,
	})

	for state, docstatus, role, _style in STATES:
		workflow.append("states", {
			"state": state,
			"doc_status": str(docstatus),
			"allow_edit": role,
		})

	for from_state, action, to_state, role, condition in TRANSITIONS:
		workflow.append("transitions", {
			"state": from_state,
			"action": action,
			"next_state": to_state,
			"allowed": role,
			"condition": condition,
		})

	workflow.insert(ignore_permissions=True)
	frappe.db.commit()
	print("created workflow: {0}".format(WORKFLOW_NAME))
