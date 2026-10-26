# Frappe Mailgun

A Frappe app that makes Mailgun a first-class email backend for Frappe and ERPNext over Mailgun's
HTTP API. No SMTP, no IMAP.

- An Email Account with the service `Mailgun` sends through Mailgun's messages API, receives through
  Mailgun's routes, or both.
- Several addresses on one Mailgun domain, each routed by Frappe's own Email Account settings.
- Delivery, open, click, bounce, complaint and unsubscribe events update the statuses Frappe already
  keeps.
- Nothing in Frappe core is patched.

The design is in [docs/superpowers/specs](docs/superpowers/specs/2026-10-26-frappe-mailgun-design.md).
The app is under construction; the README grows with it.

License: MIT.
