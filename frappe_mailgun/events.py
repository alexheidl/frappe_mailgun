from datetime import UTC, datetime, timedelta

import frappe
from frappe import _
from frappe.utils import convert_utc_to_system_timezone, now_datetime

from frappe_mailgun.client import MailgunClient
from frappe_mailgun.signature import consume_token, verify_signature

SWEEP_LOOKBACK_DAYS = 1

STATUS_RANK = {
	"Sending": 1,
	"Sent": 2,
	"Delayed": 2,
	"Error": 3,
	"Bounced": 3,
	"Rejected": 3,
	"Opened": 4,
	"Clicked": 5,
	"Marked As Spam": 6,
	"Recipient Unsubscribed": 6,
}


def _domain_of(address) -> str:
	"""The part after `@`, without angle brackets, lowercased."""
	return (address or "").strip().strip("<>").rpartition("@")[2].lower()


def _message_id(event_data: dict) -> str:
	headers = (event_data.get("message") or {}).get("headers") or {}
	return headers.get("message-id") or ""


def _resolve_domain(event_data: dict) -> str | None:
	"""The Mailgun Domain to verify against: the message-id's domain, else the site variable's.

	Core builds message ids from the site host, so when neither names a Mailgun Domain the
	queue's Email Account decides. The signature is checked afterwards, so nothing is trusted here.
	"""
	user_variables = event_data.get("user-variables") or {}
	for candidate in (_domain_of(_message_id(event_data)), _domain_of(user_variables.get("site"))):
		if candidate and frappe.db.exists("Mailgun Domain", candidate):
			return candidate
	queue = _find_queue(event_data)
	if queue:
		account = frappe.db.get_value("Email Queue", queue, "email_account")
		return (account and frappe.db.get_value("Email Account", account, "mailgun_domain")) or None
	return None


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
	"""Receive one Mailgun event: verify its signature, drop replays, apply it inline.

	The JSON body is read from the request; without a request (tests) the form dict stands in for it.
	"""
	body = frappe.request.get_json(silent=True) if frappe.request else None
	if not isinstance(body, dict):
		body = frappe.form_dict
	signature = body.get("signature") or {}
	event_data = body.get("event-data") or {}
	if not isinstance(signature, dict) or not isinstance(event_data, dict):
		frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

	domain_name = _resolve_domain(event_data)
	if not domain_name:
		frappe.throw(_("Unknown domain"), frappe.DoesNotExistError)

	signing_key = frappe.get_doc("Mailgun Domain", domain_name).get_password("webhook_signing_key")
	if not verify_signature(
		signing_key,
		signature.get("timestamp"),
		signature.get("token"),
		signature.get("signature"),
	):
		frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

	if not consume_token(signature.get("token")):
		return {"status": "duplicate"}

	apply_event(event_data)
	return {"status": "ok"}


def _to_system_datetime(timestamp):
	if timestamp is None:
		return None
	return convert_utc_to_system_timezone(datetime.fromtimestamp(float(timestamp), UTC)).replace(tzinfo=None)


def _find_queue(event_data: dict) -> str | None:
	user_variables = event_data.get("user-variables") or {}
	name = user_variables.get("email_queue")
	if name and frappe.db.exists("Email Queue", name):
		return name
	message_id = _message_id(event_data)
	if message_id:
		for candidate in {message_id, message_id.strip("<>"), f"<{message_id.strip('<>')}>"}:
			found = frappe.db.get_value("Email Queue", {"message_id": candidate})
			if found:
				return found
	return None


