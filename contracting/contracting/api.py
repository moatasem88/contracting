import frappe 

from frappe.utils import add_days, cint, cstr, flt, get_link_to_form, getdate, nowdate, strip_html

@frappe.whitelist()
def create_costing_note(source_name, target_doc=None):
    project = frappe.get_doc("Project",source_name)
    doc = frappe.new_doc("Costing Note")
    doc.project = source_name
    doc.customer = project.customer
    doc.tender_number = project.number_
    for item in project.contracting_items_child:
        doc.append("costing_note_items",{
            "is_group" : item.is_group,
            "series":item.series,
            "contracting_item_group":item.contracting_item_group,
            "item":item.contracting_items,
            "uom":item.uom,
            "qty":item.qty,
            # "total_cost":item.total
        })
    return doc


@frappe.whitelist()
def create_boq(source_name, target_doc=None):
    costing_note = frappe.get_doc("Costing Note",source_name)
    doc = frappe.new_doc("BOQ")
    doc.contracting_item_group = frappe.flags.args.contracting_item_group
    doc.unit = frappe.flags.args.uom
    doc.customer = costing_note.customer
    doc.item = frappe.flags.args.item
    doc.project_qty = frappe.flags.args.qty
    doc.project = costing_note.project
    doc.costing_note = source_name
    doc.line_id = frappe.flags.args.row
    return doc




@frappe.whitelist()
def create_quotation(source_name, target_doc=None):
    project = frappe.get_doc("Project",source_name)
    doc = frappe.new_doc("Quotation")
    doc.project = source_name
    doc.company = project.company
    doc.quotation_to = "Customer"
    doc.party_name = project.customer
    doc.transaction_date = getdate()
    for item in project.contracting_items_child:
        #if item.contracting_items:
            item_doc = frappe.get_doc("Item",item.contracting_items) if item.contracting_items else {"item_name":""} 
            doc.append("items",{
                "is_group"   :item.is_group,
                "series"     :item.series,
                "item_groups" :item.contracting_item_group,
                "item_code"  :item.contracting_items if item.contracting_items else "",
                "item_name"  :item_doc.get("item_name"),
                "uom"        :item.uom,
                "qty"        :item.qty,
                "rate"       :item.price,
                "amount"     :item.total,
                "project"    :source_name,
                "description":item_doc.get("item_name")
            })

    return doc



@frappe.whitelist()
def create_contract_from_quotation(source_name, target_doc=None):
    quotation = frappe.get_doc("Quotation",source_name)
    contract = frappe.new_doc("Contract Document")
    contract.company       = quotation.company
    contract.customer      = quotation .party_name
    contract.order_type    = quotation.order_type
    contract.transaction_date = getdate()
    if quotation.items[0].get("project"):
        contract.project = quotation.items[0].get("project")
    for item in quotation.items:
        #item_doc = frappe.get_doc("Item",item.item)
        contract.append("items",{
            "is_group"   :item.is_group,
            "series"     :item.series,
            "item_group" :item.item_groups,
            "item_code"  :item.item_code,
            "item_name"  :item.item_name,
            "uom"        :item.uom,
            "qty"        :item.qty,
            "rate"       :item.rate,
            "amount"     :item.amount,
            "project"    :item.project,
            "description":item.description,

        })

    return contract

@frappe.whitelist()
def install_app_requirements():
    print("+from fun")
    create_quotation_setters()
    create_jl_setters()
    add_company_custom_script()

def create_quotation_setters():
    create_disable_setter(["item_code", "description", "uom", "item_name"])

