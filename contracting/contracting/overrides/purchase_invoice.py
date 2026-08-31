import frappe
import erpnext
from erpnext.accounts.utils import get_account_currency, get_fiscal_year
from frappe.utils import cint, cstr, flt, formatdate, get_link_to_form, getdate, nowdate
from frappe import _, throw
from erpnext.accounts.doctype.gl_entry.gl_entry import update_outstanding_amt

from erpnext.accounts.general_ledger import (
	get_round_off_account_and_cost_center,
	make_gl_entries,
	make_reverse_gl_entries,
	merge_similar_entries,
)
from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
	check_if_return_invoice_linked_with_payment_entry,
	get_total_in_party_account_currency,
	is_overdue,
	unlink_inter_company_doc,
	update_linked_doc,
	validate_inter_company_party,
)

from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import PurchaseInvoice

class CustomPurchaseInvoice(PurchaseInvoice):
    def on_submit(self):
            super(PurchaseInvoice, self).on_submit()
            self.check_prev_docstatus()
            self.update_status_updater_args()
            self.update_prevdoc_status()

            frappe.get_doc("Authorization Control").validate_approving_authority(
                self.doctype, self.company, self.base_grand_total
            )

            if not self.is_return:
                self.update_against_document_in_jv()
                self.update_billing_status_for_zero_amount_refdoc("Purchase Receipt")
                self.update_billing_status_for_zero_amount_refdoc("Purchase Order")

            self.update_billing_status_in_pr()

            # Updating stock ledger should always be called after updating prevdoc status,
            # because updating ordered qty in bin depends upon updated ordered qty in PO
            if self.update_stock == 1:
                self.update_stock_ledger()

                if self.is_old_subcontracting_flow:
                    self.set_consumed_qty_in_subcontract_order()

                from erpnext.stock.doctype.serial_no.serial_no import update_serial_nos_after_submit

                update_serial_nos_after_submit(self, "items")

            # this sequence because outstanding may get -negative
            self.make_gl_entries()

            if self.update_stock == 1:
                self.repost_future_sle_and_gle()

            if (
                frappe.db.get_single_value("Buying Settings", "project_update_frequency") == "Each Transaction"
            ):
                self.update_project()

            update_linked_doc(self.doctype, self.name, self.inter_company_invoice_reference)
            self.update_advance_tax_references()

            self.process_common_party_accounting()


    def make_gl_entries(self, gl_entries=None, from_repost=False):
            if not gl_entries:
                gl_entries = self.get_gl_entries()

            if gl_entries:
                update_outstanding = "No" # if (cint(self.is_paid) or self.write_off_account) else "Yes"

                if self.docstatus == 1:
                    make_gl_entries(
                        gl_entries,
                        update_outstanding=update_outstanding,
                        merge_entries=False,
                        from_repost=from_repost,
                    )
                    self.make_exchange_gain_loss_journal()
                elif self.docstatus == 2:
                    provisional_entries = [a for a in gl_entries if a.voucher_type == "Purchase Receipt"]
                    make_reverse_gl_entries(voucher_type=self.doctype, voucher_no=self.name)
                    if provisional_entries:
                        for entry in provisional_entries:
                            frappe.db.set_value(
                                "GL Entry",
                                {"voucher_type": "Purchase Receipt", "voucher_detail_no": entry.voucher_detail_no},
                                "is_cancelled",
                                1,
                            )

                if update_outstanding == "No":
                    update_outstanding_amt(
                        self.credit_to,
                        "Supplier",
                        self.supplier,
                        self.doctype,
                        self.return_against if cint(self.is_return) and self.return_against else self.name,
                    )

            elif self.docstatus == 2 and cint(self.update_stock) and self.auto_accounting_for_stock:
                make_reverse_gl_entries(voucher_type=self.doctype, voucher_no=self.name)



    def get_gl_entries(self, warehouse_account=None):
            self.auto_accounting_for_stock = erpnext.is_perpetual_inventory_enabled(self.company)

            if self.auto_accounting_for_stock:
                self.stock_received_but_not_billed = self.get_company_default("stock_received_but_not_billed")
            else:
                self.stock_received_but_not_billed = None

            self.negative_expense_to_be_booked = 0.0
            gl_entries = []

            self.make_supplier_gl_entry(gl_entries)
            self.make_item_gl_entries(gl_entries)
            self.make_precision_loss_gl_entry(gl_entries)

            self.make_tax_gl_entries(gl_entries)
            self.make_internal_transfer_gl_entries(gl_entries)

            gl_entries = make_regional_gl_entries(gl_entries, self)

            gl_entries = merge_similar_entries(gl_entries)

            self.make_payment_gl_entries(gl_entries)
            self.make_write_off_gl_entry(gl_entries)
            self.make_gle_for_rounding_adjustment(gl_entries)
            return gl_entries
	



    def make_tax_gl_entries(self, gl_entries):
            # tax table gl entries
            valuation_tax = {}

            for tax in self.get("taxes"):
                amount, base_amount = self.get_tax_amounts(tax, None)
                if tax.category in ("Total", "Valuation and Total") and flt(base_amount):
                    account_currency = get_account_currency(tax.account_head)

                    dr_or_cr = "debit" if tax.add_deduct_tax == "Add" else "credit"

                    account_type = frappe.get_value(
                                        "Account", tax.account_head, "account_type"
                                    )
                    
                    if account_type in ["Payable", "Receivable"]:
                        gl_entries.append(
                        self.get_gl_dict(
                            {
                                "account": tax.account_head,
                                "party_type": tax.party_type,
                                "party": tax.party,
                                "against": self.supplier,
                                dr_or_cr: base_amount,
                                dr_or_cr + "_in_account_currency": base_amount
                                if account_currency == self.company_currency
                                else amount,
                                "cost_center": tax.cost_center,
                            },
                            account_currency,
                            item=tax,
                        )
                    )
                    else:

                        gl_entries.append(
                            self.get_gl_dict(
                                {
                                    "account": tax.account_head,
                                    "against": self.supplier,
                                    dr_or_cr: base_amount,
                                    dr_or_cr + "_in_account_currency": base_amount
                                    if account_currency == self.company_currency
                                    else amount,
                                    "cost_center": tax.cost_center,
                                },
                                account_currency,
                                item=tax,
                            )
                        )
                # accumulate valuation tax
                if (
                    self.is_opening == "No"
                    and tax.category in ("Valuation", "Valuation and Total")
                    and flt(base_amount)
                    and not self.is_internal_transfer()
                ):
                    if self.auto_accounting_for_stock and not tax.cost_center:
                        frappe.throw(
                            _("Cost Center is required in row {0} in Taxes table for type {1}").format(
                                tax.idx, _(tax.category)
                            )
                        )
                    valuation_tax.setdefault(tax.name, 0)
                    valuation_tax[tax.name] += (tax.add_deduct_tax == "Add" and 1 or -1) * flt(base_amount)

            if self.is_opening == "No" and self.negative_expense_to_be_booked and valuation_tax:
                # credit valuation tax amount in "Expenses Included In Valuation"
                # this will balance out valuation amount included in cost of goods sold

                total_valuation_amount = sum(valuation_tax.values())
                amount_including_divisional_loss = self.negative_expense_to_be_booked
                i = 1
                for tax in self.get("taxes"):
                    if valuation_tax.get(tax.name):
                        if i == len(valuation_tax):
                            applicable_amount = amount_including_divisional_loss
                        else:
                            applicable_amount = self.negative_expense_to_be_booked * (
                                valuation_tax[tax.name] / total_valuation_amount
                            )
                            amount_including_divisional_loss -= applicable_amount
                        
                        account_type = frappe.get_value(
                                        "Account", tax.account_head, "account_type"
                                    )
                    
                        if account_type in ["Payable", "Receivable"]:
                            gl_entries.append(
                                self.get_gl_dict(
                                    {
                                        "account": tax.account_head,
                                        "party_type": tax.party_type,
                                        "party": tax.party,
                                        "cost_center": tax.cost_center,
                                        "against": self.supplier,
                                        "credit": applicable_amount,
                                        "remarks": self.remarks or _("Accounting Entry for Stock"),
                                    },
                                    item=tax,
                                )
                            )
                        else:

                            gl_entries.append(
                                self.get_gl_dict(
                                    {
                                        "account": tax.account_head,
                                        "cost_center": tax.cost_center,
                                        "against": self.supplier,
                                        "credit": applicable_amount,
                                        "remarks": self.remarks or _("Accounting Entry for Stock"),
                                    },
                                    item=tax,
                                )
                            )

                        i += 1

            if self.auto_accounting_for_stock and self.update_stock and valuation_tax:
                for tax in self.get("taxes"):
                    if valuation_tax.get(tax.name):
                        account_type = frappe.get_value(
                                        "Account", tax.account_head, "account_type"
                                    )
                    
                        if account_type in ["Payable", "Receivable"]:
                            gl_entries.append(
                            self.get_gl_dict(
                                {
                                    "account": tax.account_head,
                                    "party_type": tax.party_type,
                                    "party": tax.party,
                                    "cost_center": tax.cost_center,
                                    "against": self.supplier,
                                    "credit": valuation_tax[tax.name],
                                    "remarks": self.remarks or _("Accounting Entry for Stock"),
                                },
                                item=tax,
                            )
                        )
                            
                        else:
                            gl_entries.append(
                                self.get_gl_dict(
                                    {
                                        "account": tax.account_head,
                                        "cost_center": tax.cost_center,
                                        "against": self.supplier,
                                        "credit": valuation_tax[tax.name],
                                        "remarks": self.remarks or _("Accounting Entry for Stock"),
                                    },
                                    item=tax,
                                )
                            )

@erpnext.allow_regional
def make_regional_gl_entries(gl_entries, doc):
	return gl_entries