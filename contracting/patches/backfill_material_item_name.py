import frappe


def execute():
	"""Backfill Tender Material Item.item_name from the linked Item.

	The field is new (fetch_from item.item_name) - fetch_from only
	populates on save, so existing rows were never given a chance to
	pick it up and need a one-off copy.
	"""
	frappe.db.sql(
		"""
		update `tabTender Material Item` tmi
		inner join `tabItem` i on i.name = tmi.item
		set tmi.item_name = i.item_name
		where ifnull(tmi.item_name, '') = ''
		"""
	)
	frappe.db.commit()
