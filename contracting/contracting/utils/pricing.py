import frappe


def calculate_effective_price(node):
    base = node.unit_price or 0.0
    additions_pct = 0.0
    fixed = 0.0
    for a in node.get("additions", []):
        if a.calculation_type == "Percentage":
            additions_pct += (a.value or 0)
        elif a.calculation_type == "Fixed Amount":
            fixed += (a.value or 0)
    return base * (1 + additions_pct / 100.0) + fixed


def get_addition_value(node, addition_type):
    for a in node.get("additions", []):
        if addition_type.lower() in (a.addition_name or "").lower():
            return a.value or 0
    return 0
