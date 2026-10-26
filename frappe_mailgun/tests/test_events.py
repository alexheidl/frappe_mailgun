import frappe
from frappe.tests import IntegrationTestCase

from frappe_mailgun.events import apply_event, sweep, webhook
from frappe_mailgun.tests.fixtures import (
	call_guest_json,
	event_data,
	make_account,
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

	def test_one_bounced_cc_is_partially_sent(self):
		with mock_request([(200, {}), (200, {})]):
			frappe.sendmail(
				recipients=["a@x.test"],
				cc=["b@x.test"],
				sender="support@mg.example.test",
				subject="hi",
				message="b",
				now=True,
			)
			frappe.db.commit()
		q = frappe.get_last_doc("Email Queue")
		apply_event(event_data("delivered", recipient="a@x.test", email_queue=q.name, site=frappe.local.site))
		apply_event(
			event_data(
				"failed",
				recipient="b@x.test",
				email_queue=q.name,
				site=frappe.local.site,
				severity="permanent",
				reason="bounce",
			)
		)
		q.reload()
		self.assertEqual(q.status, "Partially Sent")

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
