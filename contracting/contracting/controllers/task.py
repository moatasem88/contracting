import frappe


def merge_items(doc,event):
        doc.costing_note_merge_items = []
        doc.purchase_items = []
        for item in doc.items:
            doc.append("costing_note_merge_items",{

                "item_code": item.item,
                "rate": item.rate,
                "qty":item.qty,
                "remaining_qty": item.qty


            })
            doc.append("purchase_items",{

                "item_code": item.item,
                "rate": item.rate,
                "qty":item.qty,
                "remaining_qty": item.qty


            })
        
        
@frappe.whitelist()
def create_purchase_order(source_name, target_doc=None):
        
    doc = frappe.new_doc("Purchase Order")
    doc.contracting = 1
    doc.task = frappe.flags.args.name
    doc.project = frappe.flags.args.project
   

    for x in frappe.flags.args.item:
        if not x.get("qty"):
            continue

        item_name = frappe.get_value("Item",x.get("item_code"),"item_name")
        stock_uom = frappe.get_value("Item",x.get("item_code"),"stock_uom")

        doc.append("items",{
                        "item_code":x.get("item_code"),
                        "schedule_date": frappe.utils.today(),
                        "rate":x.get("rate"),
                        "qty":x.get("qty"),
                        "allowed_qty":x.get("allowed_qty"),
                        "stock_uom":stock_uom,
                        "uom": stock_uom,
                        "item_name": item_name,
                        "description":item_name,
                        "item_row":x.get("item_row")
            } )


    return doc


@frappe.whitelist()
def create_material_request(source_name, target_doc=None):
        mt = frappe.new_doc("Material Request")
        mt.contracting = 1
        mt.task = frappe.flags.args.name
        mt.project = frappe.flags.args.project
  
        for item in frappe.flags.args.costing_note_merge_items:
            if not item.get("qty"):
                continue
            
            is_stock_item = frappe.get_value("Item", item.get("item_code") , "is_stock_item")
            item_name = frappe.get_value("Item",item.get("item_code"),"item_name")
            stock_uom = frappe.get_value("Item",item.get("item_code"),"stock_uom")
            
            if not is_stock_item:
                continue

            mt.append("items",{

                "item_code": item.get("item_code"),
                "schedule_date": frappe.utils.today(),
                "rate": item.get("rate"),
                "qty":item.get("qty"),
                "allowed_qty":item.get("allowed_qty"),
                "item_row":item.get("item_row"),
                "stock_uom":stock_uom,
                "uom": stock_uom,
                "item_name": item_name,
                "description":item_name,
                "item_row":item.get("item_row")
            })
        return mt