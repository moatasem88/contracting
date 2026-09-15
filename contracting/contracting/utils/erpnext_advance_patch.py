"""FR-11: makes Subcontractor Contract a valid reconciliation source for
erpnext.accounts.utils.reconcile_against_document() - the same core engine
create_purchase_invoice() relies on (via CustomPurchaseInvoice.on_submit()'s
unconditional update_against_document_in_jv() call) to move an advance's
allocation from a Payment Entry's Subcontractor Contract reference row onto
a newly generated Purchase Invoice.

check_if_advance_entry_modified() hardcodes its SQL allowlist to
('', 'Sales Order', 'Purchase Order') - Subcontractor Contract isn't in it,
so reconcile_against_document() throws "Payment Entry has been modified
after you pulled it" on every attempt otherwise.

This is a verbatim copy of erpnext.accounts.utils.check_if_advance_entry_modified
(apps/erpnext/erpnext/accounts/utils.py:505-561, erpnext commit 7648d8db80,
2024-08-28, "Bumped to Version 14.73.0") with 'Subcontractor Contract' added
to both hardcoded allowlists. apps/erpnext is a live checkout tracking
github.com/frappe/erpnext.git upstream, and this bench never edits vendored
app code directly (every other core-behavior change here goes through a
Frappe hook - override_doctype_class, override_whitelisted_methods,
standard_queries); check_if_advance_entry_modified has no such hook point,
so this is applied as a plain monkeypatch instead. If a future `bench
update` changes the upstream function, diff it against the copy below and
update both.
"""

import erpnext
import frappe
from frappe import _, qb, throw
from frappe.query_builder.functions import Round


def check_if_advance_entry_modified(args):
	"""
	check if there is already a voucher reference
	check if amount is same
	check if jv is submitted
	"""
	if not args.get("unreconciled_amount"):
		args.update({"unreconciled_amount": args.get("unadjusted_amount")})

	ret = None
	if args.voucher_type == "Journal Entry":
		ret = frappe.db.sql(
			"""
			select t2.{dr_or_cr} from `tabJournal Entry` t1, `tabJournal Entry Account` t2
			where t1.name = t2.parent and t2.account = %(account)s
			and t2.party_type = %(party_type)s and t2.party = %(party)s
			and (t2.reference_type is null or t2.reference_type in ('', 'Sales Order', 'Purchase Order', 'Subcontractor Contract'))
			and t1.name = %(voucher_no)s and t2.name = %(voucher_detail_no)s
			and t1.docstatus=1 """.format(dr_or_cr=args.get("dr_or_cr")),
			args,
		)
	else:
		party_account_field = (
			"paid_from" if erpnext.get_party_account_type(args.party_type) == "Receivable" else "paid_to"
		)
		precision = frappe.get_precision("Payment Entry", "unallocated_amount")
		if args.voucher_detail_no:
			ret = frappe.db.sql(
				"""select t1.name
				from `tabPayment Entry` t1, `tabPayment Entry Reference` t2
				where
					t1.name = t2.parent and t1.docstatus = 1
					and t1.name = %(voucher_no)s and t2.name = %(voucher_detail_no)s
					and t1.party_type = %(party_type)s and t1.party = %(party)s and t1.{} = %(account)s
					and t2.reference_doctype in ('', 'Sales Order', 'Purchase Order', 'Subcontractor Contract')
					and t2.allocated_amount = %(unreconciled_amount)s
			""".format(party_account_field),
				args,
			)
		else:
			pe = qb.DocType("Payment Entry")
			ret = (
				qb.from_(pe)
				.select(pe.name)
				.where(
					(pe.name == args.voucher_no)
					& (pe.docstatus == 1)
					& (pe.party_type == args.party_type)
					& (pe.party == args.party)
					& (pe[party_account_field] == args.account)
					& (Round(pe.unallocated_amount, precision) == Round(args.unreconciled_amount, precision))
				)
				.run()
			)

	if not ret:
		throw(_("""Payment Entry has been modified after you pulled it. Please pull it again."""))


def apply():
	import erpnext.accounts.utils as erpnext_accounts_utils

	erpnext_accounts_utils.check_if_advance_entry_modified = check_if_advance_entry_modified
