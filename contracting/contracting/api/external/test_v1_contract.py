import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.api.external import v1
from contracting.contracting.api.external.v1_test_utils import as_user, keys_anywhere, make_user
from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	ensure_subcontractor,
	make_draft_contract,
	make_resource_tender_and_project,
	make_v2_template,
)

RATE_FIELDS = ("tender_unit_cost", "tender_rate")
SAI = "Supplying and installing"


def make_contract(suffix, qty=10, unit_price=100):
	project, tender = make_resource_tender_and_project(suffix, qty=qty)
	contract = make_draft_contract(project, SAI)
	contract.append("contracted_items", {"work_item": tender.boq_items[0].name, "qty": qty, "unit_price": unit_price})
	contract.insert(ignore_permissions=True)
	return project, tender, contract


class TestV1SubcontractorContract(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cm = make_user("Contracts Manager")
		cls.ce = make_user("Contracts Engineer")
		cls.ccm = make_user("cost control manager")
		cls.pd = make_user("Projects Director")
		ensure_subcontractor()

	# -- FR-05 rate hiding ----------------------------------------------------

	def test_tc05_contracts_roles_never_receive_tender_rates(self):
		_project, _tender, contract = make_contract("v1_rates")
		self.assertTrue(flt(contract.contracted_items[0].tender_unit_cost))  # there is something to hide
		for user in (self.cm, self.ce):
			with as_user(user):
				response = v1.get_subcontractor_contract_view(contract.name)
			self.assertEqual(keys_anywhere(response, RATE_FIELDS), [], user)
			self.assertTrue(response["data"]["contracted_items"])
			self.assertTrue(response["data"]["contract_labor_items"])

	def test_tc07_cost_control_sees_tender_rates(self):
		_project, _tender, contract = make_contract("v1_rates_cc")
		with as_user(self.ccm):
			response = v1.get_subcontractor_contract_view(contract.name)
		self.assertIn("tender_unit_cost", response["data"]["contracted_items"][0])
		self.assertIn("tender_rate", response["data"]["contract_labor_items"][0])

	def test_tc05b_contracts_manager_cannot_read_tender(self):
		_project, tender, _contract = make_contract("v1_tender_403")
		with as_user(self.cm):
			self.assertRaises(frappe.PermissionError, v1.get_tender_view, tender.name)

	def test_deprecated_table_never_returned(self):
		_project, _tender, contract = make_contract("v1_deprecated")
		response = v1.get_subcontractor_contract_view(contract.name)
		self.assertNotIn("items_subcontracted", response["data"])

	# -- FR-21 draft ---------------------------------------------------------------

	def test_tc21_draft_from_unallocated_lines_as_contracts_manager(self):
		project, tender = make_resource_tender_and_project("v1_draft", qty=10)
		with as_user(self.cm):
			options = v1.get_contract_draft_options(project.name, SAI)
			self.assertEqual([row["work_item"] for row in options["lines"]], [tender.boq_items[0].name])
			self.assertEqual(keys_anywhere(options, RATE_FIELDS + ("rate", "rate_egp")), [])
			response = v1.create_subcontractor_contract_draft(
				project.name, ensure_subcontractor(), SAI,
				[{"tender_boq_item": tender.boq_items[0].name, "qty": 4, "unit_price": 100}],
			)
		data = response["data"]
		self.assertEqual(response["meta"]["workflow_state"], "Draft")
		self.assertEqual(data["contracted_items"][0]["qty"], 4)
		self.assertEqual([r["qty"] for r in data["contract_labor_items"]], [4])  # propagated on the server
		self.assertEqual(keys_anywhere(response, RATE_FIELDS), [])
		self.assertFalse(frappe.get_all("Tender Item", filters={"parent": data["name"]}))

	def test_tc22_over_allocation_uses_controller_error(self):
		project, tender = make_resource_tender_and_project("v1_over", qty=10)
		before = frappe.db.count("Subcontractor Contract")
		self.assertRaises(
			frappe.ValidationError, v1.create_subcontractor_contract_draft,
			project.name, ensure_subcontractor(), SAI,
			[{"tender_boq_item": tender.boq_items[0].name, "qty": 15, "unit_price": 100}],
		)
		self.assertEqual(frappe.db.count("Subcontractor Contract"), before)

	def test_tc23_nothing_left_to_allocate(self):
		project, tender, contract = make_contract("v1_full", qty=10)
		contract.attached_contract = "/files/x.pdf"
		contract.submit()
		with self.assertRaisesRegex(frappe.ValidationError, "No unallocated quantity"):
			v1.create_subcontractor_contract_draft(
				project.name, ensure_subcontractor(), SAI,
				[{"tender_boq_item": tender.boq_items[0].name, "qty": 1, "unit_price": 100}],
			)

	def test_template_line_draft(self):
		template = make_v2_template("_v1 SC Template").name
		# A project with a Tender: follows_tender stays on, so a template line
		# is flagged as outside the Tender.
		project, _tender = make_resource_tender_and_project("v1_tpl", qty=10)
		response = v1.create_subcontractor_contract_draft(
			project.name, ensure_subcontractor(), SAI, [{"work_item_template": template, "qty": 2, "unit_price": 50}]
		)
		self.assertEqual([r["qty"] for r in response["data"]["contract_labor_items"]], [2])
		self.assertEqual(response["data"]["contracted_items"][0]["out_of_tender_scope"], 1)

	def test_line_needs_exactly_one_source(self):
		project, tender = make_resource_tender_and_project("v1_both", qty=10)
		self.assertRaises(
			frappe.ValidationError, v1.create_subcontractor_contract_draft, project.name, ensure_subcontractor(), SAI,
			[{"tender_boq_item": tender.boq_items[0].name, "work_item_template": "x", "qty": 1, "unit_price": 1}],
		)

	# -- FR-22 edit lines ------------------------------------------------------------

	def test_tc24_qty_correction_rescales_resources(self):
		_project, _tender, contract = make_contract("v1_rescale")
		response = v1.update_subcontractor_contract_lines(
			contract.name, str(contract.modified), rows=[{"row_name": contract.contracted_items[0].name, "qty": 8}]
		)
		self.assertEqual([r["qty"] for r in response["data"]["contract_labor_items"]], [8])
		self.assertEqual([r["qty"] for r in response["data"]["contract_material_items"]], [16])

	def test_tc25_no_edits_once_in_approval(self):
		_project, _tender, contract = make_contract("v1_pending")
		contract.db_set("workflow_state", "Pending Contracts Manager Approval")
		contract.reload()
		with self.assertRaisesRegex(frappe.ValidationError, "only be edited in Draft"):
			v1.update_subcontractor_contract_lines(
				contract.name, str(contract.modified), rows=[{"row_name": contract.contracted_items[0].name, "qty": 8}]
			)

	def test_tc20_stale_modified_is_refused(self):
		_project, _tender, contract = make_contract("v1_stale")
		stale = str(contract.modified)
		v1.update_subcontractor_contract_lines(contract.name, stale,
											   rows=[{"row_name": contract.contracted_items[0].name, "qty": 9}])
		self.assertRaises(
			frappe.TimestampMismatchError, v1.update_subcontractor_contract_lines, contract.name, stale,
			rows=[{"row_name": contract.contracted_items[0].name, "qty": 8}],
		)
		self.assertEqual(flt(frappe.get_doc("Subcontractor Contract", contract.name).contracted_items[0].qty), 9)

	def test_remove_line_cascades_resources(self):
		project, tender, contract = make_contract("v1_remove")
		template = make_v2_template("_v1 SC Template").name
		response = v1.update_subcontractor_contract_lines(
			contract.name, str(contract.modified),
			add=[{"work_item_template": template, "qty": 1, "unit_price": 10}],
			remove=[contract.contracted_items[0].name],
		)
		data = response["data"]
		self.assertEqual([r["work_item_template"] for r in data["contracted_items"]], [template])
		self.assertFalse([r for r in data["contract_labor_items"] if r.get("work_item")])

	def test_unknown_field_rejected(self):
		_project, _tender, contract = make_contract("v1_badfield")
		self.assertRaises(
			frappe.ValidationError, v1.update_subcontractor_contract_lines, contract.name, str(contract.modified),
			rows=[{"row_name": contract.contracted_items[0].name, "tender_unit_cost": 1}],
		)

	# -- FR-23 workflow --------------------------------------------------------------

	def test_tc26_27_workflow_actions(self):
		_project, _tender, contract = make_contract("v1_wf")
		with as_user(self.cm):
			view = v1.get_subcontractor_contract_view(contract.name)
			self.assertEqual([a["action"] for a in view["meta"]["allowed_actions"]], ["Submit for Approval"])
			preview = v1.preview_subcontractor_contract_action(contract.name, "Submit for Approval")
			self.assertEqual(preview["next_state"], "Pending Contracts Manager Approval")
			self.assertEqual(frappe.db.get_value("Subcontractor Contract", contract.name, "workflow_state"), "Draft")
			done = v1.apply_subcontractor_contract_action(contract.name, view["meta"]["modified"], "Submit for Approval")
			self.assertEqual(done["meta"]["workflow_state"], "Pending Contracts Manager Approval")
			self.assertEqual(keys_anywhere(done, RATE_FIELDS), [])
			# Contracts Manager approves its own stage...
			done = v1.apply_subcontractor_contract_action(contract.name, done["meta"]["modified"], "Approve")
			self.assertEqual(done["meta"]["workflow_state"], "Pending Projects Director Approval")
			# ...but not the Projects Director's.
			with self.assertRaisesRegex(frappe.PermissionError, "Action not allowed"):
				v1.apply_subcontractor_contract_action(contract.name, done["meta"]["modified"], "Approve")
		self.assertEqual(
			frappe.db.get_value("Subcontractor Contract", contract.name, "workflow_state"),
			"Pending Projects Director Approval",
		)

	def test_tc28_only_valid_conditional_approve_offered(self):
		_project, _tender, contract = make_contract("v1_ceo")
		contract.db_set("workflow_state", "Pending Planning and Cost Control Director Approval")
		user = make_user("Planning and Cost Control Director")
		with as_user(user):
			actions = v1.get_subcontractor_contract_view(contract.name)["meta"]["allowed_actions"]
		approve = [a for a in actions if a["action"] == "Approve"]
		self.assertEqual(len(approve), 1)
		expected = "Pending CEO Approval" if contract.requires_ceo_approval else "Approved"
		self.assertEqual(approve[0]["next_state"], expected)

	def test_out_of_tender_rows_in_cost_control_preview(self):
		template = make_v2_template("_v1 SC Template").name
		project, _tender = make_resource_tender_and_project("v1_oot", qty=10)
		contract = make_draft_contract(project, SAI)
		contract.append("contracted_items", {"work_item_template": template, "qty": 1, "unit_price": 10})
		contract.insert(ignore_permissions=True)
		contract.db_set("workflow_state", "Pending Cost Control Approval")
		with as_user(self.ccm):
			preview = v1.preview_subcontractor_contract_action(contract.name, "Approve")
		self.assertTrue(preview["out_of_tender_rows"])
		self.assertEqual(preview["out_of_tender_rows"][0]["tab"], "Contracted Items")

	# -- Components pricing ----------------------------------------------------------

	def test_switch_to_components_prices_line_from_resource_rates(self):
		_project, _tender, contract = make_contract("v1_components")
		resources = [
			{"table": "contract_labor_items", "row_name": contract.contract_labor_items[0].name, "rate": 5},
			{"table": "contract_equipment_items", "row_name": contract.contract_equipment_items[0].name, "rate": 3},
			{"table": "contract_material_items", "row_name": contract.contract_material_items[0].name, "rate": 2},
		]
		with as_user(self.cm):
			response = v1.update_subcontractor_contract_lines(
				contract.name, str(contract.modified),
				rows=[{"row_name": contract.contracted_items[0].name, "pricing_mode": "Components"}],
				resources=resources,
			)
		line = response["data"]["contracted_items"][0]
		self.assertEqual(line["pricing_mode"], "Components")
		# labor 10 x 5 + equipment 10 x 3 + material 20 x 2 = 120 over qty 10
		self.assertAlmostEqual(flt(line["unit_price"]), 12)

	def test_resource_rate_rows_must_belong_to_the_contract(self):
		_project, _tender, contract = make_contract("v1_res_own")
		_project, _tender, other = make_contract("v1_res_other")
		stray = [{"table": "contract_labor_items", "row_name": other.contract_labor_items[0].name, "rate": 5}]
		self.assertRaises(frappe.ValidationError, v1.update_subcontractor_contract_lines,
						  contract.name, str(contract.modified), resources=stray)
		bad_table = [{"table": "contracted_items", "row_name": contract.contracted_items[0].name, "rate": 5}]
		self.assertRaises(frappe.ValidationError, v1.update_subcontractor_contract_lines,
						  contract.name, str(contract.modified), resources=bad_table)
		bad_mode = [{"row_name": contract.contracted_items[0].name, "pricing_mode": "Cheap"}]
		self.assertRaises(frappe.ValidationError, v1.update_subcontractor_contract_lines,
						  contract.name, str(contract.modified), rows=bad_mode)

	# -- pickers ---------------------------------------------------------------------

	def test_name_only_pickers_for_contracts_roles(self):
		project, _tender = make_resource_tender_and_project("v1_pick", qty=10)
		with as_user(self.ce):
			projects = v1.list_project_options(search=project.project_name)
			suppliers = v1.list_subcontractor_options(search=ensure_subcontractor())
		self.assertIn(project.name, [p.name for p in projects])
		self.assertEqual([s.name for s in suppliers], [ensure_subcontractor()])
		with as_user(self.ce):  # matched from a document's registration numbers
			by_cr = v1.list_subcontractor_options(search="TEST-CR-0001")
		self.assertIn(ensure_subcontractor(), [s.name for s in by_cr])
		self.assertEqual(by_cr[0].custom_commercial_register, "TEST-CR-0001")
		with as_user(self.pd):  # no create on any contract doctype
			self.assertRaises(frappe.PermissionError, v1.list_subcontractor_options)

	def test_list_respects_permissions(self):
		make_contract("v1_list")
		with as_user(self.cm):
			self.assertTrue(v1.list_subcontractor_contracts(limit=5)["data"])
		with as_user(make_user("Site Engineer")):
			self.assertRaises(frappe.PermissionError, v1.list_subcontractor_contracts)
