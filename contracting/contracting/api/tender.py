import frappe

from contracting.contracting.utils.pricing import calculate_effective_price, get_addition_value

@frappe.whitelist()
def get_template_flat_tree(template_name, parent_qty=1.0, indent=0, parent_row_name=None):
    parent_qty = float(parent_qty)
    indent = int(indent)
    node = frappe.get_doc("Work Item Template", template_name)
    qf = float(node.quantity_factor or 1.0)
    this_qty = qf * parent_qty if indent > 0 else parent_qty
    effective_price = calculate_effective_price(node)
    current_row = {
        "work_item_template": node.name,
        "item_name": node.template_name,
        "item_type": "Work Item" if node.is_group else (node.item_type or "Material"),
        "item_code": node.item_code,
        "uom": node.uom,
        "indent": indent,
        "is_group": node.is_group,
        "parent_row_name": parent_row_name,
        "quantity_factor": qf,
        "original_quantity": this_qty,
        "unit_price": node.unit_price or 0,
        "vat_percentage": get_addition_value(node, "VAT"),
        "other_additions_pct": get_addition_value(node, "Other"),
        "fixed_additions": get_addition_value(node, "Fixed"),
        "effective_unit_price": effective_price,
        "total_amount": effective_price * this_qty if not node.is_group else 0,
        "invoiced_quantity": 0,
        "remaining_quantity": this_qty,
        "completion_percentage": 0,
        "is_locked": 0,
    }
    result = [current_row]
    children = frappe.get_all(
        "Work Item Template",
        filters={"parent_work_item_template": template_name},
        fields=["name"],
        order_by="lft"
    )
    for child in children:
        child_rows = get_template_flat_tree(
            child.name, parent_qty=this_qty,
            indent=indent + 1, parent_row_name="__PENDING__"
        )
        result.extend(child_rows)
    return result
