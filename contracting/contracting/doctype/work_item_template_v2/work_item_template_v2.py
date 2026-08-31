import frappe
from frappe.model.document import Document
# The frappe.rename_doc wrapper doesn't expose ignore_permissions; editing a
# template shouldn't require Item write permission, same as the create path.
from frappe.model.rename_doc import rename_doc

from contracting.contracting.api import WORK_ITEM_GROUP, create_work_item_group
from contracting.contracting.utils.pricing import calculate_effective_price

# Work items share the Item namespace with ~7300 material/equipment
# items, so the code is prefixed rather than being the bare template
# name - it both avoids collisions and makes work items obvious in the
# Item list and on a Sales Order.
ITEM_CODE_PREFIX = "WI-"


def work_item_code(template_name):
	return ITEM_CODE_PREFIX + template_name


class WorkItemTemplateV2(Document):
	def validate(self):
		self.validate_service_items()
		self.calculate_totals()

	def on_update(self):
		# on_update fires on insert as well as update (run_post_save_methods),
		# so there is no separate after_insert - same idiom as Tender's
		# handle_won_automation.
		self.sync_work_item()

	def after_rename(self, old_name, new_name, merge=False):
		# Renaming does not call on_update, and autoname is field:template_name,
		# so without this the Item would keep the old code and silently drift
		# from the template it represents.
		self.sync_work_item()

	def validate_service_items(self):
		# is_stock_item doesn't reliably distinguish service items from
		# physical goods in this bench's data - Item Group is the real
		# signal (dedicated "Labor" / "Equipment" groups). Enforced here
		# too, not just via the client-side item picker filter, so Data
		# Import/API entry can't slip a mismatched item into a line.
		required_group = {"labor_items": "Labor", "equipment_items": "Equipment"}
		for fieldname, group in required_group.items():
			for row in self.get(fieldname):
				item_group = frappe.db.get_value("Item", row.item, "item_group")
				if item_group != group:
					frappe.throw(
						frappe._("Row #{0}: Item {1} is in Item Group '{2}', not '{3}' - "
							"{3} lines must use an item from the '{3}' Item Group.").format(
							row.idx, row.item, item_group, group
						)
					)

	def calculate_totals(self):
		base_cost = 0.0
		for table in (self.material_items, self.labor_items, self.equipment_items):
			for row in table:
				row.amount = (row.qty_per_unit or 0) * (row.rate or 0)
				base_cost += row.amount

		self.total_cost_per_unit = calculate_effective_price(
			frappe._dict({"unit_price": base_cost, "additions": self.additions})
		)

	def sync_work_item(self):
		"""Keep an Item master record in step with this template. That Item
		is what a Tender BOQ row links to and what ends up on the Sales
		Order - a work item is a composite of Material/Labor/Equipment, so
		before this it had no item_code of its own and could never be a
		real Sales Order line."""
		create_work_item_group()

		target_code = work_item_code(self.template_name)

		if self.item and self.item != target_code:
			self.rename_work_item(target_code)
			return

		if not self.item:
			existing = self.adoptable_item(target_code)
			if existing:
				self.db_set("item", existing, update_modified=False)
			else:
				self.create_work_item(target_code)
				return

		self.update_work_item()

	def adoptable_item(self, target_code):
		"""Reclaim an Item we previously created but lost the link to (a
		restored backup, a template deleted and re-added). Anything else
		under that code belongs to someone else - adopting it would put an
		unrelated item on a customer's Sales Order, so refuse instead."""
		if not frappe.db.exists("Item", target_code):
			return None

		item_group = frappe.db.get_value("Item", target_code, "item_group")
		claimed_by = frappe.db.get_value(
			"Work Item Template V2", {"item": target_code, "name": ("!=", self.name)}, "name"
		)
		if item_group == WORK_ITEM_GROUP and not claimed_by:
			return target_code

		frappe.throw(
			frappe._(
				"Item {0} already exists{1} and is not available for this template - "
				"rename the template to something else."
			).format(
				target_code,
				frappe._(" and belongs to template {0}").format(claimed_by) if claimed_by else "",
			)
		)

	def create_work_item(self, target_code):
		item = frappe.get_doc({
			"doctype": "Item",
			"item_code": target_code,
			"item_name": self.template_name,
			"item_group": WORK_ITEM_GROUP,
			"stock_uom": self.uom,
			"is_stock_item": 0,
			"description": self.description or self.template_name,
		}).insert(ignore_permissions=True)
		self.db_set("item", item.name, update_modified=False)

	def update_work_item(self):
		item = frappe.get_doc("Item", self.item)
		changes = {
			"item_name": self.template_name,
			"stock_uom": self.uom,
			"description": self.description or self.template_name,
		}
		# Item.on_update fans out to variants, item prices and website
		# items - don't trigger all of that when nothing actually moved.
		if all(item.get(field) == value for field, value in changes.items()):
			return

		item.update(changes)
		item.save(ignore_permissions=True)

	def rename_work_item(self, target_code):
		if frappe.db.exists("Item", target_code):
			frappe.throw(
				frappe._(
					"Cannot rename this template: Item {0} already exists. Its Item would have "
					"to be renamed to that code."
				).format(target_code)
			)

		rename_doc("Item", self.item, target_code, force=True, ignore_permissions=True)
		self.db_set("item", target_code, update_modified=False)
		self.update_work_item()