def add_company_custom_script():
    name = "Company-Form"
    if not frappe.db.exists("Client Script", name):
        doc = frappe.new_doc("Client Script")
        doc.name = name
    else:
        doc = frappe.get_doc("Client Script", name)
    
    doc.dt = "Company"
    doc.enabled = 1
    doc.script = """
        frappe.ui.form.on('Company', {
            refresh(frm) {
                frm.set_query("supplier_advance_payments_account", function() {
                    return {
                        "filters": {
                            "is_group": ["=", 0],
                            "account_type": "Receivable"
                        }
                    }
                });
                frm.set_query("insurance_account_with_others", function() {
                    return {
                        "filters": {
                            "is_group": ["=", 0],
                            "account_type": "Receivable"
                        }
                    }
                });
                frm.set_query("customer_advance_payments_account", function() {
                    return {
                        "filters": {
                            "is_group": ["=", 0],
                            "account_type": "Receivable"
                        }
                    }
                });
                frm.set_query("third_party_insurance_account", function() {
                    return {
                        "filters": {
                            "is_group": ["=", 0],
                            "account_type": "Receivable"
                        }
                    }
                });
            }
        });
    """
    try:
        doc.save()
        frappe.db.commit()
        print(f"Custom script '{name}' has been saved successfully.")
    except frappe.ValidationError as e:
        print(f"Validation error: {str(e)}")
    except Exception as e:
        print(f"An error occurred while saving the custom script: {str(e)}")

def create_jl_setters():
    doc = frappe.new_doc("Property Setter")
    doc.doctype_or_field = "DocField"
    doc.doc_type = "Journal Entry Account"
    doc.field_name = "reference_type"
    doc.property = "options"
    doc.property_type = "Text"
    doc.value = "\nSales Invoice\nPurchase Invoice\nJournal Entry\nSales Order\nPurchase Order\nExpense Claim\nAsset\nLoan\nPayroll Entry\nEmployee Advance\nExchange Rate Revaluation\nInvoice Discounting\nFees"
    doc.save()



def  create_disable_setter(field):
    for i in range(0,len(field)):
        doc = frappe.new_doc("Property Setter")
        doc.doctype_or_field = "DocField"
        doc.doc_type = "Quotation Item"
        doc.field_name = field[i]
        doc.property = "reqd"
        doc.property_type = "Check"
        doc.value = "0"
        doc.save()




def get_qtys(contract_name,cuurent_qty):
    # 1 - get remaining_qty
    remianing_sql = ""
    # 2-previous qty

    # 3- completed qty

    pass

@frappe.whitelist()
def create_bank_gurantee(source_name, target_doc=None):
    project = frappe.get_doc("Project",source_name)
    doc = frappe.new_doc("Bank Guarantee")
    doc.project = source_name
    doc.customer = project.customer
    return doc
@frappe.whitelist()
def create_payment_entry(source_name, target_doc=None):
    doc = frappe.new_doc("Payment Entry")
    doc.project = source_name
    #doc.customer = customer
    return doc


import json
@frappe.whitelist()
def calculate_qty(doc,*args,**Kwargs):
    doc = json.loads(doc)
    total_qty  = 0
    completed_qty = 0
    group_name = ""
    for item in doc.get("contracting_items_child"):
        if not item.get("is_group"):
            total_qty += float(item.get("qty") or 0)
            completed_qty += float(item.get("completed_qty") or 0)
            group_name = item.get("contracting_item_group")
        else:
            if total_qty>0:
                try:
                    sql = f"""
                            update `tabItems Summary` set qty={total_qty} , completed_qty = completed_qty + {completed_qty} ,completion_percentage=completed_qty / qty * 100 where contracting_item_group='{group_name}' and parent='{doc.get("name")}'
                        """
                    frappe.db.sql(sql)
                    frappe.db.commit()
                    total_qty  = 0
                    completed_qty=0
                    group_name = ""
                except:
                    pass
    sql = f"""
					update `tabItems Summary` set qty={total_qty} , completed_qty = completed_qty + {completed_qty} ,completion_percentage=completed_qty / qty * 100 where contracting_item_group='{group_name}' and parent='{doc.get("name")}'
				"""
    try:
        frappe.db.sql(sql)
        frappe.db.commit()
    except:
        pass
    