import frappe
from frappe.tests import IntegrationTestCase


class TestInstall(IntegrationTestCase):
	def test_service_options_include_mailgun(self):
		options = frappe.get_meta("Email Account").get_field("service").options.split("\n")
		self.assertIn("Mailgun", options)
		self.assertIn("Frappe Mail", options)
		self.assertIn("GMail", options)

	def test_custom_fields_exist(self):
		meta = frappe.get_meta("Email Account")
		self.assertEqual(meta.get_field("mailgun_domain").fieldtype, "Link")
		self.assertEqual(meta.get_field("mailgun_domain").options, "Mailgun Domain")
		self.assertEqual(meta.get_field("mailgun_route_id").fieldtype, "Data")
		self.assertEqual(meta.get_field("mailgun_last_received_at").fieldtype, "Datetime")

	def test_setup_is_idempotent(self):
		from frappe_mailgun.install import setup

		setup()
		setup()
		self.assertEqual(
			frappe.db.count("Custom Field", {"dt": "Email Account", "fieldname": "mailgun_domain"}), 1
		)
