import frappe

def get_project_type(doc,method):
    if doc.project:
        project_type = frappe.db.get_value("Project", doc.project,"project_type")
        doc.project_type=project_type