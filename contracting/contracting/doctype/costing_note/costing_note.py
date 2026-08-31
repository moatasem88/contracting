# Copyright (c) 2024, kazem and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

class CostingNote(Document):
    @frappe.whitelist()
    def get_costing_note_template_data(self):
        template = frappe.get_doc("Costing Note Template", self.costing_note_template)
        data = {
            "material_costs": template.material_costs,
            "labor_costs": template.labor_costs,
            "contractors_table": template.contractors_table,
            "expenses_table": template.expenses_table,
            "equibments": template.equibments,
            "total_material_costs" : template.total_material_costs,
            "total_labor_costs" : template.total_labor_costs,
            "total_contractors" : template.total_contractors,
            "total_expenses" : template.total_expenses,
            "total_equibments_cost" : template.total_equibments_cost,
            "total_cost" : template.total_cost

        }
        return data


    def validate(self):
        self.validate_total_costs()
        # self.merge_items()

    # def merge_items(self):
    #     self.costing_note_merge_items = []
    #     self.purchase_items = []
    #     for item in self.material_costs:
    #         self.append("costing_note_merge_items",{

    #             "item_code": item.item,
    #             "rate": item.unit_cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty


    #         })
    #         self.append("purchase_items",{

    #             "item_code": item.item,
    #             "rate": item.unit_cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty


    #         })
        
    #     for item in self.labor_costs:
    #         self.append("costing_note_merge_items",{

    #             "item_code": item.item,
    #             "rate": item.cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty



    #         })
    #         self.append("purchase_items",{

    #             "item_code": item.item,
    #             "rate": item.cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty



    #         })
        
    #     for item in self.contractors_table:
    #         self.append("costing_note_merge_items",{

    #             "item_code": item.item,
    #             "schedule_date": today(),
    #             "rate": item.cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty



    #         })
    #         self.append("purchase_items",{

    #             "item_code": item.item,
    #             "schedule_date": today(),
    #             "rate": item.cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty



    #         })

    #     for item in self.expenses_table:
    #         self.append("costing_note_merge_items",{

    #             "item_code": item.item,
    #             "rate": item.cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty



    #         })
    #         self.append("purchase_items",{

    #             "item_code": item.item,
    #             "rate": item.cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty



    #         })

    #     for item in self.equibments:
    #         self.append("costing_note_merge_items",{

    #             "item_code": item.item,
    #             "rate": item.cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty



    #         })
    #         self.append("purchase_items",{

    #             "item_code": item.item,
    #             "rate": item.cost,
    #             "qty":item.qty,
    #             "remaining_qty": item.qty



    #         })



    def validate_total_costs(self):
        total = 0 
        self.total_profit_amount = 0
        self.cost_value = 0

        for cost in self.material_costs:
            total += float(cost.unit_cost or 0)

        for cost in self.labor_costs:
            total += float(cost.cost or 0)

        for cost in self.contractors_table:
            total += float(cost.cost or 0)

        for cost in self.expenses_table:
            total += float(cost.cost or 0)

        for cost in self.equibments:
            total += float(cost.cost or 0)

        total_cost = float(self.total_cost or 0)  

        if self.indirect_based_on == "Percent":
            self.cost_value = total_cost + (total_cost * (float(self.indirect_cost_percentage or 0) / 100))
        else:
            self.cost_value = total_cost + float(self.indirect_cost_amount or 0)

        self.total_profit_amount  = self.cost_value
        
        if self.profit_based_on == "Percent":
            self.total_profit_amount = self.cost_value + (self.cost_value * (float(self.profit_percentage or 0) / 100))
        else:
            self.total_profit_amount = self.cost_value + float(self.profit_amount or 0)

    def on_submit(self):
        if self.line_id:
            sql = f"""
            update `tabCosting Note Items` set total_cost ='{self.total_cost}',boq_link='{self.name}' where name='{self.line_id}' 
            """
            frappe.db.sql(sql)
            frappe.db.commit()

        self.set_tender_total_cost()
    def on_cancel(self):
        self.reset_tender_total_cost()

    def set_tender_total_cost(self):
        if self.cost_value:
            frappe.db.set_value("Contracting Items Child",self.row,"total_cost",self.cost_value)
        if self.total_profit_amount and self.project_qty:
            frappe.db.set_value("Contracting Items Child",self.row,"rate",self.total_profit_amount / float(self.project_qty)) 

        frappe.db.set_value("Contracting Items Child",self.row,"costing_note",self.name)

            


    def reset_tender_total_cost(self):
        frappe.db.set_value("Contracting Items Child",self.row,"total_cost",0)
        frappe.db.set_value("Contracting Items Child",self.row,"rate",0)
        frappe.db.set_value("Contracting Items Child",self.row,"costing_note"," ")


