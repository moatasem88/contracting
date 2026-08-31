import frappe
import erpnext
from frappe.utils import today
import json
from frappe import _


@frappe.whitelist()
def create_contract(doc):
    if isinstance(doc, str):
        doc = json.loads(doc)

    con = frappe.new_doc("Contract Document")
    con.date = today()
    con.customer = doc.get("party_name")
    con.order_type = "Sales"
    con.conversion_rate = 1
    con.contract_owner = 1
    con.price_list_currency = erpnext.get_company_currency(erpnext.get_default_company())
    con.project = doc.get("project")
    con.plc_conversion_rate = 1
    con.quotation = doc.get("name")
    for item in doc.get("tender_items"):
        con.append("items",
                {
                    "is_group":item.get("is_group"),
                    "status":item.get("status"),
                    "series":item.get("series"),
                    "contracting_item_group":item.get("contracting_item_group"),
                    "contracting_item":item.get("contracting_item"),
                    "uom":item.get("uom"),
                    "qty":item.get("qty"),
                    "remaining_qty":item.get("qty"),
                    "rate":item.get("rate"),
					"description": item.get("description") 

                })
    con.insert()

    link_to_je = frappe.utils.get_url_to_form("Contract Document", con.name)
    frappe.msgprint(
            _("Contract created successfully. <a href='{0}'>{1}</a>").format(link_to_je, con.name),
            alert=True,
            indicator="green",
        )


def create_contract_from_tender(tender):
    """Tender-Won automation: build the customer-facing Contract Document
    (this app's Sales-Order-equivalent) straight from a won Tender's BOQ,
    at each line's sell_rate - sibling to create_contract() above, which
    is keyed off a submitted Quotation instead."""
    company = erpnext.get_default_company()

    con = frappe.new_doc("Contract Document")
    con.date = today()
    con.customer = tender.client
    con.order_type = "Sales"
    con.company = company
    con.conversion_rate = 1
    con.contract_owner = 1
    con.currency = erpnext.get_company_currency(company)
    con.price_list_currency = erpnext.get_company_currency(company)
    con.selling_price_list = frappe.db.get_value("Price List", {"selling": 1}, "name")
    con.plc_conversion_rate = 1
    con.project = tender.project
    con.tender = tender.name

    for row in tender.boq_items:
        if row.is_group:
            continue
        con.append("items", {
            "is_group": 0,
            "contracting_item_group": work_item_billing_group(tender, row),
            "contracting_item": row.item_code,
            "uom": row.uom,
            "qty": row.original_quantity,
            "remaining_qty": row.original_quantity,
            "rate": row.sell_rate,
            "description": row.item_name,
        })

    con.insert(ignore_permissions=True)
    return con


def work_item_billing_group(tender, boq_row):
    """A work item is a composite of Material/Labor/Equipment now (see
    Phase B.2), not itself an Item - it has no item_code of its own to
    read an Item Group from. contracting_item_group is mandatory on
    Tender Item, so fall back to whichever Item Group its first linked
    resource belongs to, and to "All Item Groups" (always present) if
    the work item has no resources at all. Real per-work-item billing
    items are a follow-up, not solved here."""
    for fieldname in ("material_items", "labor_items", "equipment_items"):
        for resource_row in tender.get(fieldname):
            if resource_row.work_item == boq_row.idx and resource_row.item:
                item_group = frappe.db.get_value("Item", resource_row.item, "item_group")
                if item_group:
                    return item_group
    return "All Item Groups"


# @frappe.whitelist()
# def create_subcontractor(source_name, target_doc=None):
#     con = frappe.new_doc("Contract Document")
#     con.date = today()
#     con.supplier = frappe.flags.args.supplier
#     con.order_type = "Sales"
#     con.conversion_rate = 1
#     con.contract_owner = 0
#     con.price_list_currency = erpnext.get_company_currency(erpnext.get_default_company())
#     con.project = frappe.flags.args.project
#     con.plc_conversion_rate = 1
#     con.quotation =  frappe.flags.args.name

#     for x in frappe.flags.args.item:
   
#         con.append("items",
#                 {
#                     "is_group":x.get("is_group"),
#                     "status":x.get("status"),
#                     "series":x.get("series"),
#                     "contracting_item_group":x.get("contracting_item_group"),
#                     "uom":x.get("uom"),
#                     "qty":x.get("qty"),
#                     "remaining_qty":x.get("qty"),
#                     "rate":x.get("rate"),
# 					"description": x.get("description") 

#                 })
#     return con

@frappe.whitelist()
def get_costing_note_items(doc):
    if isinstance(doc, str):
        doc = frappe.parse_json(doc)

    note = frappe.get_doc("Costing Note", doc.get("costing_note"))

    items = []

    # cost_tables = [
    #     ("material_costs", "unit_cost"),
    #     ("labor_costs", "cost"),
    #     ("contractors_table", "cost"),
    #     ("expenses_table", "cost"),
    #     ("equibments", "cost")
    # ]

    for row in note.get("purchase_items"):
            if not  row.get("remaining_qty"):
                 continue
            
            item_name = frappe.get_value("Item", row.get("item_code"), "item_name")
            stock_uom = frappe.get_value("Item", row.get("item_code"), "stock_uom")
            items.append({
                "item_code": row.get("item_code"),
                "rate": row.get("rate"),
                "qty": row.get("remaining_qty"),
                "allowed_qty": row.get("remaining_qty"),
                "item_name": item_name,
                "schedule_date": today(),
                "description": item_name,
                "stock_uom": stock_uom,
                "uom":stock_uom,
                "conversion_factor":1,
                "item_row":row.get("name")


            })
    

    return items


@frappe.whitelist()
def get_costing_note_items_mt(doc):
    if isinstance(doc, str):
        doc = frappe.parse_json(doc)

    note = frappe.get_doc("Costing Note", doc.get("costing_note"))

    items = []

    # cost_tables = [
    #     ("material_costs", "unit_cost"),
    #     ("labor_costs", "cost"),
    #     ("contractors_table", "cost"),
    #     ("expenses_table", "cost"),
    #     ("equibments", "cost")
    # ]

    for row in note.get("costing_note_merge_items"):
            if not  row.get("remaining_qty"):
                 continue
            
            is_stock_item = frappe.get_value("Item", row.item_code , "is_stock_item")
            
            if not is_stock_item:
                continue
            
            item_name = frappe.get_value("Item", row.get("item_code"), "item_name")
            stock_uom = frappe.get_value("Item", row.get("item_code"), "stock_uom")
            items.append({
                "item_code": row.get("item_code"),
                "rate": row.get("rate"),
                "qty": row.get("remaining_qty"),
                "allowed_qty": row.get("remaining_qty"),
                "item_name": item_name,
                "schedule_date": today(),
                "description": item_name,
                "stock_uom": stock_uom,
                "uom":stock_uom,
                "conversion_factor":1,
                "item_row":row.get("name")


            })
    

    return items