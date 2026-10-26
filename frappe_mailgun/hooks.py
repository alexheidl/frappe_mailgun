app_name = "frappe_mailgun"
app_title = "Frappe Mailgun"
app_publisher = "Alex Heidl"
app_description = "Mailgun as an Email Account service over Mailgun's HTTP API"
app_email = "alex@heidlbrothers.de"
app_license = "MIT"

required_apps = ["frappe"]

after_install = "frappe_mailgun.install.setup"
after_migrate = ["frappe_mailgun.install.setup"]

doctype_js = {"Email Account": "public/js/email_account.js"}

scheduler_events = {"daily": ["frappe_mailgun.retention.run"]}

override_doctype_class = {"Email Account": "frappe_mailgun.overrides.email_account.MailgunEmailAccount"}
