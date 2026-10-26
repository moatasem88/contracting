"""Tender staff may create and edit resource Items - Labor, Equipment and
Subcontractors Items - but never materials (2026-10-18).

Role permissions can't scope Item create/write by Item Group, so the Tender
roles get plain Item read/create/write (patches/grant_tender_item_creation.py)
and this narrows it: validate() rejects anything outside the three resource
groups and keeps those items non-stock (the Tender's Material tab is "any
stock item", so a stock Labor item would also pass as a Material), and
has_permission() makes every other Item read-only for them.

Anyone who also holds a role that already manages Items is left alone.
"""

import frappe
from frappe import _

TENDER_ITEM_ROLES = frozenset((
	"Tender Manager",
	"Tender Engineer Civil",
	"Tender Engineer Electrical",
	"Tender Engineer Mechanical",
))
UNRESTRICTED_ITEM_ROLES = frozenset(("Item Manager", "stockdepartment", "System Manager"))
RESOURCE_ITEM_GROUPS = ("Labor", "Equipment", "Subcontractors Items")


def is_tender_item_user(user=None):
	"""True when the user's only route to creating Items is a Tender role."""
	user = user or frappe.session.user
	if user == "Administrator":
		return False
	roles = set(frappe.get_roles(user))
	return bool(roles & TENDER_ITEM_ROLES) and not roles & UNRESTRICTED_ITEM_ROLES


def validate(doc, method=None):
	"""doc_events Item.validate."""
	# A Work Item Template V2 keeps its own Item (Work Item group) in step;
	# Tender staff may create templates, so that Item is theirs to write.
	if not is_tender_item_user() or doc.flags.from_work_item_template:
		return

	allowed = ", ".join(RESOURCE_ITEM_GROUPS)
	if doc.item_group not in RESOURCE_ITEM_GROUPS:
		frappe.throw(
			_("Tender staff can only create or edit items in the {0} Item Groups. Ask the stock "
			  "department to create material items.").format(allowed),
			title=_("Item Group Not Allowed"),
		)

	before = None if doc.is_new() else doc.get_doc_before_save()
	if before and before.item_group not in RESOURCE_ITEM_GROUPS:
		frappe.throw(_("Tender staff can only edit items in the {0} Item Groups.").format(allowed))

	# Resource items are services, never stock (see module docstring).
	doc.is_stock_item = 0
	doc.is_fixed_asset = 0


def has_permission(doc, ptype=None, user=None):
	"""has_permission hook: an existing non-resource Item is read-only for
	Tender staff. None leaves every other case to the role permissions."""
	if ptype != "write" or doc.is_new() or not is_tender_item_user(user):
		return None
	group = frappe.db.get_value("Item", doc.name, "item_group")
	if group and group not in RESOURCE_ITEM_GROUPS:
		return False
	return None


@frappe.whitelist()
def get_item_group_scope():
	"""For public/js/item.js: the groups the Item Group picker should offer
	this user, or None for no restriction."""
	return list(RESOURCE_ITEM_GROUPS) if is_tender_item_user() else None
