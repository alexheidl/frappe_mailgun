import frappe
from frappe import _
from frappe.email.doctype.email_account.email_account import EmailAccount, get_append_to
from frappe.utils import comma_or, validate_email_address

from frappe_mailgun.client import MailgunClient


class NullSMTPServer:
	"""Stands in for core's SMTPServer: Mailgun sends over HTTP, so there is no session."""

	session = None

	def quit(self):
		pass

	def is_session_active(self) -> bool:
		return False


class MailgunEmailAccount(EmailAccount):
	@property
	def is_mailgun(self) -> bool:
		return self.service == "Mailgun"

	def validate(self):
		if not self.is_mailgun:
			return super().validate()

		# The parent's validate() is skipped: it demands a password and sets awaiting_password,
		# which would exclude the account from pull().
		validate_email_address(self.email_id, True)
		self.validate_domain()
		self.validate_domain_unchanged()
		self.use_imap = 0
		self.always_use_account_email_id_as_sender = 1
		self.awaiting_password = 0
		self.password = None
		self.no_smtp_authentication = 1
		self.auth_method = "Basic"
		self.login_id = None
		self.validate_append_to()
		self.validate_notify_if_unreplied()
		if self.enable_outgoing:
			self.validate_reply_to_addresses()

	def validate_domain(self):
		if not self.mailgun_domain:
			frappe.throw(_("{0} is mandatory").format(self.meta.get_label("mailgun_domain")))
		if self.email_id.rpartition("@")[2].lower() != self.mailgun_domain.lower():
			frappe.throw(
				_("The address {0} is not on the Mailgun domain {1}").format(
					self.email_id, self.mailgun_domain
				)
			)

	def validate_domain_unchanged(self):
		before = self.get_doc_before_save()
		if self.mailgun_route_id and before and before.mailgun_domain != self.mailgun_domain:
			frappe.throw(
				_("Disable incoming mail before changing the Mailgun domain of {0}").format(self.name)
			)

	def validate_append_to(self):
		if not self.append_to:
			return
		valid_doctypes = {d[0] for d in get_append_to()}
		if self.append_to not in valid_doctypes:
			frappe.throw(_("Append To can be one of {0}").format(comma_or(valid_doctypes)))

	def validate_notify_if_unreplied(self):
		if not self.notify_if_unreplied:
			return
		if not self.send_notification_to:
			frappe.throw(_("{0} is mandatory").format(self.meta.get_label("send_notification_to")))
		for address in self.get_unreplied_notification_emails():
			validate_email_address(address, True)

	def on_update(self):
		super().on_update()
		# Last, so that only the Mailgun call can fail after the parent's work.
		if self.is_mailgun:
			self.sync_route()

	def sync_route(self):
		client = MailgunClient(frappe.get_doc("Mailgun Domain", self.mailgun_domain))
		if not self.enable_incoming:
			if self.mailgun_route_id:
				client.delete_route(self.mailgun_route_id)
				self.db_set("mailgun_route_id", None)
			return

		expression = f'match_recipient("{self.email_id}")'
		notify_url = frappe.utils.get_url("/api/method/frappe_mailgun.inbound.notify")
		actions = [f'store(notify="{notify_url}")', "stop()"]
		description = f"Frappe Mailgun: {frappe.utils.get_url()} {self.name}"
		if self.mailgun_route_id:
			client.update_route(self.mailgun_route_id, expression, actions, description, 0)
		else:
			self.db_set("mailgun_route_id", client.create_route(expression, actions, description, 0))

	def on_trash(self):
		if self.is_mailgun and self.mailgun_route_id:
			client = MailgunClient(frappe.get_doc("Mailgun Domain", self.mailgun_domain))
			client.delete_route(self.mailgun_route_id)
		super().on_trash()

	def get_smtp_server(self):
		if self.is_mailgun:
			return NullSMTPServer()
		return super().get_smtp_server()

	def get_inbound_mails(self):
		if self.is_mailgun:
			# Task 6 replaces this with frappe_mailgun.inbound.get_inbound_mails(self).
			return []
		return super().get_inbound_mails()
