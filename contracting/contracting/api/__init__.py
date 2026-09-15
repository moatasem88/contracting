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
    create_work_item_group()
    create_quotation_setters()
    create_jl_setters()
    add_company_custom_script()
    apply_subcontracting_custom_fields()
    apply_tax_charge_type_fetch_setters()


def apply_subcontracting_custom_fields():
    """Re-assert the custom fields this app adds to native doctypes.
    Idempotent - create_custom_fields(update=True) updates in place."""
    from contracting.contracting.custom_fields import apply_custom_fields

    apply_custom_fields()


def apply_tax_charge_type_fetch_setters():
    """Re-assert the fetch_from Property Setters wiring Sales/Purchase
    Taxes and Charges to custom_tax_charge_type. Idempotent, same
    reasoning as apply_subcontracting_custom_fields() above."""
    from contracting.contracting.custom_fields import apply_tax_charge_type_fetch_setters

    apply_tax_charge_type_fetch_setters()


WORK_ITEM_GROUP = "Work Item"


def create_work_item_group():
    """The Item Group every auto-created work item lands in (see
    Work Item Template V2's sync_work_item). Sibling of the existing
    "Labor" / "Equipment" groups, which are the same shape: a flat
    child of All Item Groups, not a group node.

    Called from install_app_requirements (after_migrate) and again,
    defensively, from the template's own Item-sync path - after_install
    is commented out in hooks.py, so a fresh install would otherwise
    never get this group and the first template save would fail on a
    link validation error.
    """
    if frappe.db.exists("Item Group", WORK_ITEM_GROUP):
        return

    group = frappe.get_doc({
        "doctype": "Item Group",
        "item_group_name": WORK_ITEM_GROUP,
        "parent_item_group": "All Item Groups",
        "is_group": 0,
    })

    # Seeding the income account here means Item.update_defaults_from_item_group
    # copies it onto every work item at creation, so the Sales Invoice derives
    # a real contracting revenue account instead of falling back to
    # Contracting Settings.revenue_account - which is a bank-interest account.
    company = frappe.db.get_value("Company", {}, ["name", "default_income_account"], as_dict=True)
    if company and company.default_income_account:
        group.append("item_group_defaults", {
            "company": company.name,
            "income_account": company.default_income_account,
        })

    group.insert(ignore_permissions=True)


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
    