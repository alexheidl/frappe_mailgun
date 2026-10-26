from frappe.model.document import Document


class MailgunInboundMessage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		communication: DF.Link | None
		dkim: DF.Data | None
		email_account: DF.Link | None
		message_id: DF.Data | None
		received_at: DF.Datetime | None
		recipient: DF.Data | None
		spam_score: DF.Data | None
		spf: DF.Data | None
		storage_key: DF.Data
	# end: auto-generated types
