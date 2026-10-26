from datetime import datetime, timedelta
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.email.receive import InboundMail
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from frappe_mailgun.client import MailgunClient, MailgunError
from frappe_mailgun.signature import consume_token, verify_signature

SWEEP_LOOKBACK_DAYS = 3


def is_storage_url(url, domain_name: str) -> bool:
	"""True only for an https Mailgun URL under this domain's stored messages."""
	if not url or not isinstance(url, str):
		return False
	try:
		parsed = urlparse(url)
		host = parsed.hostname or ""
	except ValueError:
		return False
	return (
		parsed.scheme == "https"
		and (host == "mailgun.net" or host.endswith(".mailgun.net"))
		and parsed.path.startswith(f"/v3/domains/{domain_name}/messages/")
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def notify():
	"""Receive Mailgun's stored-message notification and queue the fetch of the raw message."""
	form = frappe.form_dict
	recipient = form.get("recipient") or ""
	domain_name = recipient.rpartition("@")[2].lower()
	if not domain_name or not frappe.db.exists("Mailgun Domain", domain_name):
		frappe.throw(_("Unknown domain"), frappe.DoesNotExistError)

	if not (form.get("timestamp") and form.get("token") and form.get("signature")):
		frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

	signing_key = frappe.get_doc("Mailgun Domain", domain_name).get_password("webhook_signing_key")
	if not verify_signature(
		signing_key, form.get("timestamp"), form.get("token"), form.get("signature")
	):
		frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

	if not is_storage_url(form.get("message-url"), domain_name):
		frappe.throw(_("Invalid message URL"), frappe.ValidationError)

	if not consume_token(form.get("token")):
		return {"status": "duplicate"}

	frappe.enqueue(
		"frappe_mailgun.inbound.fetch",
		queue="short",
		domain=domain_name,
		message_url=form.get("message-url"),
		recipient=recipient,
	)
	return {"status": "queued"}


def _is_seen(storage_key: str) -> bool:
	return bool(frappe.db.exists("Mailgun Inbound Message", storage_key))


def fetch(domain: str, message_url: str, recipient: str) -> None:
	"""Fetch one stored message from Mailgun and let the account's receive loop process it."""
	account_name = frappe.db.get_value(
		"Email Account",
		{"service": "Mailgun", "email_id": recipient.lower(), "enable_incoming": 1},
	)
	if not account_name:
		frappe.log_error(title="Mailgun inbound: no account", message=recipient)
		return

	if not is_storage_url(message_url, domain.lower()):
		frappe.log_error(title="Mailgun inbound: invalid message URL", message=str(message_url))
		return

	storage_key = message_url.rstrip("/").rsplit("/", 1)[-1]
	if _is_seen(storage_key):
		return

	account = frappe.get_doc("Email Account", account_name)
	raw = MailgunClient(frappe.get_doc("Mailgun Domain", domain)).get_stored_message(message_url)
	account.flags.mailgun_raw_messages = [(storage_key, raw)]
	account.receive()


class MailgunInboundMail(InboundMail):
	storage_key: str
	communication: Document | None

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.communication = None

	def process(self):
		self.communication = super().process()
		return self.communication


def _sweep_raw_messages(account) -> list[tuple[str, bytes]]:
	"""Fetch the unseen stored messages since the account's cursor, then advance the cursor."""
	cursor = account.mailgun_last_received_at
	begin = get_datetime(cursor) if cursor else now_datetime() - timedelta(days=SWEEP_LOOKBACK_DAYS)
	client = MailgunClient(frappe.get_doc("Mailgun Domain", account.mailgun_domain))
	params = {
		"event": "stored",
		"recipient": account.email_id,
		"begin": int(begin.timestamp()),
		"ascending": "yes",
	}
	# List every event first: fetching between pages would interleave requests.
	events = list(client.list_events(params))
	raw_messages = []
	latest = None
	for event in events:
		storage = event.get("storage") or {}
		timestamp = event.get("timestamp")
		if timestamp is not None:
			latest = max(latest or 0, timestamp)
		key, url = storage.get("key"), storage.get("url")
		if not key or not url or _is_seen(key) or any(key == k for k, _raw in raw_messages):
			continue
		if not is_storage_url(url, account.mailgun_domain.lower()):
			frappe.log_error(title="Mailgun inbound: invalid message URL", message=str(url))
			continue
		try:
			raw_messages.append((key, client.get_stored_message(url)))
		except (MailgunError, requests.RequestException):
			frappe.log_error(title="Mailgun inbound: fetch failed", message=f"{account.name} {key}")
			_record_failed(account, key)
	if latest is not None:
		account.db_set(
			"mailgun_last_received_at", datetime.fromtimestamp(latest), update_modified=False
		)
	return raw_messages


def _record_failed(account, storage_key: str) -> None:
	"""Record a key whose fetch failed, with no Communication, so the sweep does not retry it forever."""
	frappe.get_doc(
		{
			"doctype": "Mailgun Inbound Message",
			"storage_key": storage_key,
			"email_account": account.name,
			"recipient": account.email_id,
			"received_at": now_datetime(),
		}
	).insert(ignore_permissions=True, ignore_if_duplicate=True)


def get_inbound_mails(account) -> list[MailgunInboundMail]:
	if not account.enable_incoming:
		return []
	raw_messages = account.flags.get("mailgun_raw_messages")
	if raw_messages is None:
		raw_messages = _sweep_raw_messages(account)

	mails = []
	for storage_key, raw in raw_messages:
		mail = MailgunInboundMail(
			raw, account, uid=None, seen_status=None, append_to=account.append_to
		)
		mail.storage_key = storage_key
		mails.append(mail)
	account.flags.mailgun_mails = mails
	return mails


def record_mails(account) -> None:
	"""Insert a Mailgun Inbound Message for each mail the account's receive loop handled."""
	for mail in account.flags.get("mailgun_mails") or []:
		headers = mail.mail
		frappe.get_doc(
			{
				"doctype": "Mailgun Inbound Message",
				"storage_key": mail.storage_key,
				"email_account": account.name,
				"recipient": account.email_id,
				"message_id": mail.message_id,
				"communication": mail.communication.name if mail.communication else None,
				"received_at": now_datetime(),
				"spf": headers.get("X-Mailgun-Spf") or "",
				"dkim": headers.get("X-Mailgun-Dkim-Check-Result") or "",
				"spam_score": headers.get("X-Mailgun-Sscore") or "",
			}
		).insert(ignore_permissions=True, ignore_if_duplicate=True)
	account.flags.mailgun_mails = []
