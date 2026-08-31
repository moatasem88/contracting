import frappe
from frappe import _

from contracting.contracting.utils import allocation
from contracting.contracting.utils.budget import validate_items_qty_with_tolerance
from contracting.contracting.utils.deduction import cascade_deduction_tag


def get_project_type(doc,method):
    if doc.costing_note:
        costing_note_name = frappe.get_all("Costing Note", filters={"name": doc.costing_note}, pluck="name")
        project_type=frappe.get_value("Costing Note",costing_note_name,"project_type")
        doc.custom_project_type=project_type



def validate_items_qty(doc, event):
    validate_work_item_scope(doc)
    cascade_deduction_tag(doc)
    validate_items_qty_with_tolerance(doc)


def validate_work_item_scope(doc):
    """Restrict a Material Request to the materials budgeted under the work
    items it says it's for.

    Three cases:

    * `custom_is_general_material` set - the request isn't site work
      (hospitality, office supplies), so any coded stock item is fair game
      and the work-item table is ignored entirely.
    * No work items selected - unscoped request. Left alone, so every
      Material Request that predates this feature keeps working exactly as
      before.
    * Work items selected - items must be ones those work items actually
      budget for, and each line records which work item it draws against.
    """
    if doc.get("custom_is_general_material"):
        doc.set("custom_work_items", [])
        for item in doc.get("items"):
            item.custom_project_work_item = None
        return

    work_item_rows = doc.get("custom_work_items") or []
    if not work_item_rows:
        return

    _fill_work_item_labels(work_item_rows)

    project_work_items = [row.project_work_item for row in work_item_rows if row.project_work_item]
    budgeted = allocation.get_budgeted_items(project_work_items)

    if not budgeted:
        frappe.throw(
            _("The selected work items have no material budget on their Tender, "
              "so there is nothing to request against them. Tick '{0}' if this "
              "request isn't for site work.").format(_("General / Non-Site Material"))
        )

    for item in doc.get("items"):
        owners = budgeted.get(item.item_code)

        if not owners:
            frappe.throw(
                _("Row {0}: {1} is not budgeted under any of the selected work items. "
                  "Add the work item that covers it, or tick '{2}' if this request "
                  "isn't for site work.").format(
                    item.idx, frappe.bold(item.item_code), _("General / Non-Site Material")
                )
            )

        if item.custom_project_work_item:
            if item.custom_project_work_item not in owners:
                frappe.throw(
                    _("Row {0}: work item {1} does not budget for {2}.").format(
                        item.idx, frappe.bold(item.custom_project_work_item), frappe.bold(item.item_code)
                    )
                )
            continue

        if len(owners) == 1:
            # Unambiguous - don't make the user restate it.
            item.custom_project_work_item = next(iter(owners))
            continue

        frappe.throw(
            _("Row {0}: {1} is budgeted under more than one of the selected work items "
              "({2}). Set the Work Item on the row so the budget and any subcontractor "
              "conflict are measured against the right one.").format(
                item.idx, frappe.bold(item.item_code), ", ".join(sorted(owners))
            )
        )


def _fill_work_item_labels(work_item_rows):
    for row in work_item_rows:
        if not row.project_work_item:
            continue
        values = frappe.db.get_value(
            "Project BOQ Item", row.project_work_item, ["parent_item", "source_tender_boq_item"], as_dict=True
        )
        if values:
            row.work_item_name = values.parent_item
            row.source_tender_boq_item = values.source_tender_boq_item


def update_remaining_qty_on_submit(doc, method):
    if not doc.contracting:
        return
    
    for item in doc.items:
        # frappe.throw(str(item.item_row))

        tot_qty = 0
        qty = frappe.get_value("Costing Note Merge Item", item.item_row, "remaining_qty")

        if not qty:
            tot_qty = item.qty
        else:
            tot_qty = float(qty) - item.qty
        
        if not qty:
            continue

  
        if item.qty > float(qty) :
            frappe.throw("You can't exceed document qty")

        frappe.db.set_value("Costing Note Merge Item", item.item_row, "remaining_qty", tot_qty)
        

def restore_qty_on_cancel_or_delete(doc, method):
    if not doc.contracting:
        return
    
    for item in doc.items:
        tot_qty = 0
        qty = frappe.get_value("Costing Note Merge Item", item.item_row, "remaining_qty")

        if not qty:
            tot_qty = item.qty
        else:
            tot_qty =  float(qty) + item.qty
        
        frappe.db.set_value("Costing Note Merge Item", item.item_row, "remaining_qty", tot_qty)