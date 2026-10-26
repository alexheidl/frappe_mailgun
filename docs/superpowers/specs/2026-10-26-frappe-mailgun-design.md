# Frappe Mailgun: design

> A Frappe app that makes Mailgun a first-class email backend for Frappe and ERPNext over Mailgun's
> HTTP API. No SMTP, no IMAP. Its own repository, open source, for the Frappe ecosystem.

## Intent

Alex runs ERPNext (Frappe 16, ERPNext 16, Frappe CRM) on Frappe Cloud for Alex's Sauce, with mail on
Mailgun's EU region under `mg.alexssauce.com`. The ERP should send and receive its mail through
Mailgun's API, because:

- SMTP and IMAP throw away what Mailgun offers: delivery events, opens, clicks, bounces, complaints,
  logs, mailing lists, templates, tags.
- Frappe's own API-based paths do not fit: the "Frappe Mail" service on Email Account talks to
  Frappe's mail server (`frappe/mail`, hosted at frappemail.com), and Frappe Cloud's Email Delivery
  Service sends through Frappe's own Mailgun account with no configuration and no receiving.
- The long-term goal is marketing from the ERP, on the level of HubSpot and the like, with Mailgun as
  the sending and measuring engine. That is future work; this design lays the plumbing it will stand
  on and must not get in its way.

## Goals

1. Any Email Account can be a Mailgun account: it sends through Mailgun's messages API, receives
   through Mailgun's routes, or both, with its own Mailgun domain.
2. Several addresses on one domain, each routed by Frappe's existing Email Account settings:
   `support@` appends to Issue, `hello@` creates CRM Leads, each with its own Reply-To and default
   flags.
3. Incoming mail becomes exactly what IMAP would have produced: a Communication, threaded onto the
   right document, through Frappe's own inbound parser.
4. Mailgun's delivery events update what Frappe already tracks: Email Queue status, Communication
   delivery status, Email Unsubscribe.
5. Nothing in Frappe core is patched. The app uses Frappe's published hooks only.
6. Installable on any Frappe 16 site from the repo, with a README a stranger can follow.

## Non-goals (this design)

- Campaigns, mailing lists, templates, tags beyond what events need, analytics pages. These are the
  next projects and get their own designs.
- Sending as the plain domain (`alexssauce.com`). Alex keeps the `mg.` subdomain for sending so a
  burnt subdomain can be rotated without touching the main domain. The app does not care which
  domain an account uses.
- Frappe 15 support. Target 16; the hooks used exist in 15 but are untested there.
- OAuth, IMAP folders, POP3: a Mailgun account has none of these.

## Anti-goals

- A fork of Email Account or a replacement doctype. Users keep their Email Accounts and their
  Append To, auto-reply, signature, notification settings; the app only changes how mail moves.
- Trusting a webhook body. Inbound mail and events are verified by Mailgun's signature and the mail
  itself is fetched from Mailgun's storage by the site.
- Silent loss. A failed send stays an Email Queue error with Mailgun's message; a failed fetch is
  retried and logged.

## Constraints

- Frappe 16. The send override is the `override_email_send` hook (`frappe/email/doctype/email_queue/email_queue.py`),
  signature `method(email_queue_doc, sender, recipient, message)`; it replaces the SMTP branch for
  every account on the site, so the app must itself handle accounts that are not Mailgun accounts.
- Receiving has no hook. The app extends Email Account through `override_doctype_class` and routes
  by `service == "Mailgun"`, the way core routes `service == "Frappe Mail"`.
- Mailgun's API: EU base `https://api.eu.mailgun.net`, US base `https://api.mailgun.net`; sending
  by `POST /v3/<domain>/messages.mime`; routes by `/v3/routes`; stored messages by
  `GET /v3/domains/<domain>/messages/<storage key>` (kept 3 days); events by
  `GET /v3/<domain>/events`; webhooks signed with HMAC-SHA256 over `timestamp + token` using the
  domain's webhook signing key.
- Frappe Cloud hosts the site: the site is reachable from the internet, so webhooks work; the app
  has no shell access to the host and must do everything through Frappe.
- Repo conventions: Python, `bench` app layout, MIT, tests in Frappe's test runner, docs as lists.
- Alex's site runs on his private Frappe Cloud bench "Alex's Sauce" (Navi Mumbai, Frappe 16), so
  the app is added to the bench from GitHub, deployed, and installed on the site from the
  dashboard. The repository must be public or authorised for Frappe Cloud's GitHub app.