def apply_event(event_data: dict) -> str | None:
	"""Store `event_data` and apply its status moves; None when it is a repeat or another site's."""
	user_variables = event_data.get("user-variables") or {}
	site = user_variables.get("site")
	if site and site != frappe.local.site:
		return None
	event_id = event_data.get("id")
	if not event_id or frappe.db.exists("Mailgun Event", event_id):
		return None

	queue = frappe.get_doc("Email Queue", name) if (name := _find_queue(event_data)) else None
	event = event_data.get("event")
	recipient = (event_data.get("recipient") or "").lower()
	doc = frappe.get_doc(
		{
			"doctype": "Mailgun Event",
			"event_id": event_id,
			"event": event,
			"recipient": recipient,
			"message_id": _message_id(event_data),
			"email_queue": queue.name if queue else None,
			"communication": queue.communication if queue else None,
			"timestamp": _to_system_datetime(event_data.get("timestamp")),
			"severity": event_data.get("severity"),
			"reason": event_data.get("reason"),
			"data": frappe.as_json(event_data),
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return None

	if queue:
		_apply_status(queue, event, recipient, event_data)
	return doc.name


def _row(queue, recipient: str):
	for row in queue.recipients:
		if (row.recipient or "").lower() == recipient:
			return row
	return None


def _apply_status(queue, event: str, recipient: str, event_data: dict) -> None:
	communication = queue.communication
	row = _row(queue, recipient)

	if event == "delivered":
		if row:
			row.db_set("status", "Sent", update_modified=False)
		queue.reload()
		all_sent = all(r.status == "Sent" for r in queue.recipients)
		# update_db, not update_status: that would make core rewrite the Communication unguarded.
		queue.update_db(status="Sent" if all_sent else "Partially Sent", commit=False)
		if communication:
			_core_delivery_status(communication)
	elif event == "failed":
		if event_data.get("severity") == "temporary":
			set_delivery_status(communication, "Delayed")
			return
		if event_data.get("severity") != "permanent":
			return
		reason = event_data.get("reason")
		text = ((event_data.get("delivery-status") or {}).get("message")) or reason or _("Delivery failed")
		if row:
			row.db_set("error", text, update_modified=False)
		queue.reload()
		all_failed = all(r.error for r in queue.recipients)
		if all_failed:
			queue.update_db(status="Error", error=text, commit=False)
			set_delivery_status(communication, "Bounced" if reason == "bounce" else "Rejected")
		else:
			queue.update_db(status="Partially Sent", commit=False)
	elif event == "opened":
		set_delivery_status(communication, "Opened")
	elif event == "clicked":
		set_delivery_status(communication, "Clicked")
	elif event == "complained":
		set_delivery_status(communication, "Marked As Spam")
	elif event == "unsubscribed":
		set_delivery_status(communication, "Recipient Unsubscribed")
		if queue.reference_doctype and queue.reference_name:
			frappe.get_doc(
				{
					"doctype": "Email Unsubscribe",
					"email": recipient,
					"reference_doctype": queue.reference_doctype,
					"reference_name": queue.reference_name,
					"global_unsubscribe": 0,
				}
			).insert(ignore_permissions=True, ignore_if_duplicate=True)


def _rank(status) -> int:
	return STATUS_RANK.get(status or "", 0)


def _core_delivery_status(communication_name: str) -> None:
	"""Let core compute the status from the queues, unless the Communication is already past Sent."""
	if _rank(frappe.db.get_value("Communication", communication_name, "delivery_status")) >= _rank("Sent"):
		return
	frappe.get_doc("Communication", communication_name).set_delivery_status(commit=False)


def set_delivery_status(communication_name: str | None, status: str) -> None:
	"""Write `status` to the Communication only when its rank is at least the current one."""
	if not communication_name:
		return
	current = frappe.db.get_value("Communication", communication_name, "delivery_status")
	if _rank(status) >= _rank(current):
		frappe.db.set_value("Communication", communication_name, "delivery_status", status, update_modified=False)


def sweep() -> None:
	"""Hourly catch-up of events the webhook missed, from each domain's cursor."""
	for name in frappe.get_all("Mailgun Domain", pluck="name"):
		domain = frappe.get_doc("Mailgun Domain", name)
		cursor = domain.last_events_at
		begin = frappe.utils.get_datetime(cursor) if cursor else now_datetime() - timedelta(days=SWEEP_LOOKBACK_DAYS)
		params = {"begin": int(begin.timestamp()), "ascending": "yes"}
		latest = None
		for item in MailgunClient(domain).list_events(params):
			if item.get("timestamp") is not None:
				latest = max(latest or 0, item["timestamp"])
			if item.get("event") != "stored":
				apply_event(item)
		if latest is not None:
			domain.db_set("last_events_at", _to_system_datetime(latest), update_modified=False)
