"""Subcontractor deduction tagging across the buying chain.

Material bought on a subcontractor's behalf is tagged once - normally on
the Material Request - and that tag rides along to the Purchase Order,
Purchase Receipt and Purchase Invoice so the real invoiced cost can be
recovered from the subcontractor rather than paid twice.

Propagation through ERPNext's standard "Create" buttons needs no code:
frappe/model/mapper.py:map_fields copies every field that exists on both
source and target and isn't marked no_copy, and these fields are defined
with identical fieldnames and no_copy unset on all four Item doctypes.

What *does* need code is the two gaps that leaves:

* the header -> row cascade, so tagging a document once tags its lines;
* documents created outside the mapper (API, Data Import) that carry a
  source pointer but no tag - `backfill_from_source` repairs those, so
  the flag can't be silently lost.
"""

import frappe
from frappe import _

CHECK = "custom_is_subcontractor_deduction"
SUPPLIER = "custom_deduction_subcontractor"

# Row-level pointer back to the source row each doctype was mapped from.
# Purchase Invoice Item has two: a receipt-based invoice and an invoice
# raised straight off the order.
SOURCE_LINKS = {
	"Purchase Order Item": [("material_request_item", "Material Request Item")],
	"Purchase Receipt Item": [("purchase_order_item", "Purchase Order Item")],
	"Purchase Invoice Item": [
		("pr_detail", "Purchase Receipt Item"),
		("po_detail", "Purchase Order Item"),
	],
}


def cascade_deduction_tag(doc, event=None):
	"""Push the header tag onto any row that hasn't been set differently.

	The header is the usual place to set this; rows stay overridable so one
	document can still mix subcontractor material with ordinary company
	purchases.
	"""
	if not doc.meta.has_field(CHECK):
		return

	if not doc.get(CHECK):
		return

	for row in doc.get("items") or []:
		if not row.meta.has_field(CHECK):
			continue
		# Only fill blanks - never overwrite a row someone set deliberately.
		if not row.get(CHECK):
			row.set(CHECK, 1)
		if row.get(CHECK) and not row.get(SUPPLIER):
			row.set(SUPPLIER, doc.get(SUPPLIER))


def backfill_from_source(doc, event=None):
	"""Recover the tag from each row's source row when it wasn't carried.

	Covers documents created by API or Data Import rather than through the
	standard Create chain: the mapper never ran, so nothing copied the tag,
	but the row still says where it came from.
	"""
	source_links = SOURCE_LINKS.get(doc.doctype + " Item")
	if not source_links:
		return

	for row in doc.get("items") or []:
		if row.get(CHECK):
			continue

		for fieldname, source_doctype in source_links:
			source_name = row.get(fieldname)
			if not source_name:
				continue

			source = frappe.db.get_value(
				source_doctype, source_name, [CHECK, SUPPLIER], as_dict=True
			)
			if source and source.get(CHECK):
				row.set(CHECK, 1)
				row.set(SUPPLIER, source.get(SUPPLIER))
				break


def validate_deduction_rows(doc, event=None):
	"""A flagged row has to name the subcontractor it's recovered from."""
	for row in doc.get("items") or []:
		if row.get(CHECK) and not row.get(SUPPLIER):
			frappe.throw(
				_("Row {0}: this line is flagged as a subcontractor deduction but no "
				  "Deduction Subcontractor is set.").format(row.idx)
			)


def validate_deduction_gl_account(doc, event=None):
	"""FR-22 gate: an invoice with flagged lines needs the GL account set.

	The account is the accountant's per-invoice decision, so it is never
	defaulted. The party is not asked for again - it rides in from the
	Material Request stage.
	"""
	flagged = [row for row in (doc.get("items") or []) if row.get(CHECK)]
	if not flagged:
		return

	if not doc.get("custom_deduction_gl_account"):
		frappe.throw(
			_("{0} line(s) on this invoice are flagged as subcontractor deductions. "
			  "Set the Deduction GL Account before submitting.").format(len(flagged))
		)


def process_purchase_document(doc, event=None):
	"""validate hook for Purchase Order / Receipt / Invoice."""
	backfill_from_source(doc, event)
	cascade_deduction_tag(doc, event)
	validate_deduction_rows(doc, event)
