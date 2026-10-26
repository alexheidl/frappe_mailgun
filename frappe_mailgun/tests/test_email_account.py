from unittest.mock import patch

import frappe
from frappe.email.doctype.email_account.email_account import EmailAccount
from frappe.tests import IntegrationTestCase

from frappe_mailgun.overrides.email_account import MailgunEmailAccount
from frappe_mailgun.tests.fixtures import make_account, make_domain, mock_request


class TestMailgunEmailAccount(IntegrationTestCase):
	def test_class_is_overridden(self):
		self.assertIs(type(frappe.new_doc("Email Account")), MailgunEmailAccount)

	def test_requires_matching_domain(self):
		with self.assertRaises(frappe.ValidationError):
			make_account("support@other.test")

	def test_validate_forces_mailgun_settings(self):
		with mock_request([(200, {"route": {"id": "r1"}})]):
			acc = make_account()
		self.assertEqual(acc.use_imap, 0)
		self.assertEqual(acc.always_use_account_email_id_as_sender, 1)
		self.assertEqual(acc.no_smtp_authentication, 1)

	def test_saving_an_incoming_account_creates_the_route(self):
		with mock_request([(200, {"route": {"id": "r1"}})]) as m:
			acc = make_account()
		self.assertEqual(acc.mailgun_route_id, "r1")
		method, path, kw = m.calls[0]
		self.assertEqual((method, path), ("POST", "/v3/routes"))
		self.assertEqual(kw["data"]["expression"], 'match_recipient("support@mg.example.test")')
		notify = frappe.utils.get_url("/api/method/frappe_mailgun.inbound.notify")
		self.assertIn(f'store(notify="{notify}")', kw["data"]["action"])
		self.assertIn("stop()", kw["data"]["action"])

	def test_disabling_incoming_deletes_the_route(self):
		with mock_request([(200, {"route": {"id": "r1"}}), (200, {})]) as m:
			acc = make_account()
			acc.enable_incoming = 0
			acc.save()
		self.assertEqual(m.calls[1][:2], ("DELETE", "/v3/routes/r1"))
		self.assertFalse(acc.mailgun_route_id)

	def test_outgoing_only_account_creates_no_route(self):
		with mock_request([]) as m:
			make_account(incoming=False)
		self.assertEqual(m.calls, [])

	def test_smtp_server_is_a_null_object(self):
		with mock_request([(200, {"route": {"id": "r1"}})]):
			acc = make_account()
		server = acc.get_smtp_server()
		self.assertIsNone(server.session)
		server.quit()

	def test_no_route_call_when_the_parents_on_update_fails(self):
		with mock_request([]) as m, patch.object(EmailAccount, "on_update", side_effect=RuntimeError):
			with self.assertRaises(RuntimeError):
				make_account()
		self.assertEqual(m.calls, [])

	def test_domain_is_fixed_while_a_route_exists(self):
		with mock_request([(200, {"route": {"id": "r1"}})]):
			acc = make_account()
		acc.mailgun_domain = make_domain("other.example.test").name
		acc.email_id = "support@other.example.test"
		with self.assertRaises(frappe.ValidationError) as ctx:
			acc.save()
		self.assertIn("Disable incoming mail before changing the Mailgun domain", str(ctx.exception))
