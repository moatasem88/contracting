import frappe



def execute():
   frappe.delete_doc_if_exists('DocType', 'Costing Note', force=True)
   frappe.delete_doc_if_exists('DocType', 'BOQ', force=True)
