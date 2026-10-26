from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_mailgun.inbound import fetch, notify
from frappe_mailgun.tests.fixtures import (
	RAW_MAIL,
	call_guest,
	make_account,
	mock_request,
	notify_form,
)


class TestInbound(IntegrationTestCase):
	def setUp(self):
		# Core's receive loop commits, so earlier tests' mail and records outlive their rollback.
		frappe.db.delete("Communication", {"message_id": "m1@customer.test"})
		frappe.db.delete("Mailgun Inbound Message")
		with mock_request([(200, {"route": {"id": "r1"}})]):
			self.account = make_account()

	def test_notify_refuses_a_bad_signature(self):
		form = notify_form("wrong-key", recipient="support@mg.example.test")
		with self.assertRaises(frappe.AuthenticationError):
			call_guest(notify, form)

	def test_notify_refuses_an_unknown_domain(self):
		form = notify_form("sign-test", recipient="support@nowhere.test")
		with self.assertRaises(frappe.DoesNotExistError):
			call_guest(notify, form)

	def test_notify_queues_once_per_token(self):
		form = notify_form("sign-test", recipient="support@mg.example.test")
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(call_guest(notify, form)["status"], "queued")
			self.assertEqual(call_guest(notify, form)["status"], "duplicate")
		enqueue.assert_called_once()

	def test_fetch_creates_a_todo_and_a_communication(self):
		with mock_request([(200, {"body-mime": RAW_MAIL.decode()})]):
			fetch(
				"mg.example.test",
				"https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/KEY1",
				"support@mg.example.test",
			)
		comm = frappe.get_last_doc("Communication", {"message_id": "m1@customer.test"})
		self.assertEqual(comm.reference_doctype, "ToDo")
		self.assertEqual(comm.sender, "c@customer.test")
		row = frappe.get_doc("Mailgun Inbound Message", "KEY1")
		self.assertEqual(row.communication, comm.name)
		self.assertEqual((row.spf, row.dkim, row.spam_score), ("Pass", "Pass", ""))

	def test_the_same_storage_key_is_processed_once(self):
		with mock_request([(200, {"body-mime": RAW_MAIL.decode()})]) as m:
			fetch("mg.example.test", "https://x/messages/KEY2", "support@mg.example.test")
			fetch("mg.example.test", "https://x/messages/KEY2", "support@mg.example.test")
		self.assertEqual(len(m.calls), 1)
		self.assertEqual(frappe.db.count("Communication", {"message_id": "m1@customer.test"}), 1)

	def test_fetch_for_an_unknown_recipient_logs_and_returns(self):
		with mock_request([]) as m:
			fetch("mg.example.test", "https://x/messages/KEY3", "nobody@mg.example.test")
		self.assertEqual(m.calls, [])
		self.assertTrue(frappe.db.exists("Error Log", {"method": "Mailgun inbound: no account"}))

	def test_sweep_fetches_unseen_stored_events(self):
		events = {
			"items": [
				{
					"id": "e1",
					"event": "stored",
					"timestamp": 1_800_000_000,
					"storage": {"key": "KEY4", "url": "https://x/messages/KEY4"},
				}
			],
			"paging": {"next": "https://x/next"},
		}
		with mock_request(
			[(200, events), (200, {"items": [], "paging": {}}), (200, {"body-mime": RAW_MAIL.decode()})]
		):
			self.account.reload().receive()
		self.assertTrue(frappe.db.exists("Mailgun Inbound Message", "KEY4"))
		self.assertIsNotNone(
			frappe.db.get_value("Email Account", self.account.name, "mailgun_last_received_at")
		)
