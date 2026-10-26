import frappe
from frappe.model.document import Document

from frappe_mailgun.client import MailgunClient

WEBHOOK_EVENTS = (
	"accepted",
	"delivered",
	"opened",
	"clicked",
	"unsubscribed",
	"complained",
	"permanent_fail",
	"temporary_fail",
)
EVENTS_METHOD = "frappe_mailgun.events.webhook"


class MailgunDomain(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password
		domain: DF.Data
		last_events_at: DF.Datetime | None
		region: DF.Literal["EU", "US"]
		webhook_signing_key: DF.Password | None
		webhook_urls: DF.SmallText | None
	# end: auto-generated types

	@frappe.whitelist()
	def test_connection(self) -> dict:
		client = MailgunClient(self)
		info = client.get_domain()
		webhooks = client.get_webhooks()
		self._store_webhook_urls(webhooks)

		sending = info.get("sending_dns_records") or []
		receiving = info.get("receiving_dns_records") or []
		txt = [r for r in sending if r.get("record_type") == "TXT"]
		mx = [r for r in receiving if r.get("record_type") == "MX"]
		return {
			"state": (info.get("domain") or {}).get("state"),
			"spam_action": (info.get("domain") or {}).get("spam_action"),
			"mx_valid": bool(mx) and all(r.get("valid") == "valid" for r in mx),
			"dkim_valid": any(r.get("valid") == "valid" for r in txt if "_domainkey" in r.get("name", "")),
			"spf_valid": any(
				r.get("valid") == "valid" for r in txt if (r.get("value") or "").startswith("v=spf1")
			),
			"webhooks": webhooks,
		}

	@frappe.whitelist()
	def register_webhooks(self) -> dict:
		client = MailgunClient(self)
		site_url = frappe.utils.get_url(f"/api/method/{EVENTS_METHOD}")
		existing = client.get_webhooks()
		result = {}
		for event_id in WEBHOOK_EVENTS:
			urls = list(existing.get(event_id, []))
			if site_url not in urls:
				urls.append(site_url)
			client.put_webhook(event_id, urls)
			result[event_id] = urls
		self._store_webhook_urls(result)
		return result

	def _store_webhook_urls(self, webhooks: dict[str, list[str]]) -> None:
		urls = dict.fromkeys(url for event_urls in webhooks.values() for url in event_urls)
		self.db_set("webhook_urls", "\n".join(urls))
