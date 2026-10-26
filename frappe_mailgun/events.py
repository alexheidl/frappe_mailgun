from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from frappe_mailgun.client import MailgunClient
from frappe_mailgun.clock import from_unix, to_unix
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


def _text(value) -> str:
	"""`value` when it is a string, else empty: a guest's JSON may put anything anywhere."""
	return value if isinstance(value, str) else ""


def _dict(value) -> dict:
	return value if isinstance(value, dict) else {}


def _user_variables(event_data: dict) -> dict:
	return _dict(event_data.get("user-variables"))


def _message_id(event_data: dict) -> str:
	headers = _dict(_dict(event_data.get("message")).get("headers"))
	return _text(headers.get("message-id"))


def _refuse():
	frappe.throw(_("Invalid signature"), frappe.AuthenticationError)


def _verified(timestamp: str, token: str, signature: str) -> bool:
	"""True when any Mailgun Domain's signing key verifies the triple.

	The key is account-wide and the domains are few, so trying each one means nothing in the
	event body reaches the database before the signature holds.
	"""
	for name in frappe.get_all("Mailgun Domain", pluck="name"):
		key = frappe.get_doc("Mailgun Domain", name).get_password("webhook_signing_key", raise_exception=False)
		if verify_signature(key, timestamp, token, signature):
			return True
	return False


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
	"""Receive one Mailgun event: verify its signature, drop replays, apply it inline.

	A verified event this site cannot match, or another site's, still gets a 200 so Mailgun stops
	retrying it. The JSON body is read from the request; without a request (tests) the form dict stands in for it.
	"""
	body = frappe.request.get_json(silent=True) if frappe.request else None
	if not isinstance(body, dict):
		body = frappe.form_dict
	signature = body.get("signature")
	event_data = body.get("event-data")
	# Every refusal is the same 401, so callers learn nothing about domains, queues or message ids.
	if not isinstance(signature, dict) or not isinstance(event_data, dict):
		_refuse()

	timestamp, token, sig = (_text(signature.get(k)) for k in ("timestamp", "token", "signature"))
	if not _verified(timestamp, token, sig):
		_refuse()

	if not consume_token(token):
		return {"status": "duplicate"}

	apply_event(event_data)
	return {"status": "ok"}


def _to_system_datetime(timestamp):
	"""The system-timezone datetime for unix seconds; None when it is not a usable number."""
	if isinstance(timestamp, bool) or not isinstance(timestamp, int | float):
		return None
	try:
		return from_unix(timestamp)
	except (ValueError, OverflowError, OSError):
		return None


def _find_queue(event_data: dict) -> str | None:
	name = _text(_user_variables(event_data).get("email_queue"))
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
	site = _text(_user_variables(event_data).get("site"))
	if site and site != frappe.local.site:
		return None
	event_id = _text(event_data.get("id"))
	if not event_id or frappe.db.exists("Mailgun Event", event_id):
		return None

	queue = frappe.get_doc("Email Queue", name) if (name := _find_queue(event_data)) else None
	event = _text(event_data.get("event"))
	recipient = _text(event_data.get("recipient")).lower()
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
			"severity": _text(event_data.get("severity")) or None,
			"reason": _text(event_data.get("reason")) or None,
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
		if not row:
			return
		row.db_set("status", "Sent", update_modified=False)
		_roll_up(queue)
		if communication:
			_core_delivery_status(communication)
	elif event == "failed":
		if event_data.get("severity") == "temporary":
			set_delivery_status(communication, "Delayed")
			return
		if event_data.get("severity") != "permanent" or not row:
			return
		reason = _text(event_data.get("reason"))
		text = _text(_dict(event_data.get("delivery-status")).get("message")) or reason or _("Delivery failed")
		row.db_set("error", text, update_modified=False)
		if _roll_up(queue, text) == "Error":
			set_delivery_status(communication, "Bounced" if reason == "bounce" else "Rejected")
	elif event == "opened":
		set_delivery_status(communication, "Opened")
	elif event == "clicked":
		set_delivery_status(communication, "Clicked")
	elif event == "complained":
		set_delivery_status(communication, "Marked As Spam")
	elif event == "unsubscribed":
		set_delivery_status(communication, "Recipient Unsubscribed")
		key = {
			"email": recipient,
			"reference_doctype": queue.reference_doctype,
			"reference_name": queue.reference_name,
		}
		# Core raises on a duplicate before ignore_if_duplicate can apply, so check first.
		if queue.reference_doctype and queue.reference_name and not frappe.db.exists("Email Unsubscribe", key):
			frappe.get_doc(
				{
					"doctype": "Email Unsubscribe",
					"email": recipient,
					"reference_doctype": queue.reference_doctype,
					"reference_name": queue.reference_name,
					"global_unsubscribe": 0,
				}
			).insert(ignore_permissions=True)


def _roll_up(queue, error_text: str | None = None) -> str:
	"""Move a Sent queue to Error when every row has an error; return the queue's status.

	Only a Sent queue is touched. Core's get_queue() retries Not Sent and Partially Sent queues, so
	events never write Partially Sent, never touch a queue core is still sending, and never move a
	queue out of Error. A mixed outcome leaves the queue Sent with the error on the row.
	update_db, not update_status: that would make core rewrite the Communication unguarded.
	"""
	queue.reload()
	if queue.status != "Sent":
		return queue.status
	if not all(r.error for r in queue.recipients):
		return "Sent"
	queue.update_db(status="Error", error=error_text, commit=False)
	return "Error"


def _rank(status) -> int:
	return STATUS_RANK.get(status or "", 0)


def _core_delivery_status(communication_name: str) -> None:
	"""Let core compute the status from the queues, unless the Communication is already ranked above Sent."""
	if _rank(frappe.db.get_value("Communication", communication_name, "delivery_status")) > _rank("Sent"):
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
	"""Hourly catch-up of events the webhook missed, from each domain's cursor; one domain's failure spares the rest."""
	for name in frappe.get_all("Mailgun Domain", pluck="name"):
		try:
			_sweep_domain(name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Mailgun events: sweep failed", message=f"{name}\n{frappe.get_traceback()}")


def _sweep_domain(name: str) -> None:
	domain = frappe.get_doc("Mailgun Domain", name)
	cursor = domain.last_events_at
	begin = get_datetime(cursor) if cursor else now_datetime() - timedelta(days=SWEEP_LOOKBACK_DAYS)
	params = {"begin": int(to_unix(begin)), "ascending": "yes"}
	latest = None
	for item in MailgunClient(domain).list_events(params):
		timestamp = item.get("timestamp")
		if isinstance(timestamp, int | float) and not isinstance(timestamp, bool):
			latest = max(latest or 0, timestamp)
		if item.get("event") != "stored":
			apply_event(item)
	if latest is not None:
		domain.db_set("last_events_at", from_unix(latest), update_modified=False)
