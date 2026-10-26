# Frappe Mailgun

A Frappe 16 app that makes Mailgun a first-class email backend for Frappe and ERPNext over Mailgun's
HTTP API. No SMTP, no IMAP.

## What it does

- Adds the service `Mailgun` to Email Account. The account sends through Mailgun's messages API,
  receives through Mailgun's routes, or both.
- Several addresses on one Mailgun domain, each with its own Append To, Reply-To and flags.
- Incoming mail goes through Frappe's own inbound parser, so it threads and creates Issues, Leads
  and Communications as IMAP mail would.
- Delivery, open, click, bounce, complaint and unsubscribe events update the statuses Frappe already
  keeps. Nothing in Frappe core is patched.

The design is in [docs/superpowers/specs](docs/superpowers/specs/2026-10-26-frappe-mailgun-design.md).

## Install

On a bench:

1. `bench get-app https://github.com/alexheidl/frappe_mailgun`
2. `bench --site <site> install-app frappe_mailgun`

On Frappe Cloud:

1. Bench, Apps, Add App, From GitHub: `alexheidl/frappe_mailgun`.
2. Deploy the bench.
3. Site, Apps, install Frappe Mailgun.

Installing creates the `Mailgun` service option and three custom fields on Email Account (Mailgun
Domain, Mailgun Route, Mailgun Last Received At). Every migrate repeats it.

Two apps cannot share the `override_email_send` hook:

- Only one app on a site may own it. This app owns it, and sends mail of non-Mailgun Email Accounts
  through SMTP as core does.
- Frappe Cloud's Email Delivery Service must not be installed on the same site.

## On the Mailgun side

1. A sending domain, for example `mg.example.com`, with its sending DNS records valid (SPF and
   DKIM). For receiving, its MX records must be valid too.
2. The domain's spam filter set to "Mark spam with MIME headers", so mail arrives with its verdict
   headers. Test connection reports the current setting as `spam_action`.
3. An API key with the routes, messages and events scopes.
4. The HTTP webhook signing key, from Settings, API Security.
5. Note the region of the account, EU or US.

## Set up in Frappe

1. Create a Mailgun Domain (the name is the domain):
   - Domain: `mg.example.com`
   - Region: EU or US
   - API Key
   - Webhook Signing Key
2. Save, then press Test connection. It shows the domain's state, `spam_action`, whether the MX,
   DKIM and SPF records are valid, and the webhook URLs registered.
3. Press Register webhooks. It adds this site's events URL to the accepted, delivered, opened,
   clicked, unsubscribed, complained, permanent failure and temporary failure webhooks, keeping any URL already there. Pressing it
   again changes nothing.
4. Create an Email Account for each address with the service Mailgun:
   - Pick the Mailgun Domain. The address must be on that domain.
   - Tick Enable Outgoing, Enable Incoming, or both.
   - Set Append To, Reply-To Addresses and the other options as for any account.
   - Saving with incoming enabled creates a Mailgun route for the address. The route id shows in
     Mailgun Route.
   - Saving checks nothing against SMTP or IMAP: those fields are hidden.
   - A Mailgun account always sends as its own address, with the user's name as sender name.

One site per Mailgun domain is the supported setup:

- Routes are per address and the first matching `stop()` route wins.
- A second site on the same domain must use different addresses.
- Events carry the site they came from, and a site ignores other sites' events. The events
  endpoint answers 200 for a signed event it ignores, so Mailgun does not retry it.

## A worked setup

Domain `mg.example.com`, mail for `example.com` read in Google Workspace.

1. `support@mg.example.com`:
   - Enable Outgoing and Enable Incoming on.
   - Append To: Issue.
   - Enable Auto Reply off.
2. `hello@mg.example.com`:
   - Enable Outgoing and Enable Incoming on.
   - Create lead from incoming email on (needs Frappe CRM).
   - In the Reply-To Addresses table, add `hello@example.com`, and turn on Add Reply-To header.
3. In Google Workspace, add a routing rule that forwards `hello@example.com` to
   `hello@mg.example.com`.

Customers see mail from `hello@mg.example.com`, their reply goes to `hello@example.com`, Google
forwards it to the Mailgun address, Mailgun's route stores it and notifies the site, and the site
turns it into a Lead or a Communication. Mail forwarded this way fails SPF by design and is stored
anyway: the app records Mailgun's verdicts and does not act on them.

## What you see

