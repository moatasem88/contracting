import frappe

def calculate_totals(doc, method):
    if not doc.from_tender:
        return 
    
    total_quantity = 0
    total_amount = 0

    for item in doc.tender_items:
        qty = float(item.qty or 0)
        rate = float(item.rate or 0)

        item_amount = qty * rate
        item.amount = item_amount

        total_quantity += qty
        total_amount += item_amount

    doc.total_qty = total_quantity
    doc.total = total_amount

    total_taxes_and_charges = 0
    for tax in doc.taxes:
        tax_amount = 0
        tax_amount = (total_amount * tax.rate) / 100
        total_taxes_and_charges += tax_amount

    doc.total_taxes_and_charges = total_taxes_and_charges

    doc.grand_total = doc.total + doc.total_taxes_and_charges

    doc.rounding_adjustment = round(doc.grand_total, 2) - doc.grand_total
    doc.rounded_total = round(doc.grand_total, 2)

