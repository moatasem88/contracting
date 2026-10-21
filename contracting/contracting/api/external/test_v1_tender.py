import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.api import create_work_item_group
from contracting.contracting.api.external import v1
from contracting.contracting.api.external.v1_test_utils import as_user, make_user
from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import make_v2_template
from contracting.contracting.doctype.tender import tender as tender_controller
from contracting.contracting.doctype.tender.test_tender import ensure_customer, make_project_tender, make_tender


class TestV1Tender(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		create_work_item_group()
		ensure_customer()
		cls.civil = make_user("Tender Engineer Civil")
		cls.electrical = make_user("Tender Engineer Electrical")
		cls.mechanical = make_user("Tender Engineer Mechanical")
		cls.manager = make_user("Tender Manager")
		cls.cce = make_user("Cost Control Engineer")
		cls.template = make_v2_template("_v1 Tender Template").name

	# -- FR-15 create ----------------------------------------------------------------

	def test_tc14_tender_engineers_create(self):
		for user in (self.civil, self.electrical, self.mechanical):
			pt = make_project_tender("_v1 create " + user)
			with as_user(user):
				options = v1.list_project_tender_options(search=pt.name)
				self.assertEqual([o.name for o in options], [pt.name])
				response = v1.create_tender(pt.name)
			self.assertTrue(response["data"]["name"].startswith("TND-"))
			self.assertEqual(response["data"]["owner"], user)
			self.assertEqual(response["data"]["project_tender"], pt.name)
			self.assertEqual(response["data"]["project_name"], pt.project_name)

	def test_tc15_no_create_permission(self):
		pt = make_project_tender("_v1 no create")
		for user in (self.manager, self.cce):
			with as_user(user):
				self.assertRaises(frappe.PermissionError, v1.create_tender, pt.name)
				self.assertRaises(frappe.PermissionError, v1.list_project_tender_options)
		self.assertFalse(frappe.db.exists("Tender", {"project_tender": pt.name}))

	def test_create_rejects_unknown_fields(self):
		pt = make_project_tender("_v1 create bad")
		self.assertRaises(TypeError, v1.create_tender, pt.name, status="Complete")

	# -- FR-16 add from template -----------------------------------------------------

	def test_tc16_add_from_template_as_tender_engineer(self):
		tender = make_tender("_v1 add tpl")
		with as_user(self.civil):
			templates = v1.list_work_item_templates(search="_v1 Tender Template")["data"]
			self.assertEqual([t.name for t in templates], [self.template])
			response = v1.add_boq_item_from_template(tender.name, self.template, 120)
		boq = response["data"]["boq_items"]
		self.assertEqual(len(boq), 2)
		self.assertEqual(boq[-1]["work_item_template"], self.template)
		self.assertEqual(flt(boq[-1]["original_quantity"]), 120)
		labor = [r for r in response["data"]["labor_items"] if r["work_item"] == boq[-1]["idx"]]
		self.assertEqual([flt(r["qty"]) for r in labor], [120])

		# Same as desk's own call with the same inputs.
		desk = make_tender("_v1 add tpl desk")
		tender_controller.add_boq_item_from_template(desk.name, self.template, 120)
		desk.reload()
		self.assertAlmostEqual(flt(desk.total_direct_cost), flt(response["data"]["total_direct_cost"]))

	def test_add_from_template_needs_tender_write(self):
		tender = make_tender("_v1 add tpl ro")
		with as_user(self.cce):
			self.assertRaises(frappe.PermissionError, v1.add_boq_item_from_template, tender.name, self.template, 1)

	def test_add_from_template_checks_modified_only_when_sent(self):
		tender = make_tender("_v1 add tpl mod")
		self.assertRaises(frappe.TimestampMismatchError, v1.add_boq_item_from_template, tender.name, self.template,
						  1, "2001-01-01 00:00:00")
		v1.add_boq_item_from_template(tender.name, self.template, 1)

	# -- FR-17 quantities ------------------------------------------------------------

	def test_tc17_quantity_update_rescales(self):
		tender = make_tender("_v1 qty")
		v1.add_boq_item_from_template(tender.name, self.template, 100)
		tender.reload()
		row = tender.boq_items[-1]
		response = v1.update_tender_boq_quantities(tender.name, str(tender.modified),
												   [{"row_name": row.name, "original_quantity": 150}])
		labor = [r for r in response["data"]["labor_items"] if r["work_item"] == row.idx]
		self.assertEqual([flt(r["qty"]) for r in labor], [150])

	def test_tc18_non_positive_quantity_rejected(self):
		tender = make_tender("_v1 qty0")
		row = tender.boq_items[0]
		for qty in (0, -5):
			self.assertRaises(frappe.ValidationError, v1.update_tender_boq_quantities, tender.name,
							  str(tender.modified), [{"row_name": row.name, "original_quantity": qty}])
		self.assertEqual(str(frappe.db.get_value("Tender", tender.name, "modified")), str(tender.modified))

	def test_only_quantity_is_writable(self):
		tender = make_tender("_v1 qty field")
		self.assertRaises(frappe.ValidationError, v1.update_tender_boq_quantities, tender.name, str(tender.modified),
						  [{"row_name": tender.boq_items[0].name, "original_quantity": 2, "rate": 1}])

	def test_tc20_stale_write(self):
		tender = make_tender("_v1 stale")
		row = tender.boq_items[0]
		stale = str(tender.modified)
		v1.update_tender_boq_quantities(tender.name, stale, [{"row_name": row.name, "original_quantity": 11}])
		self.assertRaises(frappe.TimestampMismatchError, v1.update_tender_boq_quantities, tender.name, stale,
						  [{"row_name": row.name, "original_quantity": 12}])
		self.assertEqual(flt(frappe.db.get_value("Tender BOQ Item", row.name, "original_quantity")), 11)

	# -- FR-18/19 complete + revise --------------------------------------------------

	def test_tc19_mark_complete_then_revise(self):
		tender = make_tender("_v1 complete")
		tender.db_set("status", "In Progress")
		with as_user(self.civil):
			view = v1.get_tender_view(tender.name)
			self.assertFalse(view["meta"]["can"]["mark_complete"])
			self.assertRaises(frappe.PermissionError, v1.mark_tender_complete, tender.name, view["meta"]["modified"])
		with as_user(self.manager):
			view = v1.get_tender_view(tender.name)
			self.assertTrue(view["meta"]["can"]["mark_complete"])
			done = v1.mark_tender_complete(tender.name, view["meta"]["modified"])
			self.assertEqual(done["data"]["status"], "Complete")
			self.assertTrue(done["meta"]["can"]["revise"])
			revised = v1.create_revised_version(tender.name, done["meta"]["modified"])
		self.assertNotEqual(revised["data"]["name"], tender.name)
		self.assertEqual(revised["data"]["previous_tender"], tender.name)
		self.assertEqual(revised["data"]["status"], "Draft")

	def test_revise_needs_write_permission(self):
		"""Step 0.1: create_revised_version used to let any logged-in user
		clone any Complete Tender (ignore_permissions, no check)."""
		tender = make_tender("_v1 revise ro")
		tender.db_set("status", "Complete")
		with as_user(self.cce):
			self.assertRaises(frappe.PermissionError, tender_controller.create_revised_version, tender.name)
			self.assertRaises(frappe.PermissionError, v1.create_revised_version, tender.name, str(tender.modified))
		with as_user(make_user("Contracts Manager")):
			self.assertRaises(frappe.PermissionError, tender_controller.create_revised_version, tender.name)

	def test_list_tenders_filtered_by_project_tender(self):
		tender = make_tender("_v1 list")
		with as_user(self.cce):
			rows = v1.list_tenders(project_tender=tender.project_tender)["data"]
		self.assertEqual([r.name for r in rows], [tender.name])

	# -- BOQ source file -------------------------------------------------------------

	def test_attach_tender_file(self):
		import base64

		tender = make_tender("_v1 attach")
		content = base64.b64encode(b"item,qty\n1,10\n").decode()
		with as_user(self.civil):
			v1.attach_tender_file(tender.name, str(tender.modified), "client-boq.csv", content)
		files = frappe.get_all("File", filters={"attached_to_doctype": "Tender", "attached_to_name": tender.name},
							   fields=["file_name", "is_private"])
		# Frappe adds a suffix when a file of that name is already on disk (reruns).
		self.assertEqual(len(files), 1)
		self.assertTrue(files[0].file_name.startswith("client-boq") and files[0].file_name.endswith(".csv"))
		self.assertEqual(files[0].is_private, 1)

	def test_attach_tender_file_needs_write(self):
		import base64

		tender = make_tender("_v1 attach ro")
		with as_user(self.cce):  # read-only on Tender
			self.assertRaises(frappe.PermissionError, v1.attach_tender_file, tender.name, str(tender.modified),
							  "x.csv", base64.b64encode(b"x").decode())

	# -- BOQ intake: reviewed client BOQ -> Tender lines ------------------------------

	def _boq_lines(self):
		groups = [{"key": "1", "title": "_v1 أعمال ترابية"}, {"key": "2", "title": "_v1 أعمال الخرسانات"}]
		lines = [
			{"boq_item_no": "1.1", "template": self.template, "quantity": 10, "description": "حفر", "client_rate": 365,
			 "group": "1"},
			{"boq_item_no": "1.2", "template": self.template, "quantity": 5, "description": "ردم", "client_rate": 771,
			 "group": "1"},
			{"boq_item_no": "2.1", "template": self.template, "quantity": 2, "client_rate": 7378, "group": "2"},
		]
		return groups, lines

	def test_add_boq_lines_groups_and_pins_client_rate(self):
		tender = make_tender("_v1 boq lines")
		groups, lines = self._boq_lines()
		with as_user(self.civil):
			response = v1.add_boq_lines(tender.name, lines, groups)
		self.assertEqual(sorted(response["added"]), ["1.1", "1.2", "2.1"])
		boq = response["view"]["data"]["boq_items"]
		layout = [(r["is_group"], r["indent"], r.get("boq_item_no") or r["item_name"]) for r in boq[1:]]
		self.assertEqual(layout, [(1, 0, "_v1 أعمال ترابية"), (0, 1, "1.1"), (0, 1, "1.2"),
								  (1, 0, "_v1 أعمال الخرسانات"), (0, 1, "2.1")])
		row = next(r for r in boq if r.get("boq_item_no") == "1.1")
		self.assertEqual(row["description"], "حفر")
		self.assertEqual(flt(row["sell_rate"]), 365)
		cost = flt(row["effective_unit_price"])
		self.assertTrue(cost > 0)
		self.assertAlmostEqual(flt(row["margin_percent"]), (365 / cost - 1) * 100, places=4)
		# Each line's resources point at its own row.
		labor = [r for r in response["view"]["data"]["labor_items"] if r["work_item"] == row["idx"]]
		self.assertEqual([flt(r["qty"]) for r in labor], [10])

	def test_add_boq_lines_retry_skips_and_joins_existing_group(self):
		tender = make_tender("_v1 boq retry")
		groups, lines = self._boq_lines()
		v1.add_boq_lines(tender.name, lines[:1], groups)
		response = v1.add_boq_lines(tender.name, lines, groups)
		self.assertEqual(response["skipped"], ["1.1"])
		self.assertEqual(sorted(response["added"]), ["1.2", "2.1"])
		boq = response["view"]["data"]["boq_items"]
		numbers = [r.get("boq_item_no") or r["item_name"] for r in boq[1:]]
		# 1.2 lands under the existing earthworks group, not after the new one.
		self.assertEqual(numbers, ["_v1 أعمال ترابية", "1.1", "1.2", "_v1 أعمال الخرسانات", "2.1"])
		moved = next(r for r in boq if r.get("boq_item_no") == "1.2")
		labor = [r for r in response["view"]["data"]["labor_items"] if r["work_item"] == moved["idx"]]
		self.assertEqual([flt(r["qty"]) for r in labor], [5])
		again = v1.add_boq_lines(tender.name, lines, groups)
		self.assertEqual((again["added"], again["skipped"]), ({}, ["1.1", "1.2", "2.1"]))

	def test_add_boq_lines_checks_everything_first(self):
		tender = make_tender("_v1 boq bad")
		groups, lines = self._boq_lines()
		bad = lines + [{"boq_item_no": "2.2", "template": "_no such template", "quantity": 1, "group": "2"}]
		self.assertRaises(frappe.DoesNotExistError, v1.add_boq_lines, tender.name, bad, groups)
		self.assertRaises(frappe.ValidationError, v1.add_boq_lines, tender.name, lines + lines[:1], groups)
		self.assertEqual(len(frappe.get_doc("Tender", tender.name).boq_items), 1)

	def test_add_boq_lines_needs_write_and_open_tender(self):
		tender = make_tender("_v1 boq perm")
		groups, lines = self._boq_lines()
		with as_user(self.cce):
			self.assertRaises(frappe.PermissionError, v1.add_boq_lines, tender.name, lines, groups)
		frappe.db.set_value("Tender", tender.name, "status", "Complete")
		self.assertRaises(frappe.ValidationError, v1.add_boq_lines, tender.name, lines, groups)

	def test_create_work_item_template_draft(self):
		with as_user(self.civil):
			first = v1.create_work_item_template_draft("_v1 draft انترلوك سمك 6 سم", "Nos", "أعمال موقع عام", "وصف")
			second = v1.create_work_item_template_draft("_v1 draft انترلوك سمك 6 سم", "Nos")
		self.assertEqual(first["name"], "_v1 draft انترلوك سمك 6 سم")
		self.assertEqual(second["name"], "_v1 draft انترلوك سمك 6 سم (2)")
		doc = frappe.get_doc("Work Item Template V2", first["name"])
		self.assertEqual((doc.uom, doc.category, len(doc.labor_items)), ("Nos", "أعمال موقع عام", 0))
		self.assertRaises(frappe.ValidationError, v1.create_work_item_template_draft, "_v1 draft x", "_no unit")
		with as_user(self.cce):
			self.assertRaises(frappe.PermissionError, v1.create_work_item_template_draft, "_v1 draft y", "Nos")
		# The template's own Work Item is allowed; a material Item still is not.
		self.assertEqual(frappe.db.get_value("Item", doc.item, "item_group"), "Work Item")
		group = frappe.db.get_value("Item Group", {"is_group": 0, "name": ("not in", (
			"Labor", "Equipment", "Subcontractors Items", "Work Item"))}, "name")
		with as_user(self.civil):
			material = frappe.get_doc({"doctype": "Item", "item_code": "_v1 material x", "item_group": group,
									   "stock_uom": "Nos"})
			self.assertRaises(frappe.ValidationError, material.insert)

	def test_draft_template_line_sells_at_client_rate_with_no_cost(self):
		with as_user(self.civil):
			draft = v1.create_work_item_template_draft("_v1 draft zero cost", "Nos")["name"]
		tender = make_tender("_v1 boq zero")
		response = v1.add_boq_lines(tender.name, [
			{"boq_item_no": "9.1", "template": draft, "quantity": 4, "client_rate": 250}])
		row = next(r for r in response["view"]["data"]["boq_items"] if r.get("boq_item_no") == "9.1")
		self.assertEqual((flt(row["sell_rate"]), flt(row["sell_amount"]), flt(row["margin_percent"])), (250, 1000, 0))
