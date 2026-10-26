from unittest.mock import patch

import frappe
import requests
from frappe.utils import get_datetime
from frappe.tests import IntegrationTestCase

from frappe_mailgun.client import MailgunClient, MailgunError
from frappe_mailgun.clock import from_unix
from frappe_mailgun.inbound import fetch, is_storage_url, notify
from frappe_mailgun.tests.fixtures import (
	RAW_MAIL,
	call_guest,
	make_account,
	make_domain,
	mock_request,
	notify_form,
)

HOSTILE_URLS = (
	# urlparse reads the host as Mailgun's; requests sends to evil.test.
	"https://evil.test\\@sw.api.mailgun.net/v3/domains/mg.example.test/messages/K",
	# Dot segments climb out of the domain's stored messages.
	"https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/../../../v3/routes",
	# Userinfo that looks like Mailgun in front of a foreign host.
	"https://api.mailgun.net@evil.test/v3/domains/mg.example.test/messages/K",
)


class TestStorageUrl(IntegrationTestCase):
	def test_a_storage_url_is_accepted(self):
		self.assertTrue(
			is_storage_url("https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/AbC=_-1", "mg.example.test")
		)

	def test_hostile_urls_are_refused(self):
		for url in HOSTILE_URLS:
			with self.subTest(url=url):
				self.assertFalse(is_storage_url(url, "mg.example.test"))

	def test_the_client_refuses_hostile_urls_before_any_request(self):
		client = MailgunClient(make_domain())
		with patch("frappe_mailgun.client.requests.request") as request:
			for url in HOSTILE_URLS:
				with self.subTest(url=url):
					with self.assertRaises(MailgunError) as cm:
						client._request("GET", url)
					self.assertEqual(cm.exception.status, 0)
		request.assert_not_called()


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

	def test_notify_treats_badly_typed_fields_as_absent(self):
		form = notify_form("sign-test", recipient="support@mg.example.test")
		form["recipient"] = {"x": 1}
		with self.assertRaises(frappe.DoesNotExistError):
			call_guest(notify, form)
		form = notify_form("sign-test", recipient="support@mg.example.test")
		form["timestamp"] = int(form["timestamp"])
		with self.assertRaises(frappe.AuthenticationError):
			call_guest(notify, form)
		form = notify_form("sign-test", recipient="support@mg.example.test")
		form["message-url"] = ["https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/K"]
		self._refused(form, frappe.ValidationError)

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
			fetch("mg.example.test", "https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/KEY2", "support@mg.example.test")
			fetch("mg.example.test", "https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/KEY2", "support@mg.example.test")
		self.assertEqual(len(m.calls), 1)
		self.assertEqual(frappe.db.count("Communication", {"message_id": "m1@customer.test"}), 1)

	def test_fetch_for_an_unknown_recipient_logs_and_returns(self):
		with mock_request([]) as m:
			fetch("mg.example.test", "https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/KEY3", "nobody@mg.example.test")
		self.assertEqual(m.calls, [])
		self.assertTrue(frappe.db.exists("Error Log", {"method": "Mailgun inbound: no account"}))

	def test_sweep_fetches_unseen_stored_events(self):
		events = {
			"items": [
				{
					"id": "e1",
					"event": "stored",
					"timestamp": 1_800_000_000,
					"storage": {"key": "KEY4", "url": "https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/KEY4"},
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

	def _refused(self, form, exc):
		with patch("frappe.enqueue") as enqueue, patch(
			"frappe_mailgun.inbound.consume_token"
		) as consume:
			with self.assertRaises(exc):
				call_guest(notify, form)
		enqueue.assert_not_called()
		consume.assert_not_called()

	def test_notify_refuses_a_foreign_host(self):
		form = notify_form("sign-test", recipient="support@mg.example.test")
		form["message-url"] = "https://evil.test/v3/domains/mg.example.test/messages/K"
		self._refused(form, frappe.ValidationError)

	def test_notify_refuses_an_http_url(self):
		form = notify_form("sign-test", recipient="support@mg.example.test")
		form["message-url"] = "http://sw.api.mailgun.net/v3/domains/mg.example.test/messages/K"
		self._refused(form, frappe.ValidationError)

	def test_notify_refuses_another_domains_path(self):
		form = notify_form("sign-test", recipient="support@mg.example.test")
		form["message-url"] = "https://sw.api.mailgun.net/v3/domains/other.test/messages/K"
		self._refused(form, frappe.ValidationError)

	def test_notify_refuses_a_missing_message_url(self):
		form = notify_form("sign-test", recipient="support@mg.example.test")
		del form["message-url"]
		self._refused(form, frappe.ValidationError)

	def test_sweep_survives_a_dead_message(self):
		def event(key, ts):
			return {
				"event": "stored",
				"timestamp": ts,
				"storage": {
					"key": key,
					"url": f"https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/{key}",
				},
			}

		events = {"items": [event("DEAD1", 1_800_000_000), event("LIVE1", 1_800_000_100)], "paging": {}}
		with mock_request([(200, events), (404, {"message": "gone"}), (200, {"body-mime": RAW_MAIL.decode()})]):
			self.account.reload().receive()
		self.assertTrue(frappe.db.exists("Mailgun Inbound Message", "DEAD1"))
		self.assertFalse(frappe.db.get_value("Mailgun Inbound Message", "DEAD1", "communication"))
		self.assertTrue(frappe.db.get_value("Mailgun Inbound Message", "LIVE1", "communication"))
		self.assertIsNotNone(
			frappe.db.get_value("Email Account", self.account.name, "mailgun_last_received_at")
		)

	def stored(self, key, ts):
		return {
			"event": "stored",
			"timestamp": ts,
			"storage": {"key": key, "url": f"https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/{key}"},
		}

	def cursor(self):
		return get_datetime(frappe.db.get_value("Email Account", self.account.name, "mailgun_last_received_at"))

	def test_sweep_leaves_a_transient_failure_for_the_next_pull(self):
		events = {
			"items": [
				self.stored("LIVE2", 1_800_000_000),
				self.stored("BUSY1", 1_800_000_100),
				self.stored("LATER1", 1_800_000_200),
			],
			"paging": {},
		}
		with mock_request([(200, events), (200, {"body-mime": RAW_MAIL.decode()}), (503, {"message": "busy"})]):
			self.account.reload().receive()
		self.assertTrue(frappe.db.exists("Mailgun Inbound Message", "LIVE2"))
		self.assertFalse(frappe.db.exists("Mailgun Inbound Message", "BUSY1"))
		self.assertFalse(frappe.db.exists("Mailgun Inbound Message", "LATER1"))
		# The cursor stops at the failed event, so the next pull lists it again.
		self.assertEqual(self.cursor(), from_unix(1_800_000_100))
		self.assertTrue(frappe.db.exists("Error Log", {"method": "Mailgun inbound: fetch failed"}))

	def test_sweep_records_a_gone_message_and_moves_on(self):
		events = {"items": [self.stored("GONE1", 1_800_000_000)], "paging": {}}
		with mock_request([(200, events), (410, {"message": "gone"})]):
			self.account.reload().receive()
		self.assertTrue(frappe.db.exists("Mailgun Inbound Message", "GONE1"))
		self.assertEqual(self.cursor(), from_unix(1_800_000_000))

	def test_fetch_with_a_timeout_logs_and_returns(self):
		frappe.db.delete("Error Log", {"method": "Mailgun inbound: fetch failed"})
		with patch.object(MailgunClient, "get_stored_message", side_effect=requests.Timeout("slow")):
			fetch("mg.example.test", "https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/SLOW1", "support@mg.example.test")
		self.assertFalse(frappe.db.exists("Mailgun Inbound Message", "SLOW1"))
		self.assertTrue(frappe.db.exists("Error Log", {"method": "Mailgun inbound: fetch failed"}))

	def test_fetch_of_a_gone_message_records_it(self):
		with mock_request([(404, {"message": "gone"})]):
			fetch("mg.example.test", "https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/GONE2", "support@mg.example.test")
		self.assertTrue(frappe.db.exists("Mailgun Inbound Message", "GONE2"))
		self.assertFalse(frappe.db.get_value("Mailgun Inbound Message", "GONE2", "communication"))

	def test_sweep_logs_a_broken_event_and_moves_on(self):
		events = {
			"items": [
				self.stored("BROKEN1", 1_800_000_000),
				{**self.stored("ODD1", "soon"), "storage": None},
				self.stored("LIVE3", 1_800_000_100),
			],
			"paging": {},
		}
		# BROKEN1's body has no body-mime, which raises a KeyError in the client.
		with mock_request([(200, events), (200, {}), (200, {"body-mime": RAW_MAIL.decode()})]):
			self.account.reload().receive()
		self.assertTrue(frappe.db.get_value("Mailgun Inbound Message", "LIVE3", "communication"))
		self.assertEqual(self.cursor(), from_unix(1_800_000_100))
		self.assertTrue(frappe.db.exists("Error Log", {"method": "Mailgun inbound: event failed"}))
