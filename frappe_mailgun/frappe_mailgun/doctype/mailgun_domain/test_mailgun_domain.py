import frappe
from frappe.tests import IntegrationTestCase

from frappe_mailgun.client import MailgunClient, MailgunError
from frappe_mailgun.tests.fixtures import make_domain, mock_request

DOMAIN_JSON = {
	"domain": {"state": "active", "spam_action": "tag"},
	"receiving_dns_records": [{"record_type": "MX", "valid": "valid"}],
	"sending_dns_records": [
		{"record_type": "TXT", "name": "mg.example.test", "valid": "valid", "value": "v=spf1 ..."},
		{
			"record_type": "TXT",
			"name": "s1._domainkey.mg.example.test",
			"valid": "valid",
			"value": "k=rsa; ...",
		},
	],
}


class TestMailgunDomain(IntegrationTestCase):
	def test_base_url_by_region(self):
		self.assertEqual(MailgunClient(make_domain(region="EU")).base_url, "https://api.eu.mailgun.net")
		self.assertEqual(
			MailgunClient(make_domain("us.example.test", region="US")).base_url, "https://api.mailgun.net"
		)

	def test_test_connection_reads_domain_state(self):
		with mock_request(
			[(200, DOMAIN_JSON), (200, {"webhooks": {"delivered": {"urls": ["https://x"]}}})]
		) as m:
			result = make_domain().test_connection()
		self.assertEqual(result["state"], "active")
		self.assertEqual(result["spam_action"], "tag")
		self.assertTrue(result["mx_valid"])
		self.assertTrue(result["dkim_valid"])
		self.assertTrue(result["spf_valid"])
		self.assertEqual(result["webhooks"], {"delivered": ["https://x"]})
		self.assertEqual(m.calls[0][:2], ("GET", "/v4/domains/mg.example.test"))

	def test_register_webhooks_adds_this_site_without_removing_others(self):
		with mock_request(
			[(200, {"webhooks": {"delivered": {"urls": ["https://other"]}}})] + [(200, {})] * 8
		) as m:
			urls = make_domain().register_webhooks()
		site_url = frappe.utils.get_url("/api/method/frappe_mailgun.events.webhook")
		self.assertEqual(urls["delivered"], ["https://other", site_url])
		puts = [c for c in m.calls if c[0] == "PUT"]
		posts = [c for c in m.calls if c[0] == "POST"]
		self.assertEqual(len(puts), 1)  # delivered existed
		self.assertEqual(len(posts), 7)  # the other seven are created

	def test_send_mime_request_shape(self):
		with mock_request([(200, {"id": "<x@mg>", "message": "Queued"})]) as m:
			MailgunClient(make_domain()).send_mime("a@b.test", b"raw", {"o:tag": "frappe"})
		method, path, kw = m.calls[0]
		self.assertEqual((method, path), ("POST", "/v3/mg.example.test/messages.mime"))
		self.assertEqual(kw["data"]["to"], "a@b.test")
		self.assertEqual(kw["files"]["message"][1], b"raw")

	def test_non_2xx_raises_mailgun_error(self):
		with mock_request([(400, {"message": "'to' parameter is not a valid address"})]):
			with self.assertRaises(MailgunError) as cm:
				MailgunClient(make_domain()).send_mime("bad", b"raw", {})
		self.assertEqual(cm.exception.status, 400)
		self.assertIn("not a valid address", cm.exception.text)

	def test_list_events_follows_paging(self):
		page1 = {
			"items": [{"id": "1"}],
			"paging": {"next": "https://api.eu.mailgun.net/v3/mg.example.test/events/next"},
		}
		page2 = {
			"items": [],
			"paging": {"next": "https://api.eu.mailgun.net/v3/mg.example.test/events/end"},
		}
		with mock_request([(200, page1), (200, page2)]):
			ids = [e["id"] for e in MailgunClient(make_domain()).list_events({"begin": 0})]
		self.assertEqual(ids, ["1"])