@frappe.whitelist()
def create_cost_control_planning(source_name, target_doc=None):
        
        cp = frappe.new_doc("Cost Control Planning")
        cp.contracting_item_group = frappe.flags.args.contracting_item_group
        cp.item = frappe.flags.args.item
        cp.project_qty = frappe.flags.args.project_qty
        cp.unit = frappe.flags.args.unit
        cp.project = frappe.flags.args.project
        cp.costing_note = frappe.flags.args.name

        cp.tender = frappe.flags.args.tender
        cp.project_qty = frappe.flags.args.project_qty
        cp.costing_note_template = frappe.flags.args.costing_note_template
        cp.expected_time_period = frappe.flags.args.expected_time_period
        cp.project_qty = frappe.flags.args.project_qty
        cp.custom_project_type= frappe.flags.args.project_type

        for item in frappe.flags.args.material_costs_items:
            cp.append("material_costs",{

                "item": item.get("item"),
                "qty": item.get("qty"),
                "depreciasion_percentage": item.get("depreciasion_percentage"),
                "total_expected_amount":item.get("total_expected_amount"),
                "unit_cost":item.get("unit_cost"),
                "vat":item.get("vat"),
                "vat_amount":item.get("vat_amount"),
                "total_cost_with_vat":item.get("total_cost_with_vat")
            })
        for item in frappe.flags.args.labor_costs:
            cp.append("labor_costs",{

                "item": item.get("item"),
                "uom": item.get("uom"),
                "qty": item.get("qty"),
                "cost": item.get("cost"),
                "total_cost":item.get("total_cost"),
                "vat":item.get("vat"),
                "vat_amount":item.get("vat_amount"),
                "total_cost_with_vat":item.get("total_cost_with_vat")
            })
        
        for item in frappe.flags.args.contractors_table:
            cp.append("contractors_table",{

                "item": item.get("item"),
                "uom": item.get("uom"),
                "qty": item.get("qty"),
                "cost": item.get("cost"),
                "total_cost":item.get("total_cost"),
                "vat":item.get("vat"),
                "vat_amount":item.get("vat_amount"),
                "total_cost_with_vat":item.get("total_cost_with_vat")
            })
        
        for item in frappe.flags.args.expenses_table:
            cp.append("expenses_table",{

                "item": item.get("item"),
                "uom": item.get("uom"),
                "qty": item.get("qty"),
                "cost": item.get("cost"),
                "total_cost":item.get("total_cost"),
                "vat":item.get("vat"),
                "vat_amount":item.get("vat_amount"),
                "total_cost_with_vat":item.get("total_cost_with_vat")
            })

        for item in frappe.flags.args.equibments:
            cp.append("equibments",{

                "item": item.get("item"),
                "uom": item.get("uom"),
                "qty": item.get("qty"),
                "cost": item.get("cost"),
                "total_cost":item.get("total_cost"),
                "vat":item.get("vat"),
                "vat_amount":item.get("vat_amount"),
                "total_cost_with_vat":item.get("total_cost_with_vat")
            })
     
            
        return cp