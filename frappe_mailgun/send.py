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

	if not account.is_mailgun:
		account.get_smtp_server().session.sendmail(from_addr=sender, to_addrs=recipient, msg=mime)
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
