import frappe

# 2026-10-18 BRD (Subcontractor Contract / Contractor Invoice / Contract
# Addendum overhaul), FR-01/FR-02/FR-26: both approval workflows converge on
# one chain. Declarative rather than incremental - the live "Subcontractor
# Contract Approval" workflow was never touched by the earlier
# rebuild_subcontractor_contract_approval_chain patch, so this inspects the
# current shape and only rewrites when it differs from the target.
#
#   Draft
#     -> Pending Contracts Manager Approval
#       -> Pending Projects Director Approval
#         -> Pending Cost Control Engineer Review   (needs checklist_attachment)
#           -> Pending Cost Control Approval
#             -> Pending Planning and Cost Control Director Approval
#               -> Approved                  (requires_ceo_approval == 0)
#               -> Pending CEO Approval      (requires_ceo_approval == 1)
#                 -> Approved
#
# NOTE: Subcontractor Contract Approval is also exported as a fixture
# (hooks.py), and bench migrate re-imports fixtures after patches - run
# `bench export-fixtures` after this patch so fixtures/workflow.json matches,
# or the next migrate silently reverts it.

WORKFLOWS = ("Subcontractor Contract Approval", "Subcontractor Contract Addendum Approval")

CM = "Contracts Manager"
PD = "Projects Director"
CCE = "Cost Control Engineer"
CC = "cost control manager"
DIRECTOR = "Planning and Cost Control Director"
CEO = "Company CEO"

DRAFT = "Draft"
PENDING_CM = "Pending Contracts Manager Approval"
PENDING_PD = "Pending Projects Director Approval"
PENDING_CCE = "Pending Cost Control Engineer Review"
PENDING_CC = "Pending Cost Control Approval"
PENDING_DIRECTOR = "Pending Planning and Cost Control Director Approval"
PENDING_CEO = "Pending CEO Approval"
APPROVED = "Approved"
REJECTED = "Rejected"
CANCELLED = "cancelled"

STATES = [
	(DRAFT, "0", CM),
	(PENDING_CM, "0", CM),
	(PENDING_PD, "0", PD),
	(PENDING_CCE, "0", CCE),
	(PENDING_CC, "0", CC),
	(PENDING_DIRECTOR, "0", DIRECTOR),
	(PENDING_CEO, "0", CEO),
	(APPROVED, "1", CM),
	(REJECTED, "0", CM),
	(CANCELLED, "2", CM),
]

# (state, action, next_state, allowed, condition)
TRANSITIONS = [
	(DRAFT, "Submit for Approval", PENDING_CM, CM, ""),
	(PENDING_CM, "Approve", PENDING_PD, CM, ""),
	(PENDING_CM, "Reject", REJECTED, CM, ""),
	(PENDING_PD, "Approve", PENDING_CCE, PD, ""),
	(PENDING_PD, "Reject", REJECTED, PD, ""),
	(PENDING_CCE, "Approve", PENDING_CC, CCE, "doc.checklist_attachment"),
	(PENDING_CCE, "Reject", REJECTED, CCE, ""),
	(PENDING_CC, "Approve", PENDING_DIRECTOR, CC, ""),
	(PENDING_CC, "Reject", REJECTED, CC, ""),
	(PENDING_DIRECTOR, "Approve", APPROVED, DIRECTOR, "doc.requires_ceo_approval == 0"),
	(PENDING_DIRECTOR, "Approve", PENDING_CEO, DIRECTOR, "doc.requires_ceo_approval == 1"),
	(PENDING_DIRECTOR, "Reject", REJECTED, DIRECTOR, ""),
	(PENDING_CEO, "Approve", APPROVED, CEO, ""),
	(PENDING_CEO, "Reject", REJECTED, CEO, ""),
	(REJECTED, "Reopen", DRAFT, CM, ""),
	(APPROVED, "Cancel", CANCELLED, CM, ""),
]


def execute():
	for state, _status, _role in STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc({
				"doctype": "Workflow State", "workflow_state_name": state, "style": "Warning",
			}).insert(ignore_permissions=True)

	for name in WORKFLOWS:
		rebuild(name)


def rebuild(name):
	if not frappe.db.exists("Workflow", name):
		print("skipped (missing): {0}".format(name))
		return

	workflow = frappe.get_doc("Workflow", name)
	current_states = sorted((s.state, str(s.doc_status), s.allow_edit) for s in workflow.states)
	current_transitions = sorted(
		(t.state, t.action, t.next_state, t.allowed, t.condition or "") for t in workflow.transitions
	)
	if current_states == sorted(STATES) and current_transitions == sorted(TRANSITIONS):
		print("already in target shape: {0}".format(name))
		return

	target_states = {s[0] for s in STATES}
	doctype = workflow.document_type
	stranded = frappe.db.sql(
		"select workflow_state, count(*) from `tab{0}` where docstatus = 0 and ifnull(workflow_state, '') != '' "
		"group by workflow_state".format(doctype)
	)
	for state, count in stranded:
		if state not in target_states:
			print("WARNING: {0} {1} doc(s) sit in state {2!r}, which the target chain does not have".format(
				count, doctype, state))

	workflow.set("states", [])
	for state, doc_status, allow_edit in STATES:
		workflow.append("states", {"state": state, "doc_status": doc_status, "allow_edit": allow_edit})

	workflow.set("transitions", [])
	for state, action, next_state, allowed, condition in TRANSITIONS:
		workflow.append("transitions", {
			"state": state, "action": action, "next_state": next_state,
			"allowed": allowed, "condition": condition, "allow_self_approval": 1,
		})

	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	print("rebuilt workflow: {0}".format(name))
