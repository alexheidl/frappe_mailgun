import frappe
from frappe.tests import IntegrationTestCase

from frappe_mailgun.signature import consume_token, verify_signature
from frappe_mailgun.tests.fixtures import signed


class TestSignature(IntegrationTestCase):
	def test_valid_signature(self):
		s = signed("sign-test")
		self.assertTrue(verify_signature("sign-test", **s))

	def test_wrong_key(self):
		self.assertFalse(verify_signature("other", **signed("sign-test")))

	def test_stale_timestamp(self):
		s = signed("sign-test", now=1_000_000)
		self.assertFalse(verify_signature("sign-test", **s, now=1_000_000 + 901))

	def test_non_ascii_signature_is_refused(self):
		self.assertFalse(verify_signature("k", "1000000", "t", "é", now=1000000))

	def test_missing_field(self):
		self.assertFalse(verify_signature("sign-test", "", "t", "s"))

	def test_token_is_consumed_once(self):
		self.assertTrue(consume_token("tok-1"))
		self.assertFalse(consume_token("tok-1"))