- Frappe Cloud's Email Delivery Service app is on that bench but not installed on the site (the
  site's apps are frappe, erpnext, crm, payments, webshop, india_compliance), so no second
  `override_email_send` competes with this one; hooks come only from apps installed on the site. It
  stays uninstalled, and can be removed from the bench after this app is in. The README warns that
  only one app may own that hook on a site.

## The design

### Settings: one record per Mailgun domain

A doctype **Mailgun Domain** (name = the domain):

- `domain` (Data, the Mailgun sending domain, e.g. `mg.alexssauce.com`)
- `region` (Select: EU, US)
- `api_key` (Password: the account or a domain sending key with routes and events permissions)
- `webhook_signing_key` (Password: Settings → Webhooks → HTTP webhook signing key)
- `last_events_at` (Datetime: the events sweep's cursor)
- a **Test connection** button: `GET /v4/domains/<domain>` and report state, MX and sending DNS.

Each Email Account references one Mailgun Domain. The key is stored once per domain.

### Email Account: the "Mailgun" service

- A Property Setter adds `Mailgun` to Email Account's `service` options.
- Custom fields on Email Account, shown when `service == "Mailgun"`:
  - `mailgun_domain` (Link to Mailgun Domain, required)
  - `mailgun_route_id` (Data, read-only: the route the app created for incoming)
  - `mailgun_last_received_at` (Datetime: the inbound sweep's cursor)
- `override_doctype_class` → `frappe_mailgun.overrides.email_account.MailgunEmailAccount`
  (subclass of `EmailAccount`):
  - `validate()`: for a Mailgun account, require `mailgun_domain`, check the address's domain
    equals the Mailgun Domain's domain, set `use_imap = 0`, skip SMTP and IMAP validation
    entirely, call the domain's test connection once (skipped in tests and patches), then run the
    parent's remaining validations (Append To, Reply-To addresses, notifications). For other
    services behave exactly as core.
  - `on_update()`: for a Mailgun account with incoming enabled, create or update the Mailgun route
    `match_recipient("<email_id>")` → `store(notify="<inbound endpoint>")`, `stop()`, priority 0,
    and save its id; with incoming disabled or the account disabled, delete the route. Route
    description carries the site URL so the dashboard shows who owns it.
  - `receive()`: for a Mailgun account, the sweep (below); otherwise the parent's.
  - `get_smtp_server()`: for a Mailgun account, return a stub with the `SMTPServer` surface core
    touches (`session` never connects, `quit()` is a no-op). Core calls this before the send hook
    on every send and `SMTPServer()` itself throws on an empty host, so it cannot raise.
  - `validate()` also forces `always_use_account_email_id_as_sender = 1`, as core does for Frappe
    Mail: the plain domain is not signed, so a desk user's own address as From would fail DMARC.
    The user's name still appears as the sender name, and Reply-To carries the plain address.
- Reply-To: Frappe 16's own `add_reply_to_header` and `reply_to_addresses` on Email Account. The
  app adds nothing; the README documents the cloak (send as `hello@mg.alexssauce.com`, Reply-To
  `hello@alexssauce.com`, a Google Workspace routing rule forwarding the plain address to the
  `mg.` one).

### Sending

`override_email_send = ["frappe_mailgun.send.send"]`, `send(email_queue, sender, recipient, message)`:

1. Resolve the queue's outgoing account (`email_queue.get_email_account()`).
2. **Not a Mailgun account:** do what core's SMTP branch does (prepare the message, `sendmail`
   through `email_account.get_smtp_server().session`), so SMTP accounts on the same site keep
   working. This is the one place the app repeats core code; it is small and it is tested.
3. **Mailgun account:** `POST /v3/<domain>/messages.mime` with `to=<recipient>` only (core calls
   the hook once per recipient of the queue, so every To, CC and BCC address gets its one copy),
   `message=<the raw MIME>`, `o:tag` = `frappe` and the queue's `reference_doctype` (lower-cased,
   spaces to hyphens, max 3 tags), `v:site` = the site name, `v:email_queue` = the queue name,
   `v:communication` = the queue's communication, `o:tracking-opens` and `o:tracking-clicks` as the
   Email Account's `track_email_status`, `h:Reply-To` only when core did not already put one in the
   MIME. Mailgun's own `Message-Id` is left to the MIME (Frappe sets it, and matches events by it).
4. On HTTP 200 the queue is left for `SendMailContext` to mark Sent. On any other status raise with
   Mailgun's message, so the queue records the error and retries as core does.

Recipients, CC and BCC: one Email Queue holds all of them as Email Queue Recipient rows and the
MIME headers stay as core built them; the envelope is one address per hook call.

