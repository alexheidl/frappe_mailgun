from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_mailgun.client import MailgunClient
from frappe_mailgun.clock import from_unix, to_unix
from frappe_mailgun.events import apply_event, sweep, webhook
from frappe_mailgun.tests.fixtures import (
	call_guest_json,
	event_data,
	make_account,
	make_domain,
	mock_request,
	signed,
)


class TestEvents(IntegrationTestCase):
	def setUp(self):
		frappe.flags.testing_email = True
		with mock_request([(200, {"route": {"id": "r1"}})]):
			self.account = make_account(append_to=None)
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert()
		# frappe.sendmail does not create a Communication, so make the one the queue links to.
		self.comm = frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"communication_medium": "Email",
				"sent_or_received": "Sent",
				"subject": "hi",
				"sender": "support@mg.example.test",
				"recipients": "to@x.test",
				"reference_doctype": "ToDo",
				"reference_name": self.todo.name,
			}
		).insert(ignore_permissions=True)
		with mock_request([(200, {})]):
			frappe.sendmail(
				recipients=["to@x.test"],
				sender="support@mg.example.test",
				subject="hi",
				message="b",
				reference_doctype="ToDo",
				reference_name=self.todo.name,
				communication=self.comm.name,
				now=True,
			)
			# frappe.sendmail(now=True) sends in an after-commit hook.
			frappe.db.commit()
		self.queue = frappe.get_last_doc("Email Queue")

	def tearDown(self):
		frappe.flags.testing_email = False

	def ev(self, event, **extra):
		extra.setdefault("site", frappe.local.site)
		return event_data(event, recipient="to@x.test", email_queue=self.queue.name, **extra)

	def test_delivered_marks_recipient_and_queue_sent(self):
		apply_event(self.ev("delivered"))
		self.queue.reload()
		self.assertEqual(self.queue.recipients[0].status, "Sent")
		self.assertEqual(self.queue.status, "Sent")

	def test_permanent_failure_sets_error_and_bounced(self):
		apply_event(self.ev("failed", severity="permanent", reason="bounce", message="550 no such user"))
		self.queue.reload()
		self.assertEqual(self.queue.status, "Error")
		self.assertIn("550", self.queue.recipients[0].error)
		self.assertEqual(
			frappe.db.get_value("Communication", self.comm.name, "delivery_status"), "Bounced"
		)

	def row_of(self, queue, recipient):
		return next(r for r in queue.recipients if r.recipient == recipient)

	def test_one_bounced_cc_leaves_the_queue_sent(self):
		q = self.send_to(recipients=["a@x.test"], cc=["b@x.test"])
		site = frappe.local.site
		apply_event(event_data("delivered", recipient="a@x.test", email_queue=q.name, site=site))
		apply_event(event_data("failed", recipient="b@x.test", email_queue=q.name, site=site, severity="permanent", reason="bounce"))
		q.reload()
		# Core retries Partially Sent queues, so a mixed outcome stays Sent with the error on the row.
		self.assertEqual(q.status, "Sent")
		self.assertTrue(self.row_of(q, "b@x.test").error)
		self.assertFalse(self.row_of(q, "a@x.test").error)

	def test_every_row_failing_sets_error(self):
		q = self.send_to(recipients=["a@x.test"], cc=["b@x.test"])
		site = frappe.local.site
		for r in ("a@x.test", "b@x.test"):
			apply_event(event_data("failed", recipient=r, email_queue=q.name, site=site, severity="permanent", reason="bounce"))
		q.reload()
		self.assertEqual(q.status, "Error")

	def test_a_queue_still_sending_is_not_rolled_up(self):
		for status in ("Not Sent", "Sending", "Partially Sent"):
			with self.subTest(status=status):
				self.queue.db_set("status", status)
				apply_event(self.ev("failed", severity="permanent", reason="bounce"))
				self.queue.reload()
				self.assertEqual(self.queue.status, status)

	def test_a_queue_in_error_stays_in_error(self):
		self.queue.db_set("status", "Error")
		apply_event(self.ev("delivered"))
		self.queue.reload()
		self.assertEqual(self.queue.status, "Error")

	def test_statuses_only_move_forward(self):
		apply_event(self.ev("opened"))
		apply_event(self.ev("delivered"))
		self.assertEqual(
			frappe.db.get_value("Communication", self.comm.name, "delivery_status"), "Opened"
		)

	def test_unsubscribe_is_scoped_to_the_reference(self):
		apply_event(self.ev("unsubscribed"))
		unsub = frappe.get_last_doc("Email Unsubscribe", {"email": "to@x.test"})
		self.assertEqual(
			(unsub.reference_doctype, unsub.reference_name, unsub.global_unsubscribe),
			("ToDo", self.todo.name, 0),
		)

	def test_another_sites_event_is_ignored(self):
		self.assertIsNone(apply_event(self.ev("delivered", site="other.localhost")))

	def test_missing_queue_is_stored_without_links(self):
		name = apply_event(
			event_data("delivered", recipient="x@x.test", email_queue="EQ-gone", site=frappe.local.site)
		)
		self.assertFalse(frappe.db.get_value("Mailgun Event", name, "email_queue"))

	def test_duplicate_event_id_is_ignored(self):
		e = self.ev("delivered")
		self.assertIsNotNone(apply_event(e))
		self.assertIsNone(apply_event(e))

	def test_webhook_refuses_a_bad_signature(self):
		body = {"signature": signed("wrong"), "event-data": self.ev("delivered")}
		with self.assertRaises(frappe.AuthenticationError):
			call_guest_json(webhook, body)

	def test_webhook_applies_a_signed_event_once(self):
		body = {"signature": signed("sign-test"), "event-data": self.ev("delivered")}
		self.assertEqual(call_guest_json(webhook, body), {"status": "ok"})
		self.assertEqual(call_guest_json(webhook, body), {"status": "duplicate"})

	def test_sweep_applies_unseen_events_and_moves_the_cursor(self):
		items = {
			"items": [self.ev("delivered", id="sw1", timestamp=1_800_000_000)],
			"paging": {"next": "https://x/n"},
		}
		with mock_request([(200, items), (200, {"items": [], "paging": {}})]):
			sweep()
		self.assertTrue(frappe.db.exists("Mailgun Event", "sw1"))
		self.assertIsNotNone(frappe.db.get_value("Mailgun Domain", "mg.example.test", "last_events_at"))

	def comm_status(self):
		return frappe.db.get_value("Communication", self.comm.name, "delivery_status")

	def test_delayed_then_delivered_becomes_sent(self):
		apply_event(self.ev("failed", severity="temporary"))
		self.assertEqual(self.comm_status(), "Delayed")
		apply_event(self.ev("delivered"))
		self.assertEqual(self.comm_status(), "Sent")

	def test_a_second_unsubscribe_is_a_noop(self):
		apply_event(self.ev("unsubscribed"))
		apply_event(self.ev("unsubscribed"))
		self.assertEqual(frappe.db.count("Email Unsubscribe", {"email": "to@x.test", "reference_name": self.todo.name}), 1)

	def test_unknown_domain_is_refused_like_a_bad_signature(self):
		event = self.ev("delivered", site="nowhere.example")
		event["message"] = {"headers": {"message-id": "<x@nowhere.example>"}}
		event["user-variables"]["email_queue"] = "EQ-gone"
		with self.assertRaises(frappe.AuthenticationError):
			call_guest_json(webhook, {"signature": signed("k"), "event-data": event})
		with self.assertRaises(frappe.AuthenticationError):
			call_guest_json(webhook, {"signature": signed("k")})

	def send_to(self, **kwargs):
		with mock_request([(200, {})] * 2):
			frappe.sendmail(sender="support@mg.example.test", subject="hi", message="b", now=True, **kwargs)
			frappe.db.commit()
		return frappe.get_last_doc("Email Queue")

	def test_failure_then_delivery_for_the_other_recipient_stays_sent(self):
		q = self.send_to(recipients=["a@x.test"], cc=["b@x.test"])
		site = frappe.local.site
		apply_event(event_data("failed", recipient="b@x.test", email_queue=q.name, site=site, severity="permanent", reason="bounce"))
		apply_event(event_data("delivered", recipient="a@x.test", email_queue=q.name, site=site))
		q.reload()
		self.assertEqual(q.status, "Sent")

	def test_delivery_for_an_address_not_on_the_queue_leaves_it_alone(self):
		apply_event(event_data("delivered", recipient="zz@x.test", email_queue=self.queue.name, site=frappe.local.site))
		self.queue.reload()
		self.assertEqual(self.queue.status, "Sent")
		apply_event(self.ev("failed", severity="permanent", reason="bounce"))
		apply_event(event_data("delivered", recipient="zz@x.test", email_queue=self.queue.name, site=frappe.local.site))
		self.queue.reload()
		self.assertEqual(self.queue.status, "Error")

	def test_a_non_numeric_timestamp_is_stored_as_none(self):
		e = self.ev("delivered")
		e["timestamp"] = "soon"
		name = apply_event(e)
		self.assertFalse(frappe.db.get_value("Mailgun Event", name, "timestamp"))

	def drop_domain(self, name):
		frappe.delete_doc("Mailgun Domain", name, force=True)
		frappe.db.commit()

	def test_sweep_survives_a_failing_domain(self):
		make_domain("mg2.example.test")
		self.addCleanup(self.drop_domain, "mg2.example.test")
		queue_name = self.queue.name
		frappe.db.set_value("Mailgun Domain", "mg.example.test", "last_events_at", None)
		frappe.db.set_value("Mailgun Domain", "mg2.example.test", "last_events_at", None)

		def fake(self, params):
			if self.domain == "mg.example.test":
				raise RuntimeError("boom")
			yield event_data("delivered", recipient="to@x.test", email_queue=queue_name, site=frappe.local.site, id="sw2", timestamp=1_800_000_000)

		with patch.object(MailgunClient, "list_events", fake):
			sweep()
		self.assertTrue(frappe.db.exists("Mailgun Event", "sw2"))
		self.assertIsNotNone(frappe.db.get_value("Mailgun Domain", "mg2.example.test", "last_events_at"))


class TestClock(IntegrationTestCase):
	def test_system_timezone_round_trip(self):
		previous = frappe.db.get_single_value("System Settings", "time_zone")
		self.addCleanup(self.set_zone, previous)
		self.set_zone("Asia/Kolkata")
		dt = datetime(2026, 1, 1, 5, 30)
		self.assertEqual(to_unix(dt), 1767225600)
		self.assertEqual(from_unix(to_unix(dt)), dt)

	def set_zone(self, zone):
		frappe.db.set_single_value("System Settings", "time_zone", zone)
		frappe.clear_cache()
