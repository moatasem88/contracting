import frappe
from frappe.utils import add_days, cint, cstr, flt, get_link_to_form, getdate, nowdate, strip_html
from erpnext.projects.doctype.project.project import Project


class CustomProject(Project):
    pass
    
    #    def calculate_gross_margin(self):
    #         expense_amount = (
    #             flt(self.total_costing_amount)
    #             + flt(self.total_purchase_cost)
    #             + flt(self.get("total_consumed_material_cost", 0))+
    #             flt(self.total_expense) if self.total_expense else 0
    #         )

    #         self.gross_margin = (flt(self.total_billed_amount)+flt(self.total_revenue)) - expense_amount
    #         if self.total_billed_amount or self.total_revenue:
    #             self.per_gross_margin = (self.gross_margin / (flt(self.total_billed_amount)+flt(self.total_revenue))) * 100
