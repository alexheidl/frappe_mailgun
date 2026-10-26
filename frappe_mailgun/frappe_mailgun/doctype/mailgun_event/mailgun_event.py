from frappe.model.document import Document


class MailgunEvent(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		communication: DF.Link | None
		data: DF.JSON | None
		email_queue: DF.Link | None
		event: DF.Data | None
		event_id: DF.Data
		message_id: DF.Data | None
		reason: DF.Data | None
		recipient: DF.Data | None
		severity: DF.Data | None
		timestamp: DF.Datetime | None
	# end: auto-generated types
