const MAILGUN_HIDDEN_FIELDS = [
	"smtp_server",
	"smtp_port",
	"use_tls",
	"use_ssl_for_outgoing",
	"email_server",
	"use_imap",
	"use_ssl",
	"use_starttls",
	"password",
	"awaiting_password",
	"login_id_is_different",
	"login_id",
	"auth_method",
	"no_smtp_authentication",
	"imap_folder",
	"email_sync_option",
	"initial_sync_count",
];

function toggle_mailgun_fields(frm) {
	frm.toggle_display(MAILGUN_HIDDEN_FIELDS, frm.doc.service !== "Mailgun");
}

frappe.ui.form.on("Email Account", {
	refresh: toggle_mailgun_fields,
	service: toggle_mailgun_fields,
});
