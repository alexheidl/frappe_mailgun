import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from unittest.mock import patch

import frappe
import requests

from frappe_mailgun.client import MailgunClient, _raise_for_status


def make_domain(name: str = "mg.example.test", region: str = "EU"):
	"""Return the Mailgun Domain `name`, inserting it with test keys if missing."""
	if frappe.db.exists("Mailgun Domain", name):
		return frappe.get_doc("Mailgun Domain", name)
	return frappe.get_doc(
		{
			"doctype": "Mailgun Domain",
			"domain": name,
			"region": region,
			"api_key": "key-test",
			"webhook_signing_key": "sign-test",
		}
	).insert(ignore_permissions=True)


def signed(signing_key: str, now: int | None = None) -> dict:
	"""Return a Mailgun webhook signature triple for `signing_key`, timestamped at `now`."""
	timestamp = str(int(now or time.time()))
	token = frappe.generate_hash(length=50)
	signature = hmac.new(
		signing_key.encode(), (timestamp + token).encode(), hashlib.sha256
	).hexdigest()
	return {"timestamp": timestamp, "token": token, "signature": signature}


class _Recorder:
	"""Holds the recorded `(method, path, kwargs)` calls on `.calls`."""

	def __init__(self):
		self.calls: list[tuple[str, str, dict]] = []


def _fake_response(status: int, body: dict | bytes) -> requests.Response:
	response = requests.Response()
	response.status_code = status
	response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
	return response


@contextmanager
def mock_request(responses: list[tuple[int, dict | bytes]]):
	"""Patch `MailgunClient._request` to return `responses` in order; yields the recorded calls."""
	recorder = _Recorder()
	queue = list(responses)

	def fake(self, method, path, **kwargs):
		recorder.calls.append((method, path, kwargs))
		if not queue:
			raise AssertionError(f"unexpected request {method} {path}: no response left")
		response = _fake_response(*queue.pop(0))
		_raise_for_status(response)
		return response

	with patch.object(MailgunClient, "_request", fake):
		yield recorder


def make_account(
	email_id: str = "support@mg.example.test",
	*,
	incoming: bool = True,
	outgoing: bool = True,
	append_to: str = "ToDo",
	domain: str | None = None,
):
	"""Insert a Mailgun Email Account for `email_id`, replacing an existing one. Route calls are mocked by the caller."""
	name = f"Mailgun {email_id}"
	if frappe.db.exists("Email Account", name):
		# Clear the route id so deleting never calls Mailgun.
		frappe.db.set_value("Email Account", name, "mailgun_route_id", None)
		frappe.delete_doc("Email Account", name, force=True)
	return frappe.get_doc(
		{
			"doctype": "Email Account",
			"email_account_name": name,
			"email_id": email_id,
			"service": "Mailgun",
			"mailgun_domain": make_domain(domain or "mg.example.test").name,
			"enable_incoming": int(incoming),
			"enable_outgoing": int(outgoing),
			"append_to": append_to,
		}
	).insert(ignore_permissions=True)


def make_smtp_account():
	"""Insert an outgoing-only GMail Email Account, replacing an existing one. Core skips connections under `frappe.in_test`."""
	name = "SMTP smtp@example.test"
	if frappe.db.exists("Email Account", name):
		frappe.delete_doc("Email Account", name, force=True)
	return frappe.get_doc(
		{
			"doctype": "Email Account",
			"email_account_name": name,
			"email_id": "smtp@example.test",
			"service": "GMail",
			"awaiting_password": 1,
			"enable_outgoing": 1,
			"enable_incoming": 0,
		}
	).insert(ignore_permissions=True)


RAW_MAIL = b"From: Customer <c@customer.test>\r\nTo: support@mg.example.test\r\nSubject: Help\r\nMessage-ID: <m1@customer.test>\r\nDate: Sun, 26 Oct 2026 10:00:00 +0530\r\nX-Mailgun-Spf: Pass\r\nX-Mailgun-Dkim-Check-Result: Pass\r\nContent-Type: text/plain\r\n\r\nMy order is late.\r\n"


def call_guest(fn, form: dict):
	"""Call `fn` as Guest with `form` as the form dict, restoring the previous user and form dict."""
	user, form_dict = frappe.session.user, frappe.local.form_dict
	frappe.set_user("Guest")
	frappe.local.form_dict = frappe._dict(form)
	try:
		return fn()
	finally:
		frappe.set_user(user)
		frappe.local.form_dict = form_dict


def notify_form(key: str, recipient: str) -> dict:
	"""Return the form Mailgun posts for a stored message to `recipient`, signed with `key`."""
	domain = recipient.rpartition("@")[2]
	return {
		**signed(key),
		"recipient": recipient,
		"message-url": f"https://sw.api.mailgun.net/v3/domains/{domain}/messages/KEYn",
	}
