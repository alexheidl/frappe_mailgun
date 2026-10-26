import frappe
import requests
from frappe import _

from frappe_mailgun.client import MailgunClient, MailgunError


def send(email_queue, sender: str, recipient: str, message: str | bytes) -> None:
	"""`override_email_send` hook: core calls it once per recipient of an Email Queue."""
	if frappe.in_test and not frappe.flags.testing_email:
		return

	account = email_queue.get_email_account(raise_error=True)
	mime = message.encode("utf-8") if isinstance(message, str) else message

	if account.service == "Frappe Mail":
		account.get_frappe_mail_client().send_raw(
			sender=sender,
			recipients=recipient,
			message=message,
			is_newsletter=email_queue.reference_doctype == "Newsletter",
		)
		return

	if not account.is_mailgun:
		_send_smtp(email_queue, account, sender, recipient, mime)
		return

	tags = ["frappe"]
	if email_queue.reference_doctype:
		tags.append(email_queue.reference_doctype.lower().replace(" ", "-"))
	tracking = "yes" if account.track_email_status else "no"
	options = {
		"o:tag": tags,
		"v:site": frappe.local.site,
		"v:email_queue": email_queue.name,
		"v:communication": email_queue.communication or "",
		"o:tracking-opens": tracking,
		"o:tracking-clicks": tracking,
	}
	client = MailgunClient(frappe.get_cached_doc("Mailgun Domain", account.mailgun_domain))
	try:
		client.send_mime(recipient, mime, options)
	except MailgunError as e:
		raise frappe.OutgoingEmailError(_("Mailgun refused the message: {0}").format(e.text)) from e
	except requests.RequestException as e:
		raise frappe.OutgoingEmailError(_("Mailgun could not be reached: {0}").format(e)) from e


def _send_smtp(email_queue, account, sender: str, recipient: str, mime: bytes) -> None:
	"""Send as core's EmailQueue.send does for SMTP accounts, which this hook replaces.

	Copies core's validate_and_prepare_message (the SIZE check) and get_smtp_options (DSN).
	"""
	session = account.get_smtp_server().session

	if session.has_extn("SIZE"):
		max_size = int(session.esmtp_features.get("size") or 0)
		if max_size > 0 and len(mime) > max_size:
			frappe.throw(
				_("Email size {0:.2f} MB exceeds the maximum allowed size of {1:.2f} MB").format(
					len(mime) / (1024 * 1024), max_size / (1024 * 1024)
				)
			)

	mail_options: list[str] = []
	rcpt_options: list[str] = []
	if session.has_extn("DSN") and (dsn_notify_type := account.dsn_notify_type):
		mail_options.extend(["RET=FULL", f"ENVID={email_queue.name}"])
		rcpt_options.append(f"NOTIFY={dsn_notify_type}")

	session.sendmail(
		from_addr=sender,
		to_addrs=recipient,
		msg=mime,
		mail_options=mail_options,
		rcpt_options=rcpt_options,
	)
