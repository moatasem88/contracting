import frappe

# SUPERSEDED 2026-10-18 by rebuild_contract_and_addendum_approval_chain_2026_10_18.py
# (the live workflow never received this chain; the 2026-10-18 BRD defines the
# final one, with different state names). execute() is a deliberate no-op so
# running it can no longer reintroduce the old shape.

# 2026-09-27 BRD: extends the existing Subcontractor Contract Approval
# workflow in place - the deployed chain (Contracts Manager -> cost control
# manager -> conditional CEO) skips Projects Director, a Cost Control
# Engineer checklist review, and Planning and Cost Control Director, and its
# CEO trigger is a single whole-contract grand_total threshold rather than
# the per-work-item conditions set_ceo_approval_flag() now computes.
#
# New chain:
#   Draft
#     -> Pending Contracts Manager Approval
#       -> Pending Projects Director Approval
#         -> Pending Cost Control Engineer Review (requires checklist_attachment)
#           -> Pending Cost Control Approval
#             -> Pending Cost Control Director Approval
#               -> Approved                     (requires_ceo_approval == 0)
#               -> Pending CEO Approval          (requires_ceo_approval == 1)
#                 -> Approved
#
# allow_edit (Workflow Document State) and allowed (Workflow Transition) are
# both single-value Links to Role - Frappe unions roles across *multiple
# rows sharing the same state* (frappe.workflow.get_document_state_roles /
# frappe.model.workflow.get_transitions both iterate and match on state, not
# assuming one row per state), which is how this workflow's own existing
# CEO-conditional branch already works (two "Pending Cost Control Approval"
# -> Approve rows differing only in condition/next_state). Contracts
# Engineer's access to Draft and to Submit for Approval is added the same
# way: as second rows, not by editing the existing Contracts Manager rows.

WORKFLOW_NAME = "Subcontractor Contract Approval"

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

NEW_STATES = (
	(PENDING_PD, "Warning"),
	(PENDING_CCE, "Warning"),
	(PENDING_CCD, "Warning"),
)


def execute():
	return

	if frappe.db.exists(
		"Workflow Document State", {"parent": WORKFLOW_NAME, "state": PENDING_PD}
	):
		return

	for state, style in NEW_STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc({
				"doctype": "Workflow State", "workflow_state_name": state, "style": style,
			}).insert(ignore_permissions=True)

	workflow = frappe.get_doc("Workflow", WORKFLOW_NAME)

	# -- states: second Draft row + the three new pending states -----------
	workflow.append("states", {
		"state": DRAFT, "doc_status": "0", "allow_edit": CONTRACTS_ENGINEER,
	})
	workflow.append("states", {
		"state": PENDING_PD, "doc_status": "0", "allow_edit": PROJECTS_DIRECTOR,
	})
	workflow.append("states", {
		"state": PENDING_CCE, "doc_status": "0", "allow_edit": COST_CONTROL_ENGINEER,
	})
	workflow.append("states", {
		"state": PENDING_CCD, "doc_status": "0", "allow_edit": PLANNING_DIRECTOR,
	})

	# -- transitions ---------------------------------------------------------

	# Second Draft -> Submit for Approval row for Contracts Engineer.
	workflow.append("transitions", {
		"state": DRAFT, "action": "Submit for Approval", "next_state": PENDING_CM,
		"allowed": CONTRACTS_ENGINEER, "condition": "", "allow_self_approval": 1,
	})

	# Pending Contracts Manager Approval's Approve now goes to Projects
	# Director, not straight to Cost Control.
	_retarget(workflow, PENDING_CM, "Approve", CONTRACTS_MANAGER, PENDING_PD)

	# New Projects Director step.
	workflow.append("transitions", {
		"state": PENDING_PD, "action": "Approve", "next_state": PENDING_CCE,
		"allowed": PROJECTS_DIRECTOR, "condition": "", "allow_self_approval": 1,
	})
	workflow.append("transitions", {
		"state": PENDING_PD, "action": "Reject", "next_state": REJECTED,
		"allowed": PROJECTS_DIRECTOR, "condition": "", "allow_self_approval": 1,
	})

	# New Cost Control Engineer checklist review.
	workflow.append("transitions", {
		"state": PENDING_CCE, "action": "Approve", "next_state": PENDING_CC,
		"allowed": COST_CONTROL_ENGINEER, "condition": "doc.checklist_attachment",
		"allow_self_approval": 1,
	})
	workflow.append("transitions", {
		"state": PENDING_CCE, "action": "Reject", "next_state": REJECTED,
		"allowed": COST_CONTROL_ENGINEER, "condition": "", "allow_self_approval": 1,
	})

	# Pending Cost Control Approval's two Approve rows used to branch
	# straight to Approved/Pending CEO Approval - collapse to a single
	# unconditional row targeting the new Director state; the branch moves
	# one state later.
	_remove_transitions(workflow, PENDING_CC, "Approve")
	workflow.append("transitions", {
		"state": PENDING_CC, "action": "Approve", "next_state": PENDING_CCD,
		"allowed": COST_CONTROL, "condition": "", "allow_self_approval": 1,
	})

	# New Planning and Cost Control Director step - this is where the
	# CEO-conditional branch now lives.
	workflow.append("transitions", {
		"state": PENDING_CCD, "action": "Approve", "next_state": APPROVED,
		"allowed": PLANNING_DIRECTOR, "condition": "doc.requires_ceo_approval == 0",
		"allow_self_approval": 1,
	})
	workflow.append("transitions", {
		"state": PENDING_CCD, "action": "Approve", "next_state": PENDING_CEO,
		"allowed": PLANNING_DIRECTOR, "condition": "doc.requires_ceo_approval == 1",
		"allow_self_approval": 1,
	})
	workflow.append("transitions", {
		"state": PENDING_CCD, "action": "Reject", "next_state": REJECTED,
		"allowed": PLANNING_DIRECTOR, "condition": "", "allow_self_approval": 1,
	})

	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	print("rebuilt workflow: {0}".format(WORKFLOW_NAME))


def _retarget(workflow, state, action, allowed, new_next_state):
	for t in workflow.transitions:
		if t.state == state and t.action == action and t.allowed == allowed:
			t.next_state = new_next_state


def _remove_transitions(workflow, state, action):
	workflow.transitions = [
		t for t in workflow.transitions if not (t.state == state and t.action == action)
	]
