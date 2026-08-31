# Copyright (c) 2024, kazem and contributors
# For license information, please see license.txt

import frappe
from frappe import _
import json
from frappe.model.document import Document
import frappe.utils

class CostControlPlanning(Document):
    def validate(self):
        self.merge_items()

    def merge_items(self):
        self.costing_note_merge_items = []
        self.purchase_items = []
        for item in self.material_costs:
            self.append("costing_note_merge_items",{

                "item_code": item.item,
                "rate": item.unit_cost,
                "qty":item.qty,
                "remaining_qty": item.qty


            })
            self.append("purchase_items",{

                "item_code": item.item,
                "rate": item.unit_cost,
                "qty":item.qty,
                "remaining_qty": item.qty


            })
        
        for item in self.labor_costs:
            self.append("costing_note_merge_items",{

                "item_code": item.item,
                "rate": item.cost,
                "qty":item.qty,
                "remaining_qty": item.qty



            })
            self.append("purchase_items",{

                "item_code": item.item,
                "rate": item.cost,
                "qty":item.qty,
                "remaining_qty": item.qty



            })
        
        for item in self.contractors_table:
            self.append("costing_note_merge_items",{

                "item_code": item.item,
                "schedule_date": frappe.utils.today(),
                "rate": item.cost,
                "qty":item.qty,
                "remaining_qty": item.qty



            })
            self.append("purchase_items",{

                "item_code": item.item,
                "schedule_date": frappe.utils.today(),
                "rate": item.cost,
                "qty":item.qty,
                "remaining_qty": item.qty



            })

        for item in self.expenses_table:
            self.append("costing_note_merge_items",{

                "item_code": item.item,
                "rate": item.cost,
                "qty":item.qty,
                "remaining_qty": item.qty



            })
            self.append("purchase_items",{

                "item_code": item.item,
                "rate": item.cost,
                "qty":item.qty,
                "remaining_qty": item.qty



            })

        for item in self.equibments:
            self.append("costing_note_merge_items",{

                "item_code": item.item,
                "rate": item.cost,
                "qty":item.qty,
                "remaining_qty": item.qty



            })
            self.append("purchase_items",{

                "item_code": item.item,
                "rate": item.cost,
                "qty":item.qty,
                "remaining_qty": item.qty



            })

    


    @frappe.whitelist()
    def create_tasks(self):
        """
        Create tasks for the given Cost Control Planning document.
        This function can be triggered by a custom button or as a method.
        """
        if not self.tasks:
            frappe.throw(_("No tasks to create."))
            return
        
        for task in self.tasks:
            new_task = frappe.new_doc("Task")
            new_task.project = self.project
            new_task.contracting = 1
            new_task.cost_control_planning = self.name
            new_task.contracting_items = self.item
            new_task.subject = task.subject
            new_task.exp_start_date = task.start_date
            new_task.exp_end_date = task.end_date

            new_task.project  = self.project
            new_task.insert()
            task.task_name = new_task.name

            self.save()
        
        frappe.msgprint("Tasks created successfully.")

@frappe.whitelist()
def append_task_items(args, row):
    if isinstance(args, str):
        args = json.loads(args)

    if isinstance(row, str):
        row = json.loads(row)

    acc_qty = 0

    
        
    for arg in args:
        acc_qty +=  arg.get("qty")
        task = frappe.get_doc("Task",arg.get("task"),"project")
        if arg.get("qty") > row.get("qty"):
            frappe.throw(f"You can't exceed the qty for row {arg.get('idx')}")

       

        if acc_qty > row.get("qty"):
            frappe.throw(f"You can't exceed the qty for row {arg.get('idx')}")

        task_project = task.get("project")
        
        if not task_project:
            task_project = ""

        tasks = frappe.get_all("Task",filters={"project":task_project},pluck='name')
        


        items = frappe.get_all(
            "Task Items",
            filters={"item": row.get("item"), "parent": ["in", tasks]},
            fields=["sum(qty) as total_qty"]  # Add alias for the sum
        )

        total_qty = items[0].total_qty if items[0].total_qty else 0

        if total_qty +  arg.get("qty") > row.get("qty"):
            frappe.throw(f"All available items used in the tasks")

        if frappe.db.exists("Task Items",{"item": row.get("item"),"row_ref": row.get("name"),"parent": arg.get("task")}):
            qty = frappe.db.get_value("Task Items",{"item": row.get("item"),"row_ref": row.get("name"),"parent": arg.get("task")},"qty")

            if not qty:
                qty = 0

            frappe.db.set_value("Task Items",{"item": row.get("item"),"row_ref": row.get("name"),"parent": arg.get("task")},"qty",arg.get("qty") + qty)
            frappe.db.set_value("Task Items",{"item": row.get("item"),"row_ref": row.get("name"),"parent": arg.get("task")},"amount",arg.get("rate") * (arg.get("qty") + qty))

        else:
            task.append("items",
                        {
                            "item": row.get("item"),
                            "qty": arg.get("qty"),
                            "rate" :arg.get("rate"),
                            "row_ref": row.get("name"),
                            "amount": arg.get("rate") * arg.get("qty")
                        })
            
            task.save()
   
    
@frappe.whitelist()
def create_purchase_order(source_name, target_doc=None):
        
    doc = frappe.new_doc("Purchase Order")
    doc.contracting = 1
    doc.cost_control_planning = frappe.flags.args.name
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
        mt.cost_control_planning = frappe.flags.args.name
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