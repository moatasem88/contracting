import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from contracting.contracting.api.external import v1
from contracting.contracting.api.external.v1_test_utils import as_user, make_user
from contracting.contracting.doctype.project_tender.test_project_tender import make_ic_project_tender

TOTALS = (
	"total_direct_cost", "total_indirect_cost", "indirect_cost_percent_of_direct_cost",
	"indirect_cost_percent_of_total_price", "total_direct_and_indirect_cost", "sub_total_overhead_and_profit",
	"total_sell_price", "total_addition_percent",
)


def make_costed_project_tender(name):
	pt = make_ic_project_tender(name)
	pt.append("indirect_cost_details", {"cost_component": "Risk", "calculation_type": "Percentage of Direct Cost",
										"rate": 5})
	pt.append("indirect_cost_details", {"cost_component": "Taxes", "calculation_type": "Fixed Amount",
										"amount": 300})
	pt.save(ignore_permissions=True)
	return pt


class TestV1ProjectTender(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.planner = make_user("Planning Manager")
		cls.ccm = make_user("cost control manager")

	def test_tc10_rate_edit_matches_desk(self):
		via_app = make_costed_project_tender("_v1 PT app")
		via_desk = make_costed_project_tender("_v1 PT desk")
		with as_user(self.planner):
			response = v1.update_project_tender_lines(
				via_app.name, str(via_app.modified),
				indirect_cost_details=[{"row_name": via_app.indirect_cost_details[0].name, "rate": 7}],
			)
		via_desk.indirect_cost_details[0].rate = 7
		via_desk.save(ignore_permissions=True)
		for field in TOTALS:
			self.assertAlmostEqual(flt(response["data"][field]), flt(via_desk.get(field)), msg=field)
		self.assertEqual(response["data"]["indirect_cost_details"][0]["rate"], 7)

	def test_fixed_amount_row_takes_amount_only(self):
		pt = make_costed_project_tender("_v1 PT fixed")
		row = pt.indirect_cost_details[1]
		self.assertRaisesRegex(
			frappe.ValidationError, "set its amount", v1.update_project_tender_lines, pt.name, str(pt.modified),
			indirect_cost_details=[{"row_name": row.name, "rate": 3}],
		)
		response = v1.update_project_tender_lines(pt.name, str(pt.modified),
												  indirect_cost_details=[{"row_name": row.name, "amount": 450}])
		self.assertEqual(flt(response["data"]["indirect_cost_details"][1]["amount"]), 450)

	def test_tc11_rate_and_amount_together_rejected(self):
		pt = make_costed_project_tender("_v1 PT both")
		self.assertRaisesRegex(
			frappe.ValidationError, "not both", v1.update_project_tender_lines, pt.name, str(pt.modified),
			indirect_cost_details=[{"row_name": pt.indirect_cost_details[0].name, "rate": 7, "amount": 1}],
		)
		self.assertEqual(str(frappe.db.get_value("Project Tender", pt.name, "modified")), str(pt.modified))

	def test_tc12_status_not_writable(self):
		pt = make_costed_project_tender("_v1 PT status")
		self.assertRaises(TypeError, v1.update_project_tender_lines, pt.name, str(pt.modified), status="Won")
		self.assertRaisesRegex(
			frappe.ValidationError, "cannot be changed", v1.update_project_tender_lines, pt.name, str(pt.modified),
			indirect_cost_details=[{"row_name": pt.indirect_cost_details[0].name, "status": "Won"}],
		)
		self.assertNotEqual(frappe.db.get_value("Project Tender", pt.name, "status"), "Won")

	def test_tc13_row_of_another_project_tender_rejected(self):
		pt = make_costed_project_tender("_v1 PT a")
		other = make_costed_project_tender("_v1 PT b")
		self.assertRaisesRegex(
			frappe.ValidationError, "is not a row of", v1.update_project_tender_lines, pt.name, str(pt.modified),
			indirect_cost_details=[{"row_name": other.indirect_cost_details[0].name, "rate": 7}],
		)

	def test_negative_value_rejected(self):
		pt = make_costed_project_tender("_v1 PT neg")
		self.assertRaises(
			frappe.ValidationError, v1.update_project_tender_lines, pt.name, str(pt.modified),
			indirect_cost_details=[{"row_name": pt.indirect_cost_details[0].name, "rate": -1}],
		)

	def test_closed_bid_is_locked(self):
		for status in ("Won", "Lost", "Cancelled", "Superseded"):
			pt = make_costed_project_tender("_v1 PT " + status)
			pt.db_set("status", status)
			with self.assertRaisesRegex(frappe.ValidationError, "can no longer be edited"):
				v1.update_project_tender_lines(
					pt.name, str(pt.modified),
					indirect_cost_details=[{"row_name": pt.indirect_cost_details[0].name, "rate": 7}],
				)
			self.assertFalse(v1.get_project_tender_view(pt.name)["meta"]["can"]["edit_lines"])

	def test_read_only_role_cannot_edit(self):
		pt = make_costed_project_tender("_v1 PT ro")
		with as_user(self.ccm):
			view = v1.get_project_tender_view(pt.name)
			self.assertFalse(view["meta"]["can"]["write"])
			self.assertRaises(
				frappe.PermissionError, v1.update_project_tender_lines, pt.name, str(pt.modified),
				indirect_cost_details=[{"row_name": pt.indirect_cost_details[0].name, "rate": 7}],
			)

	def test_stale_modified_rejected(self):
		pt = make_costed_project_tender("_v1 PT stale")
		self.assertRaises(
			frappe.TimestampMismatchError, v1.update_project_tender_lines, pt.name, "2001-01-01 00:00:00",
			indirect_cost_details=[{"row_name": pt.indirect_cost_details[0].name, "rate": 7}],
		)
