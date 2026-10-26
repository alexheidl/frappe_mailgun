import frappe


def run() -> None:
	"""Daily cleanup of replay tokens and webhook records past their retention window."""
	_delete_older_than("Mailgun Webhook Token", 1)
	for doctype, days in (("Mailgun Inbound Message", 7), ("Mailgun Event", 90)):
		if frappe.db.exists("DocType", doctype):
			_delete_older_than(doctype, days)


def _delete_older_than(doctype: str, days: int) -> None:
	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -days)
	frappe.db.delete(doctype, {"creation": ("<", cutoff)})
