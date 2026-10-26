from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.email.doctype.email_account.email_account import EmailAccount
from frappe.tests import IntegrationTestCase

from frappe_mailgun.send import send
from frappe_mailgun.tests.fixtures import make_account, make_smtp_account, mock_request


class TestSend(IntegrationTestCase):
	def setUp(self):
		frappe.flags.testing_email = True
		with mock_request([(200, {"route": {"id": "r1"}})]):
			self.account = make_account(append_to=None)

	def tearDown(self):
		frappe.flags.testing_email = False

	def make_queue(self, recipients=("to@x.test",), reference_doctype="ToDo"):
		return frappe.get_doc(
			{
				"doctype": "Email Queue",
				"sender": "support@mg.example.test",
				"email_account": self.account.name,
				"message": "Subject: hi\n\nbody",
				"reference_doctype": reference_doctype,
				"recipients": [{"recipient": r} for r in recipients],
			}
		).insert(ignore_permissions=True)

	def test_mailgun_account_posts_mime_per_recipient(self):
		self.account.db_set("track_email_status", 0)
		q = self.make_queue(("a@x.test", "b@x.test"))
		with mock_request([(200, {}), (200, {})]) as m:
			send(q, q.sender, "a@x.test", q.message)
			send(q, q.sender, "b@x.test", q.message)
		self.assertEqual([c[2]["data"]["to"] for c in m.calls], ["a@x.test", "b@x.test"])
		data = m.calls[0][2]["data"]
		self.assertEqual(data["v:email_queue"], q.name)
		self.assertEqual(data["v:site"], frappe.local.site)
		self.assertEqual(data["o:tag"], ["frappe", "todo"])
		self.assertEqual(data["o:tracking-opens"], "no")
		self.assertEqual(data["o:tracking-clicks"], "no")

	def test_tracking_follows_the_account(self):
		self.account.db_set("track_email_status", 1)
		q = self.make_queue()
		with mock_request([(200, {})]) as m:
			send(q, q.sender, "to@x.test", q.message)
		self.assertEqual(m.calls[0][2]["data"]["o:tracking-opens"], "yes")
		self.assertEqual(m.calls[0][2]["data"]["o:tracking-clicks"], "yes")

	def test_mailgun_refusal_becomes_an_outgoing_email_error(self):
		q = self.make_queue()
		with mock_request([(400, {"message": "'to' parameter is not a valid address"})]):
			with self.assertRaises(frappe.OutgoingEmailError) as cm:
				send(q, q.sender, "to@x.test", q.message)
		self.assertIn("not a valid address", str(cm.exception))

	def test_network_error_becomes_an_outgoing_email_error(self):
		import requests

		q = self.make_queue()
		with patch(
			"frappe_mailgun.client.MailgunClient._request", side_effect=requests.ConnectionError("down")
		):
			with self.assertRaises(frappe.OutgoingEmailError) as cm:
				send(q, q.sender, "to@x.test", q.message)
		self.assertIn("could not be reached", str(cm.exception))

	def test_non_mailgun_account_uses_smtp(self):
		smtp_account = make_smtp_account()
		q = self.make_queue()
		q.db_set("email_account", smtp_account.name)
		q.reload()
		session = MagicMock()
		session.has_extn.return_value = False
		with patch.object(EmailAccount, "get_smtp_server", return_value=SimpleNamespace(session=session)):
			send(q, q.sender, "to@x.test", q.message)
		session.sendmail.assert_called_once()
		self.assertEqual(session.sendmail.call_args.kwargs["to_addrs"], "to@x.test")
		self.assertEqual(session.sendmail.call_args.kwargs["mail_options"], [])

	def smtp_queue(self, **account_values):
		smtp_account = make_smtp_account()
		for field, value in account_values.items():
			frappe.db.set_value("Email Account", smtp_account.name, field, value)
		frappe.clear_document_cache("Email Account", smtp_account.name)
		q = self.make_queue()
		q.db_set("email_account", smtp_account.name)
		return q.reload()

	def test_smtp_account_with_dsn_passes_core_options(self):
		q = self.smtp_queue(dsn_notify_type="SUCCESS,FAILURE")
		session = MagicMock()
		session.has_extn.side_effect = lambda name: name == "DSN"
		with patch.object(EmailAccount, "get_smtp_server", return_value=SimpleNamespace(session=session)):
			send(q, q.sender, "to@x.test", q.message)
		kwargs = session.sendmail.call_args.kwargs
		self.assertEqual(kwargs["mail_options"], ["RET=FULL", f"ENVID={q.name}"])
		self.assertEqual(kwargs["rcpt_options"], ["NOTIFY=SUCCESS,FAILURE"])

	def test_smtp_size_limit_refuses_a_large_message(self):
		q = self.smtp_queue()
		session = MagicMock()
		session.has_extn.side_effect = lambda name: name == "SIZE"
		session.esmtp_features = {"size": "10"}
		with patch.object(EmailAccount, "get_smtp_server", return_value=SimpleNamespace(session=session)):
			with self.assertRaises(frappe.ValidationError):
				send(q, q.sender, "to@x.test", q.message)
		session.sendmail.assert_not_called()

	def test_frappe_mail_account_sends_through_send_raw(self):
		q = self.smtp_queue(service="Frappe Mail")
		q.db_set("reference_doctype", "Newsletter")
		client = MagicMock()
		with patch.object(EmailAccount, "get_frappe_mail_client", return_value=client), patch.object(
			EmailAccount, "get_smtp_server", side_effect=AssertionError("no SMTP for Frappe Mail")
		):
			send(q, q.sender, "to@x.test", q.message)
		client.send_raw.assert_called_once_with(
			sender=q.sender, recipients="to@x.test", message=q.message, is_newsletter=True
		)

	def test_in_test_guard_without_flag_sends_nothing(self):
		frappe.flags.testing_email = False
		q = self.make_queue()
		with mock_request([]) as m:
			send(q, q.sender, "to@x.test", q.message)
		self.assertEqual(m.calls, [])

	def test_end_to_end_through_the_queue(self):
		with mock_request([(200, {})]) as m:
			frappe.sendmail(
				recipients=["to@x.test"],
				sender="support@mg.example.test",
				subject="hi",
				message="body",
				now=True,
			)
			# frappe.sendmail(now=True) sends in an after-commit hook.
			frappe.db.commit()
		self.assertEqual(m.calls[0][2]["data"]["to"], "to@x.test")
		q = frappe.get_last_doc("Email Queue")
		self.assertEqual(q.status, "Sent")
