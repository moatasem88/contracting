"""Fixtures shared by the v1 API tests. Not a test module itself (no test_
prefix), so the runner doesn't collect it.

Permission tests need users holding exactly one role: every real account on
site1.local that has one of these roles also has most of the others, which
hides precisely the gaps these tests exist to catch.
"""

from contextlib import contextmanager

import frappe


def make_user(role, suffix=""):
	slug = role.lower().replace(" ", "_")
	email = f"_v1_{slug}{suffix}@example.com"
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "_v1 " + role,
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": role}],
		}).insert(ignore_permissions=True)
	return email


@contextmanager
def as_user(user):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


def keys_anywhere(data, keys):
	"""Every occurrence of any of keys in a nested dict/list - for asserting a
	field is absent from a whole response, not just its top level."""
	found = []
	if isinstance(data, dict):
		for key, value in data.items():
			if key in keys:
				found.append(key)
			found.extend(keys_anywhere(value, keys))
	elif isinstance(data, list):
		for value in data:
			found.extend(keys_anywhere(value, keys))
	return found