- Communication: the delivery status moves as Mailgun reports it, from Sending to Sent, Delayed,
  Bounced, Rejected, Opened, Clicked, Marked As Spam or Recipient Unsubscribed. A status is replaced only by
  one of equal or higher rank, so statuses never move down. The ranks, lowest first:
  - Sending
  - Sent or Delayed
  - Error, Bounced or Rejected
  - Opened
  - Clicked
  - Marked As Spam or Recipient Unsubscribed
- Email Queue: status per recipient. A Sent queue moves to Error only when every recipient failed.
  One bounced CC leaves the queue Sent, with the error on that recipient's row. A queue still
  sending, or already in Error, is left alone.
- Unsubscribe events create an Email Unsubscribe for the mail's reference document only, never a
  global one. A mail with no reference document, or whose reference document was deleted, gets no
  Email Unsubscribe, and the Communication status still changes.
- Mailgun Event list: every event received, kept 90 days.
- Mailgun Inbound Message list: every fetched message with SPF, DKIM and spam score, kept 7 days.
- Error Log titles to search for:
  - `Mailgun inbound: no account`
  - `Mailgun inbound: invalid message URL`
  - `Mailgun inbound: fetch failed`
  - `Mailgun inbound: event failed` (the inbound sweep skipped one event and went on)
  - `Mailgun events: sweep failed`
  - `Mailgun events: event failed` (the events sweep undid one event and went on)
- A failed send stays in the Email Queue with Mailgun's message as its error, and core retries it.

## Scheduled jobs

- Hourly, `frappe_mailgun.events.sweep`: reads each Mailgun Domain's events from Last Events At and
  applies those the webhook missed (1 day back on the first run).
- Email pull, per Mailgun account with incoming on: the inbound sweep reads `stored` events since
  Mailgun Last Received At (3 days back on the first run) and fetches messages not yet seen.
  Mailgun keeps stored messages for 3 days.
- Daily, `frappe_mailgun.retention.run`: deletes webhook tokens older than 1 day, inbound records
  older than 7 days and events older than 90 days.

## Troubleshooting

- 401 on the notify endpoint (`/api/method/frappe_mailgun.inbound.notify`): the Webhook Signing Key
  on the Mailgun Domain does not match Mailgun's, or the signature is missing. Its other answers:
  - 404 for a recipient domain that is not a Mailgun Domain.
  - 417 for a `message-url` that is not a stored message of the domain.
  - A replayed token is ignored.
- 401 on the events endpoint (`/api/method/frappe_mailgun.events.webhook`): no Mailgun Domain's
  Webhook Signing Key verifies the signature, or the signature block is missing or malformed. Check
  the Webhook Signing Key first. Its other answers:
  - 200 for every signed event, including one for a queue that is gone (stored without links) and
    another site's event (ignored, nothing stored).
  - A replayed token is ignored.
- Error Log "Mailgun inbound: no account": the route still exists for an address that has no
  enabled incoming Mailgun account. Delete the route in Mailgun, or re-enable incoming on the account.
- Mail does not arrive and no route changes show: untick Enable Incoming, save, tick it, save. The
  route is deleted and created again.
- Route description reads `Frappe Mailgun: <site url> <account>`: that is how to tell whose route
  it is in Mailgun.
- Mail missed a webhook (site down, a deploy): the sweeps recover it within the windows above.
  A failed fetch logs `Mailgun inbound: fetch failed`, then:
  - Mailgun answered 404 or 410: the message is gone, so it is recorded without a Communication
    and not tried again.
  - Any other error or a timeout: nothing is recorded and the inbound sweep tries again on the
    next pull, while Mailgun still stores the message (3 days).
- Statuses not moving: press Register webhooks, check the registered URLs with Test connection, and
  check that the site is reachable from the internet.
- Changing a Mailgun account's domain: disable incoming first, so its route is removed from the
  old domain.

## Security notes

- The webhook signing key is account-wide, and Mailgun's signature covers only the timestamp and
  the token, not the body. Anyone who can read one of the account's webhook requests can replay
  its signed triple with a body of their own for 15 minutes. A site accepts each token once, so a
  replay lands only on a site the original request has not reached.
- The app pins every storage URL to Mailgun's hosts, and the Mailgun client refuses any absolute
  URL that is not on `mailgun.net`, so the API key never travels elsewhere.
- The app never acts on an event body before a signing key verifies its signature.
- Keep the signing key out of logs and other services where possible.

## Development

Tests run on a bench with Frappe 16 and Python 3.14: `bench --site <site> run-tests --app
frappe_mailgun`, with `allow_tests` set. CI does this on every push, see
[.github/workflows/ci.yml](.github/workflows/ci.yml).

License: MIT.
