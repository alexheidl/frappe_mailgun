import json
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
