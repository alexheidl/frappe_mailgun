import frappe
from frappe.tests import IntegrationTestCase

from frappe_mailgun.retention import run
from frappe_mailgun.signature import consume_token


class TestRetention(IntegrationTestCase):
	def test_old_tokens_are_deleted(self):
		consume_token("tok-old")
		frappe.db.set_value(
			"Mailgun Webhook Token",
			"tok-old",
			"creation",
			frappe.utils.add_days(frappe.utils.now_datetime(), -2),
		)
		consume_token("tok-new")
		run()
		self.assertFalse(frappe.db.exists("Mailgun Webhook Token", "tok-old"))
		self.assertTrue(frappe.db.exists("Mailgun Webhook Token", "tok-new"))
