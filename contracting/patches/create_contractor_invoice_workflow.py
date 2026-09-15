import frappe

WORKFLOW_NAME = "Contractor Invoice Approval"
DOCTYPE = "Contractor Invoice"

SITE_ENGINEER = "Site Engineer"
PROJECT_MANAGER = "Project Manager"
COST_CONTROL_ENGINEER = "Cost Control Engineer"
COST_CONTROL_MANAGER = "cost control manager"
PLANNING_DIRECTOR = "Planning and Cost Control Director"
ACCOUNTANT = "Accountant"
ACCOUNTS_MANAGER = "Accounts Manager"

DRAFT = "Draft"
PENDING_PM = "Pending Project Manager Approval"
PENDING_CCE = "Pending Cost Control Engineer Review"
PENDING_CCM = "Pending Cost Control Manager Approval"
PENDING_PCD = "Pending Planning and Cost Control Director Approval"
PENDING_ACCT = "Pending Accountant Approval"
PENDING_AM = "Pending Accounts Manager Approval"
APPROVED = "Approved"
REJECTED = "Rejected"

# (state, docstatus, role allowed to edit in it, style)
#
# Rejected stays at docstatus 0, not 2 as the BRD originally specified:
# Frappe's workflow engine (frappe.model.workflow.apply_workflow) has no
# path from a docstatus-0 state straight to docstatus 2 - the only legal
# hops are 0->0, 0->1, 1->1, 1->2 (frappe.model.document.
# check_docstatus_transition raises DocstatusTransitionError on 0->2).
# Mirrors Subcontractor Contract Approval's own REJECTED state (also
# docstatus 0) - a Reopen transition below resets the document to Draft in
# place instead of Frappe's native Amend.
STATES = [
	(DRAFT, 0, SITE_ENGINEER, ""),
	(PENDING_PM, 0, PROJECT_MANAGER, "Warning"),
	(PENDING_CCE, 0, COST_CONTROL_ENGINEER, "Warning"),
	(PENDING_CCM, 0, COST_CONTROL_MANAGER, "Warning"),
	(PENDING_PCD, 0, PLANNING_DIRECTOR, "Warning"),
	(PENDING_ACCT, 0, ACCOUNTANT, "Warning"),
	(PENDING_AM, 0, ACCOUNTS_MANAGER, "Warning"),
	(APPROVED, 1, ACCOUNTS_MANAGER, "Success"),
	(REJECTED, 0, SITE_ENGINEER, "Danger"),
]

# (from, action, to, allowed role, condition)
#
# FR-23: Draft -> Submit for Review requires invoice_attachment.
# FR-25: Pending Cost Control Engineer Review -> Approve requires
# checklist_attachment - the Cost Control Engineer cannot pass their own
# step through to Cost Control Manager without it.
# FR-26: Reject is available from every one of the 6 pending-approval
# states, to the role that owns that state, always landing on Rejected.
# Reopen (Rejected -> Draft) is the Reopen-in-place substitute for native
# Amend - see the STATES comment above.
TRANSITIONS = [
	(DRAFT, "Submit for Review", PENDING_PM, SITE_ENGINEER, "doc.invoice_attachment"),
	(PENDING_PM, "Approve", PENDING_CCE, PROJECT_MANAGER, ""),
	(PENDING_PM, "Reject", REJECTED, PROJECT_MANAGER, ""),
	(PENDING_CCE, "Approve", PENDING_CCM, COST_CONTROL_ENGINEER, "doc.checklist_attachment"),
	(PENDING_CCE, "Reject", REJECTED, COST_CONTROL_ENGINEER, ""),
	(PENDING_CCM, "Approve", PENDING_PCD, COST_CONTROL_MANAGER, ""),
	(PENDING_CCM, "Reject", REJECTED, COST_CONTROL_MANAGER, ""),
	(PENDING_PCD, "Approve", PENDING_ACCT, PLANNING_DIRECTOR, ""),
	(PENDING_PCD, "Reject", REJECTED, PLANNING_DIRECTOR, ""),
	(PENDING_ACCT, "Approve", PENDING_AM, ACCOUNTANT, ""),
	(PENDING_ACCT, "Reject", REJECTED, ACCOUNTANT, ""),
	(PENDING_AM, "Approve", APPROVED, ACCOUNTS_MANAGER, ""),
	(PENDING_AM, "Reject", REJECTED, ACCOUNTS_MANAGER, ""),
	(REJECTED, "Reopen", DRAFT, SITE_ENGINEER, ""),
]


def execute():
	"""Create the Contractor Invoice approval chain.

	Not re-asserted on every migrate - same reasoning as
	create_subcontractor_contract_workflow.py: workflows commonly get
	hand-tuned in the UI after creation, and overwriting those edits on
	every deploy would be worse than leaving this one-shot.
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
