import hashlib
import hmac
import time

import frappe

MAX_AGE_SECONDS = 900


def verify_signature(
	signing_key: str, timestamp: str, token: str, signature: str, now: int | None = None
) -> bool:
	"""Check Mailgun's webhook HMAC, rejecting missing, stale or mismatched requests."""
	if not (signing_key and timestamp and token and signature):
		return False
	try:
		sent_at = int(timestamp)
	except ValueError:
		return False
	if (now or time.time()) - sent_at > MAX_AGE_SECONDS:
		return False
	expected = hmac.new(
		signing_key.encode(), (timestamp + token).encode(), hashlib.sha256
	).hexdigest()
	return hmac.compare_digest(expected.encode(), signature.encode())


def consume_token(token: str) -> bool:
	"""Record `token` as seen; false when it was already seen (a replay)."""
	try:
		frappe.get_doc(
			{
				"doctype": "Mailgun Webhook Token",
				"token": token,
				"seen_at": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return False
	return True
