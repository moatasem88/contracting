"""Request-scoped permlevel enforcement for Subcontractor Contract's API reads.

Follows Tender BRD FR-15 (2026-10-13): Tender Unit Cost / Tender Rate are
permlevel 1 so the contracts department (Contracts Manager, Contracts
Engineer) never receives them. The desk form already strips them
(frappe.desk.form.load.getdoc applies field-level read permissions
unconditionally). Every other read path - frappe.client.get / get_list /
get_value, /api/resource, Report View and its export - only does so when
System Settings "Apply Perm Level on API Calls" is on, and it is off on this
site. Turning it on site-wide would change API responses for every doctype
with permlevel fields (Purchase Invoice, Sales Order, Project, ...), so this
turns Frappe's own enforcement on for the current request only, and only
when that request reads one of the doctypes below.

frappe.get_system_settings() reads frappe.local.system_settings, which lives
for one request (frappe.destroy() releases frappe.local afterwards), so the
override never outlives the request it was set for.
"""

from urllib.parse import unquote

import frappe

GUARDED_DOCTYPES = {
	"Subcontractor Contract",
	"Contractor Contract Item",
	"Contractor Contract Material Item",
	"Contractor Contract Labor Item",
	"Contractor Contract Subcontract Item",
	"Contractor Contract Equipment Item",
}

# form_dict keys that name the doctype a whitelisted read method acts on
# (frappe.client.*, frappe.desk.reportview.get / export_query, ...).
_DOCTYPE_KEYS = ("doctype", "parent", "parenttype", "parent_doctype")


def apply_permlevel_on_guarded_reads():
	"""before_request hook."""
	if not frappe.request or not _targets_guarded_doctype():
		return

	settings = frappe._dict(frappe.get_cached_doc("System Settings").as_dict())
	settings.apply_perm_level_on_api_calls = 1
	frappe.local.system_settings = settings


def _targets_guarded_doctype():
	path = unquote(frappe.request.path or "")
	if path.startswith("/api/resource/"):
		return path[len("/api/resource/"):].split("/", 1)[0] in GUARDED_DOCTYPES

	form_dict = frappe.local.form_dict or {}
	return any(form_dict.get(key) in GUARDED_DOCTYPES for key in _DOCTYPE_KEYS)


# ---------------------------------------------------------------------------
# Response scrub - every path that *echoes* a saved document back
# ---------------------------------------------------------------------------
# The before_request guard covers the read APIs that honour the system
# setting. Write paths don't consult it at all: frappe.desk.form.save.
# savedocs, frappe.model.workflow.apply_workflow, frappe.client.save/submit
# all return the full saved document - so a Contracts Manager saving a
# contract would receive the freshly computed Tender Unit Cost in the save
# response. Stripping the fields from the document itself inside the save
# would also strip them from its Version diff, logging "value -> None" with
# the old value in the timeline - so this works on the outgoing JSON
# instead. Both fieldnames exist only on this feature's doctypes.

SCRUBBED_FIELDS = ("tender_unit_cost", "tender_rate")
_SCRUBBED_BYTES = tuple(f.encode() for f in SCRUBBED_FIELDS)
PERMLEVEL_PARENT = "Subcontractor Contract"


def scrub_tender_cost_from_response(response=None, request=None):
	"""after_request hook. Fails closed: Frappe logs and ignores exceptions
	raised here, which would otherwise send the unscrubbed body."""
	if response is None or response.mimetype != "application/json":
		return
	data = response.get_data()
	if not any(marker in data for marker in _SCRUBBED_BYTES):
		return
	try:
		if _may_read_tender_cost():
			return
		response.set_data(frappe.as_json(_strip(frappe.parse_json(data.decode()))))
	except Exception:
		frappe.logger().error("Tender cost response scrub failed - body withheld", exc_info=True)
		response.status_code = 500
		response.set_data(frappe.as_json({"exc_type": "PermissionError", "message": "Response withheld"}))


def _may_read_tender_cost():
	user = frappe.session.user if getattr(frappe.local, "session", None) else "Guest"
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	return any(
		perm.permlevel == 1 and perm.read and perm.role in roles
		for perm in frappe.get_meta(PERMLEVEL_PARENT).permissions
	)


def _strip(value):
	if isinstance(value, dict):
		return {k: _strip(v) for k, v in value.items() if k not in SCRUBBED_FIELDS}
	if isinstance(value, list):
		return [_strip(v) for v in value]
	return value
