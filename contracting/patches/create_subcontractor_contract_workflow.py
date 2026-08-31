import frappe

WORKFLOW_NAME = "Subcontractor Contract Approval"
DOCTYPE = "Subcontractor Contract"

CONTRACTS_MANAGER = "Contracts Manager"
COST_CONTROL = "cost control manager"
CEO = "Company CEO"

DRAFT = "Draft"
PENDING_CM = "Pending Contracts Manager Approval"
PENDING_CC = "Pending Cost Control Approval"
PENDING_CEO = "Pending CEO Approval"
APPROVED = "Approved"
REJECTED = "Rejected"
CANCELLED = "Cancelled"

# (state, docstatus, role allowed to edit in it, style)
STATES = [
	(DRAFT, 0, CONTRACTS_MANAGER, ""),
	(PENDING_CM, 0, CONTRACTS_MANAGER, "Warning"),
	(PENDING_CC, 0, COST_CONTROL, "Warning"),
	(PENDING_CEO, 0, CEO, "Warning"),
	(APPROVED, 1, CONTRACTS_MANAGER, "Success"),
	(REJECTED, 0, CONTRACTS_MANAGER, "Danger"),
	(CANCELLED, 2, CONTRACTS_MANAGER, "Danger"),
]

# (from, action, to, allowed role, condition)
#
# The two transitions out of Pending Cost Control Approval are what make the
# CEO step conditional: requires_ceo_approval is precomputed on the document
# from Contracting Settings' threshold, because Frappe evaluates workflow
# conditions through safe_eval, which cannot read a Single doctype.
TRANSITIONS = [
	(DRAFT, "Submit for Approval", PENDING_CM, CONTRACTS_MANAGER, ""),
	(PENDING_CM, "Approve", PENDING_CC, CONTRACTS_MANAGER, ""),
	(PENDING_CM, "Reject", REJECTED, CONTRACTS_MANAGER, ""),
	(PENDING_CC, "Approve", APPROVED, COST_CONTROL, "doc.requires_ceo_approval == 0"),
	(PENDING_CC, "Approve", PENDING_CEO, COST_CONTROL, "doc.requires_ceo_approval == 1"),
	(PENDING_CC, "Reject", REJECTED, COST_CONTROL, ""),
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
	deploy would be worse than leaving this one-shot.
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
