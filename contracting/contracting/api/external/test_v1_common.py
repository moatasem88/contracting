import ast
import inspect
import os
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from contracting.contracting.api.external import _common, v1
from contracting.contracting.api.external.v1_test_utils import as_user, make_user
from contracting.contracting.doctype.subcontractor_contract.test_subcontractor_contract import (
	make_draft_contract,
	make_resource_tender_and_project,
)

PACKAGE_DIR = os.path.dirname(_common.__file__)
SERVICE_FILES = sorted(
	f for f in os.listdir(PACKAGE_DIR)
	if f.endswith(".py") and not f.startswith("test_") and f != "v1_test_utils.py"
)


def make_contract(suffix):
	project, tender = make_resource_tender_and_project(suffix, qty=10)
	contract = make_draft_contract(project, "Supplying and installing")
	contract.append("contracted_items", {"work_item": tender.boq_items[0].name, "qty": 10, "unit_price": 100})
	return contract.insert(ignore_permissions=True)


class TestV1Guarantees(FrappeTestCase):
	"""BRD FR-07 / FR-30 / TC-08, checked against the source itself."""

	def _sources(self):
		for name in SERVICE_FILES:
			with open(os.path.join(PACKAGE_DIR, name)) as f:
				yield name, f.read()

	def test_no_permission_bypass(self):
		banned = re.compile(r"ignore_permissions|ignore_validate|ignore_mandatory|flags\.ignore_")
		for name, source in self._sources():
			code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
			self.assertFalse(_outside_docstrings(code, banned), name)

	def test_no_direct_docstatus_or_workflow_state_writes(self):
		for name, source in self._sources():
			tree = ast.parse(source)
			for node in ast.walk(tree):
				targets = node.targets if isinstance(node, ast.Assign) else []
				for target in targets:
					if isinstance(target, ast.Attribute):
						self.assertNotIn(target.attr, ("docstatus", "workflow_state"), f"{name}:{node.lineno}")
				if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("db_set", "set_value"):
					self.fail(f"{name}:{node.lineno} writes through db_set/set_value")

	def test_no_child_doctype_is_listed(self):
		"""Child rows only ever leave inside their parent's view."""
		child_doctypes = set(frappe.get_all("DocType", filters={"istable": 1, "module": "Contracting"}, pluck="name"))
		for name, source in self._sources():
			for doctype in re.findall(r'frappe\.get_(?:list|all)\(\s*"([^"]+)"', source):
				self.assertNotIn(doctype, child_doctypes, name)

	def test_every_public_method_is_whitelisted_and_no_passthrough(self):
		public = [n for n, f in inspect.getmembers(v1, inspect.isfunction)
				  if f.__module__ == v1.__name__ and not n.startswith("_")]
		self.assertGreater(len(public), 30)
		for name in public:
			self.assertIn(getattr(v1, name), frappe.whitelisted, name)
			self.assertFalse(re.match(r"(get_doc|get_list|run|call|sql|save|delete)$", name), name)
		for name in public:
			if name in DOCTYPE_ARG_ALLOWED:
				continue
			params = inspect.signature(getattr(v1, name)).parameters
			self.assertNotIn("doctype", params, name)

	def test_doctype_arg_methods_refuse_other_doctypes(self):
		"""The one exception above takes a doctype, but only the app's own."""
		for doctype in ("User", "Sales Invoice", "Contractor Contract Item"):
			self.assertRaises(frappe.PermissionError, v1.get_document_timeline, doctype, "x")


# Read-only and restricted to _session.APP_DOCTYPES (tested below).
DOCTYPE_ARG_ALLOWED = ("get_document_timeline",)


def _outside_docstrings(code, pattern):
	tree = ast.parse(code)
	docstrings = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
			doc = ast.get_docstring(node, clean=False)
			if doc:
				docstrings.add(doc)
	stripped = code
	for doc in docstrings:
		stripped = stripped.replace(doc, "")
	return bool(pattern.search(stripped))


class TestV1Common(FrappeTestCase):
	def test_envelope_shape(self):
		contract = make_contract("v1_env")
		response = v1.get_subcontractor_contract_view(contract.name)
		self.assertEqual(set(response), {"data", "meta"})
		self.assertTrue({"doctype", "name", "modified", "docstatus", "workflow_state", "allowed_actions", "can"}
						<= set(response["meta"]))
		self.assertEqual(response["meta"]["modified"], str(contract.modified))

	def test_list_limit_is_capped(self):
		self.assertEqual(v1.list_tenders(limit=1000)["meta"]["limit"], _common.MAX_LIMIT)
		self.assertEqual(v1.list_tenders()["meta"]["limit"], _common.DEFAULT_LIMIT)

	def test_missing_document(self):
		self.assertRaises(frappe.DoesNotExistError, v1.get_tender_view, "TND-NOPE")

	def test_inbox_only_lists_actionable_documents(self):
		contract = make_contract("v1_inbox")
		with as_user(make_user("Contracts Manager")):
			names = [d["name"] for d in v1.list_my_pending_approvals(limit=100)["data"]]
		self.assertIn(contract.name, names)  # Draft: Contracts Manager can Submit for Approval
		with as_user(make_user("Projects Director")):
			names = [d["name"] for d in v1.list_my_pending_approvals(limit=100)["data"]]
		self.assertNotIn(contract.name, names)
		contract.db_set("workflow_state", "Pending Projects Director Approval")
		with as_user(make_user("Projects Director")):
			entries = [d for d in v1.list_my_pending_approvals(limit=100)["data"] if d["name"] == contract.name]
		self.assertEqual([a["action"] for a in entries[0]["allowed_actions"]], ["Approve", "Reject"])

	def test_strip_keeps_keys_for_privileged_user(self):
		contract = make_contract("v1_strip_admin")
		self.assertIn("tender_unit_cost", _common.strip(contract)["contracted_items"][0])


class TestV1Session(FrappeTestCase):
	def test_session_info_reflects_single_role(self):
		with as_user(make_user("Contracts Engineer")):
			info = v1.get_session_info()
		self.assertEqual(info["roles"].count("Contracts Engineer"), 1)
		self.assertTrue(info["can"]["Subcontractor Contract"]["create"])
		self.assertFalse(info["can"]["Tender"]["read"])
		self.assertIn(info["language"], ("en", "ar"))

	def test_timeline_lists_creation_and_workflow_changes(self):
		contract = make_contract("v1_timeline")
		frappe.get_doc({
			"doctype": "Comment", "comment_type": "Workflow", "content": "Pending Contracts Manager Approval",
			"reference_doctype": contract.doctype, "reference_name": contract.name,
		}).insert(ignore_permissions=True)
		with as_user(make_user("Contracts Manager")):
			events = v1.get_document_timeline(contract.doctype, contract.name)["data"]
		self.assertEqual([e["type"] for e in events], ["Created", "State"])
		self.assertEqual(events[1]["content"], "Pending Contracts Manager Approval")

	def test_timeline_needs_read_on_the_document(self):
		contract = make_contract("v1_timeline_perm")
		with as_user(make_user("Site Engineer")):
			self.assertRaises(frappe.PermissionError, v1.get_document_timeline, contract.doctype, contract.name)