The hook runs in tests too (core's `frappe.in_test` guard is on the SMTP branch, not the hook), so
`send` returns at once under `frappe.in_test` unless `frappe.flags.testing_email` is set, as core
does.

### Receiving: notify, fetch, parse

Inbound endpoint: `POST /api/method/frappe_mailgun.inbound.notify` (guest, no session):

1. Verify Mailgun's signature with the signing key of the domain named in the posted `recipient`'s
   domain; refuse with 401 otherwise. Refuse replays: keep `token` for 24 hours in a **Mailgun
   Webhook Token** doctype with a TTL and reject a seen token.
2. Read `storage` → `key` from the notification. Fetch the raw MIME from Mailgun
   (`GET /v3/domains/<domain>/messages/<key>`, `Accept: message/rfc2822`) with the domain's key.
3. Find the Email Account by `recipient` (the `To` the route matched), and hand the MIME to
   Frappe's inbound parser the way `receive()` does: `InboundMail(raw, account, uid=<storage key>,
   seen_status=1, append_to=account.append_to).process()`. Frappe then threads by `In-Reply-To`
   and `References`, creates the Issue or the Communication, triggers Frappe CRM's lead creation
   when the account has `create_lead_from_incoming_email`, sends the auto-reply, and so on.
4. Record the storage key in a **Mailgun Inbound Message** doctype (account, storage key,
   message id, received at, the Communication it became, 7-day TTL), so the sweep does not process
   it twice. Communication's `uid` is an Int and cannot hold it.
5. Return 200 quickly: the fetch and parse run in a background job; the endpoint enqueues and
   returns. Mailgun retries a non-2xx for 8 hours; the token store makes retries harmless.

Why notify-and-fetch rather than `forward()`: the site never parses a body an unauthenticated
caller could have posted, the payload is the raw message Frappe already knows how to read,
attachments arrive intact, and a missed webhook can be recovered from storage.

The sweep (`receive()` for Mailgun accounts, run by Frappe's existing email pull scheduler):

- `GET /v3/<domain>/events?event=stored&recipient=<email_id>&begin=<cursor>&ascending=yes`,
  fetch and process every storage key not yet seen, advance `mailgun_last_received_at`.
- Catches webhooks that never arrived (site down, a deploy) within Mailgun's 3-day storage window.

Sender authentication: Mailgun's stored message carries its own verdicts. The app records
`X-Mailgun-Spf`, `X-Mailgun-Dkim-Check-Result` and the spam score as a note on the Communication
(in `email_status` style fields or a comment) and does nothing else with them. The ERP is a
mailbox: it stores what came in, like IMAP did. Mail forwarded from Google Workspace fails SPF by
design and is still stored. Mailgun's spam filter ("Mark spam with MIME headers") must be on for the
domain so the headers exist; the Test connection button says when it is off.

### Events: Mailgun → Frappe's statuses

Events endpoint: `POST /api/method/frappe_mailgun.events.webhook` (guest), signature-checked and
replay-protected like inbound. The app registers Mailgun's webhooks for the domain (`/v3/domains/
<domain>/webhooks`) from the Mailgun Domain record (a **Register webhooks** button, idempotent).

Each event names a `recipient`, finds its Email Queue by `user-variables.email_queue` (or by
`message.headers.message-id` when the variables are missing), and is ignored, with a 200, when
`user-variables.site` is another site's: a Mailgun domain may be shared by sites and each keeps to
its own mail. Statuses move per recipient first, then roll up:

- the Email Queue Recipient row for that address: Sent on `delivered`, Error on a permanent
  `failed`;
- the Email Queue: Sent when every row is Sent, Error only when every row is Error (so one bounced
  CC does not trigger the failed-mail notification or a retry to the people who got it), otherwise
  Partially Sent;
- the Communication: delivery status from the queue as core computes it, plus the states core cannot
  know (Opened, Clicked, Marked As Spam, Recipient Unsubscribed) set directly.

The map:

| Mailgun event   | Email Queue status | Communication delivery status |
| --------------- | ------------------ | ----------------------------- |
| `accepted`      | Sending            | Sending                       |
| `delivered`     | Sent               | Sent                          |
| `failed` (temporary) | (unchanged)   | Delayed                       |
| `failed` (permanent) | Error, with the reason | Bounced or Rejected per `reason` |
| `opened`        |                    | Opened                        |
| `clicked`       |                    | Clicked                       |
| `complained`    |                    | Marked As Spam                |
| `unsubscribed`  |                    | Recipient Unsubscribed, and an Email Unsubscribe for the recipient against the queue's reference document (never global: an unsubscribe from a newsletter must not stop invoices) |

`complained` sets the delivery status only. Mailgun's own suppression list already blocks further
sends to that address from the domain, and the marketing work decides what a complaint means for
lists.

Statuses only move forward (an `opened` after a `clicked` does not downgrade). The raw
event is kept in a **Mailgun Event** doctype (Mailgun's event id as the name, event, recipient,
message id, timestamp, the JSON) with a 90-day TTL, which the future marketing work reads. A
scheduled sweep (hourly) reads `GET /v3/<domain>/events?begin=<last_events_at>&ascending=yes`,
processes any event id not yet stored, and advances the cursor, so a missed webhook is not a lost
status.

Webhook registration adds this site's URL to each event's webhook (Mailgun allows three URLs per
event) and never removes another site's; the Test connection button lists the URLs registered. The
README says one site per domain is the supported setup and that a second site on the same domain
must use different addresses, because routes are per address and the first `stop()` route wins.

### What the user sees

- Mailgun Domain list and form, with Test connection and Register webhooks.
- Email Account form: choose service Mailgun, pick the domain, tick incoming or outgoing, choose
  Append To and Reply-To as always. Saving creates the route; the form shows the route id.
- Communication timeline: delivery status moves as Mailgun reports it, as it does for SMTP sites
  with DSN today.
- Error Log entries for refused webhooks, failed fetches and Mailgun errors, with Mailgun's text.

### Repository

- Name: `frappe_mailgun` (app name), repo `alexheidl/frappe_mailgun` unless Alex names it otherwise.
- Layout: a standard `bench new-app` layout; `frappe_mailgun/` with `hooks.py`, `overrides/`,
  `send.py`, `inbound.py`, `events.py`, `client.py` (one thin Mailgun HTTP client), doctypes under
  `frappe_mailgun/doctype/`, fixtures for the Property Setter and custom fields, `tests/`.
- Tests: Frappe's `bench run-tests --app frappe_mailgun`, with Mailgun's HTTP mocked at the
  client boundary (`responses`), covering: send maps a queue to the right request; a non-Mailgun
  account still goes to SMTP; the signature check and replay store; inbound fetch and parse create
  a Communication threaded onto an Issue; each event moves the statuses as the table says; the route
  is created, updated and deleted with the account.
- README: install, the Mailgun side (domain, keys, spam filter, webhooks), one worked setup for
  `support@` to Issue and `hello@` to CRM Lead with the Reply-To cloak, troubleshooting.
- License MIT. CI: GitHub Actions running the tests on a fresh bench with Frappe 16.

## Decisions Alex made

- Full API integration, send and receive, no SMTP.
- Own repository, open source for the Frappe ecosystem.
- Several addresses per domain, each assigned in Frappe: `support@` for tickets, `hello@` for the
  Frappe CRM app's leads.
- A Reply-To per address, to cloak the `mg.` subdomain behind the plain domain, with Google
  Workspace forwarding the plain address.
- Keep sending on the subdomain; do not sign the plain domain, so a burnt subdomain can be rotated.
- Campaigns later, on a separate subdomain, as their own project.

## Calls I made

- Settings per domain (Mailgun Domain doctype) referenced by the Email Account, rather than keys on
  each account.
- A Mailgun account always sends as its own address (as core forces for Frappe Mail), because the
  plain domain is not signed. A desk user's mail shows their name, the account's address, and the
  Reply-To.
- One recipient per Mailgun request; statuses per recipient, rolled up so one bounced CC is not a
  failed mail.
- An `unsubscribed` event unsubscribes the address from the mail's reference document only, never
  globally; a `complained` event changes the delivery status only.
- An hourly events sweep and an inbound sweep back up the webhooks; events and inbound messages are
  deduplicated by Mailgun's ids in their own doctypes.
- A shared domain across sites is tolerated (other sites' events are ignored, webhook URLs are added
  not replaced) but documented as unsupported.
- Extend Email Account with a service option plus `override_doctype_class`, rather than a new
  doctype, so every existing Email Account feature keeps working.
- Inbound by `store(notify=...)` and fetch, not `forward()`.
- The app manages Mailgun routes and webhooks itself.
- Events are stored raw for 90 days for the marketing work to come.
- Sender authentication verdicts are recorded, not enforced; the ERP behaves like a mailbox.
- Tags on outbound mail: `frappe` plus the reference doctype.

## Left to the builder

- The exact fields and TTLs on the Webhook Token and Mailgun Event doctypes.
- How the background job for an inbound notification is named and retried.
- The shape of the Mailgun client (one module, `requests`, timeouts, error text passthrough).
- The README's wording and the CI workflow.
