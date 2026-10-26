import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

MAILGUN_ONLY = 'eval:doc.service=="Mailgun"'

CUSTOM_FIELDS = {
	"Email Account": [
		{
			"fieldname": "mailgun_domain",
			"fieldtype": "Link",
			"options": "Mailgun Domain",
			"label": "Mailgun Domain",
			"insert_after": "service",
			"depends_on": MAILGUN_ONLY,
			"mandatory_depends_on": MAILGUN_ONLY,
		},
		{
			"fieldname": "mailgun_route_id",
			"fieldtype": "Data",
			"label": "Mailgun Route",
			"read_only": 1,
			"insert_after": "mailgun_domain",
			"depends_on": MAILGUN_ONLY,
		},
		{
			"fieldname": "mailgun_last_received_at",
			"fieldtype": "Datetime",
			"label": "Mailgun Last Received At",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "mailgun_route_id",
		},
	]
}


def setup() -> None:
	"""Create the Email Account custom fields and the Mailgun service option. Idempotent."""
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	_add_service_option()


def _add_service_option() -> None:
	options = frappe.get_meta("Email Account").get_field("service").options or ""
	parts = options.split("\n")
	if "Mailgun" in parts:
		return
	# Keep the leading blank option, put Mailgun first among the real ones.
	index = 1 if parts and parts[0] == "" else 0
	parts.insert(index, "Mailgun")
	# Skip whole-doctype validation: it would reject the mailgun_domain Link while Mailgun Domain
	# does not exist yet (fresh install), and a new select option needs no such check.
	make_property_setter(
		"Email Account",
		"service",
		"options",
		"\n".join(parts),
		"Text",
		validate_fields_for_doctype=False,
	)
