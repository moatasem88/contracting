import frappe
from frappe import _
from frappe.utils import getdate, today

# 2026-10-18 BRD (FR-03/FR-07/FR-26/FR-27): guards shared by Subcontractor
# Contract and Contractor Contract Addendum, which run the same approval
# chain. Base DocPerm write is granted to every workflow role (each needs it
# to save its own transition), and Workflow allow_edit is UI-only, so these
# server-side checks are the real enforcement - same reasoning as
# ContractorInvoice._enforce_attachment_windows.

CHECKLIST_STATE = "Pending Cost Control Engineer Review"


def enforce_checklist_window(doc):
	"""checklist_attachment may only change while the document sits in the
	Cost Control Engineer's review state (and be set at all only there)."""
	before = doc.get_doc_before_save()
	if not before:
		return
	state = before.workflow_state or "Draft"
	if (doc.checklist_attachment or None) != (before.checklist_attachment or None) and state != CHECKLIST_STATE:
		frappe.throw(
			_("{0} can only be changed while this document is in state {1}.").format(
				_(doc.meta.get_label("checklist_attachment")), frappe.bold(CHECKLIST_STATE)
			)
		)


def protect_effective_date(doc):
	"""effective_date is stamped by stamp_effective_date and never edited:
	read_only alone does not stop an API write, so any change is reverted
	(before it was stamped) or rejected (after)."""
	before = doc.get_doc_before_save()
	original = before.effective_date if before else None
	if _date(doc.effective_date) == _date(original):
		return
	if original:
		frappe.throw(_("Effective Date is set automatically on approval and cannot be changed."))
	doc.effective_date = None


def stamp_effective_date(doc):
	"""Called from before_submit - the single point every route to Approved
	(Director, or CEO when required) passes through."""
	if not doc.effective_date:
		doc.effective_date = today()


def _date(value):
	return getdate(value) if value else None
