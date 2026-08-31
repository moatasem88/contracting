import erpnext
import frappe

RESOURCE_TABLES = ("Tender Material Item", "Tender Labor Item", "Tender Equipment Item")


def execute():
	"""Backfill currency/exchange_rate on every existing Material/Labor/
	Equipment row, so historical data has a valid currency the moment
	Tender.validate() starts requiring one to resolve an exchange rate
	(see resolve_exchange_rate in tender.py).

	Writes through frappe.db.sql rather than loading/saving each Tender, for
	the same reason backfill_header_totals does: re-saving live Tenders here
	could fire Tender.on_update()'s won-automation side effects.
	"""
	base_currency = erpnext.get_company_currency(erpnext.get_default_company())

	for doctype in RESOURCE_TABLES:
		frappe.db.sql(
			"""
			update `tab{0}`
			set currency = %s, exchange_rate = 1
			where currency is null or currency = ''
			""".format(doctype),
			(base_currency,),
		)

	frappe.db.commit()
	print("backfill_resource_item_currency: defaulted currency to {0} across {1} tables".format(
		base_currency, len(RESOURCE_TABLES)
	))
