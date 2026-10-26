from frappe.model.document import Document


class MailgunWebhookToken(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		seen_at: DF.Datetime | None
		token: DF.Data
	# end: auto-generated types
