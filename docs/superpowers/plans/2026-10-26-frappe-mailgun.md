# Frappe Mailgun Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Frappe 16 app, `frappe_mailgun`, that sends an Email Account's mail through Mailgun's messages API, receives it through Mailgun's routes, and turns Mailgun's events into Frappe's delivery statuses, with no patch to core.

**Architecture:** The app hooks `override_email_send` for sending and `override_doctype_class` on Email Account for validation, routes and receiving; a thin `MailgunClient` is the only place HTTP happens, so tests mock one method. Two guest endpoints (inbound notify, events webhook) verify Mailgun's signature, refuse replays through a token store, and enqueue the work. Scheduled sweeps over Mailgun's events API back up both webhooks.

**Tech Stack:** Python 3.14, Frappe 16 (`version-16`), `requests` (already a Frappe dependency), Frappe's test runner (`IntegrationTestCase`), MariaDB, Redis, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-26-frappe-mailgun-design.md`

## Global Constraints

- Frappe 16 only; Python `>=3.14,<3.15` (Frappe's own requirement). No new Python dependencies.
- No patch to Frappe core. Only `override_email_send`, `override_doctype_class`, `doctype_js`, `after_install`, `after_migrate`, `scheduler_events`.
- Mailgun base URL by region: EU `https://api.eu.mailgun.net`, US `https://api.mailgun.net`. Auth is HTTP basic `api:<key>`.
- Signature: hex HMAC-SHA256 with the domain's webhook signing key over `timestamp + token`, compared with `hmac.compare_digest`; a timestamp older than 15 minutes is refused.
- Inbound mail is never parsed from a POST body: the site fetches the raw MIME from Mailgun's storage with its own key.
- A Mailgun account sends only as its own address (`always_use_account_email_id_as_sender = 1`), one Mailgun request per recipient.
- Delivery statuses move forward only. An `unsubscribed` event is never a global unsubscribe.
- All user-facing strings go through `frappe._()`. Docs are lists, not semicolon-joined sentences.
- Commits: one per task, message in the form `<area>: <what>`, with the trailers the session requires.
- Tests mock HTTP by patching `frappe_mailgun.client.MailgunClient._request`; nothing in the suite talks to Mailgun.

## Review Focus

1. A signed inbound notification for an address with no Mailgun Email Account (a route left behind): the endpoint answers 200 and logs it, so Mailgun stops retrying. Pinned in Task 6.
2. The same stored message arriving twice (webhook, then sweep): one Communication. Pinned in Task 6.
3. Mailgun refusing a send with 400 (a bad recipient): the Email Queue shows Mailgun's text as its error and core's retry limit applies; no exception escapes the hook. Pinned in Task 5.
4. An event whose Email Queue is gone (deleted after a redaction, or another site's): the event is stored and nothing raises. Pinned in Task 7.
5. A `delivered` event arriving after `opened` (out of order): the Communication stays Opened. Pinned in Task 7.

---

## File structure

```
frappe_mailgun/
  pyproject.toml                       flit-core build, name frappe_mailgun, Frappe app metadata
  frappe_mailgun/
    __init__.py                        __version__ = "0.1.0"
    hooks.py                           the hooks listed in Global Constraints
    modules.txt                        Frappe Mailgun
    patches.txt                        empty
    install.py                         after_install / after_migrate: custom fields + property setter
    client.py                          MailgunClient: every HTTP call to Mailgun
    signature.py                       verify_signature(), consume_token()
    send.py                            the override_email_send hook
    inbound.py                         notify endpoint, fetch, sweep, MailgunInboundMailSource
    events.py                          webhook endpoint, apply_event(), hourly sweep
    retention.py                       daily cleanup of the three stores
    overrides/__init__.py
    overrides/email_account.py         MailgunEmailAccount, NullSMTPServer
    public/js/email_account.js         hides SMTP and IMAP fields for the Mailgun service
    frappe_mailgun/                    the module "Frappe Mailgun"
      doctype/mailgun_domain/          mailgun_domain.{json,py,js}, test_mailgun_domain.py
      doctype/mailgun_webhook_token/   mailgun_webhook_token.{json,py}
      doctype/mailgun_inbound_message/ mailgun_inbound_message.{json,py}
      doctype/mailgun_event/           mailgun_event.{json,py}
    tests/
      __init__.py
      fixtures.py                      make_domain(), make_account(), signed() helpers
      test_install.py
      test_signature.py
      test_email_account.py
      test_send.py
      test_inbound.py
      test_events.py
      test_retention.py
  .github/workflows/ci.yml
  README.md
```

Frappe's test runner collects `test_*.py` under each doctype folder and under `frappe_mailgun/tests/`. `bench --site <site> run-tests --app frappe_mailgun` runs all of them; `--module frappe_mailgun.tests.test_send` runs one file.

---

### Task 0: A local bench (Alex's part first)

**Files:** none in the repo.

The machine has Python 3.14.4, Node 24 and `uv`, but no MariaDB, Redis or bench. The sudo steps are Alex's; the executor cannot run them.

- [ ] **Step 1 (Alex): system packages**

```bash
sudo apt update && sudo apt install -y mariadb-server mariadb-client redis-server libmariadb-dev pkg-config build-essential
sudo mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('bench'); FLUSH PRIVILEGES;"
sudo tee /etc/mysql/mariadb.conf.d/60-frappe.cnf >/dev/null <<'EOF'
[mysqld]
character-set-client-handshake = FALSE
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci

[mysql]
default-character-set = utf8mb4
EOF
sudo service mariadb restart && sudo service redis-server start
```

- [ ] **Step 2: install bench and init a Frappe 16 bench**

```bash
uv tool install frappe-bench
cd ~ && bench init --frappe-branch version-16 --python python3.14 frappe-bench
```

Expected: `~/frappe-bench/apps/frappe` on `version-16`, `bench --version` prints a version.

- [ ] **Step 3: a site, and this app from the checkout**

```bash
cd ~/frappe-bench
bench new-site mailgun.localhost --db-root-password bench --admin-password admin
bench --site mailgun.localhost set-config developer_mode 1
bench --site mailgun.localhost set-config allow_tests true
bench get-app /home/alex/frappe_mailgun
```

`bench get-app` on a local path symlinks nothing: it copies. Replace the copy with a symlink so edits land in the repo:

```bash
rm -rf apps/frappe_mailgun && ln -s /home/alex/frappe_mailgun apps/frappe_mailgun
```

(Task 1 creates the app files `get-app` needs; run Step 3 after Task 1's first commit if the repo has no `pyproject.toml` yet.)

- [ ] **Step 4: verify**

Run: `cd ~/frappe-bench && bench --site mailgun.localhost install-app frappe_mailgun && bench --site mailgun.localhost run-tests --app frappe_mailgun`
Expected: the install succeeds and the run reports `Ran 0 tests` or the tests of whatever task is done.

---

### Task 1: App skeleton, custom fields, property setter

**Files:**
- Create: `pyproject.toml`, `frappe_mailgun/__init__.py`, `frappe_mailgun/hooks.py`, `frappe_mailgun/modules.txt`, `frappe_mailgun/patches.txt`, `frappe_mailgun/install.py`, `frappe_mailgun/public/js/email_account.js`, `frappe_mailgun/frappe_mailgun/__init__.py`, `frappe_mailgun/tests/__init__.py`
- Test: `frappe_mailgun/tests/test_install.py`

**Interfaces:**
- Produces: `frappe_mailgun.install.setup() -> None` (idempotent: creates the custom fields and the property setter, safe to run on every migrate). Custom fields on Email Account: `mailgun_domain` (Link to Mailgun Domain, label "Mailgun Domain", `insert_after` `service`, `depends_on` and `mandatory_depends_on` both `eval:doc.service=="Mailgun"`), `mailgun_route_id` (Data, read only, label "Mailgun Route", `insert_after` `mailgun_domain`, same `depends_on`), `mailgun_last_received_at` (Datetime, read only, hidden, `insert_after` `mailgun_route_id`). Property setter: Email Account `service` options with `Mailgun` as the first non-empty option, keeping every existing option.
- `hooks.py` carries: `app_name = "frappe_mailgun"`, `app_title = "Frappe Mailgun"`, `app_license = "MIT"`, `required_apps = ["frappe"]`, `after_install = "frappe_mailgun.install.setup"`, `after_migrate = ["frappe_mailgun.install.setup"]`, `doctype_js = {"Email Account": "public/js/email_account.js"}`. The other hooks are added by the tasks that need them.

- [ ] **Step 1: Write the failing test**

```python
# frappe_mailgun/tests/test_install.py
import frappe
from frappe.tests import IntegrationTestCase


class TestInstall(IntegrationTestCase):
    def test_service_options_include_mailgun(self):
        options = frappe.get_meta("Email Account").get_field("service").options.split("\n")
        self.assertIn("Mailgun", options)
        self.assertIn("Frappe Mail", options)
        self.assertIn("GMail", options)

    def test_custom_fields_exist(self):
        meta = frappe.get_meta("Email Account")
        self.assertEqual(meta.get_field("mailgun_domain").fieldtype, "Link")
        self.assertEqual(meta.get_field("mailgun_domain").options, "Mailgun Domain")
        self.assertEqual(meta.get_field("mailgun_route_id").fieldtype, "Data")
        self.assertEqual(meta.get_field("mailgun_last_received_at").fieldtype, "Datetime")

    def test_setup_is_idempotent(self):
        from frappe_mailgun.install import setup
        setup()
        setup()
        self.assertEqual(
            frappe.db.count("Custom Field", {"dt": "Email Account", "fieldname": "mailgun_domain"}), 1
        )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~/frappe-bench && bench --site mailgun.localhost install-app frappe_mailgun; bench --site mailgun.localhost run-tests --app frappe_mailgun --module frappe_mailgun.tests.test_install`
Expected: the install fails or the tests fail on the missing field or option.

- [ ] **Step 3: Write the app files**

`pyproject.toml`: flit-core backend, `name = "frappe_mailgun"`, `requires-python = ">=3.14"`, `dependencies = []`, `[tool.bench.dev-dependencies]` empty, the `[project]` description from the README's first sentence.

`install.py`: `setup()` calls `frappe.custom.doctype.custom_field.custom_field.create_custom_fields({"Email Account": [...]}, ignore_validate=True)` and `frappe.custom.doctype.property_setter.property_setter.make_property_setter("Email Account", "service", "options", <options>, "Text")`, where `<options>` is the current options with `Mailgun` inserted after the leading blank line when absent.

`public/js/email_account.js`: on `refresh` and on `service` change, `frm.toggle_display([...SMTP and IMAP fields...], frm.doc.service !== "Mailgun")` for `smtp_server, smtp_port, use_tls, use_ssl_for_outgoing, email_server, use_imap, use_ssl, use_starttls, password, awaiting_password, login_id_is_different, login_id, auth_method, no_smtp_authentication, imap_folder, email_sync_option, initial_sync_count`.

Note: `test_custom_fields_exist` needs the `Mailgun Domain` doctype to exist as the Link target only at runtime; the custom field is created with the option string regardless. Task 2 creates the doctype.

- [ ] **Step 4: Run the tests**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun --module frappe_mailgun.tests.test_install`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "app: skeleton, Email Account custom fields and the Mailgun service option"
```

---

### Task 2: Mailgun Domain doctype and the client

**Files:**
- Create: `frappe_mailgun/client.py`, `frappe_mailgun/frappe_mailgun/doctype/mailgun_domain/mailgun_domain.json`, `.py`, `.js`, `frappe_mailgun/tests/fixtures.py`
- Test: `frappe_mailgun/frappe_mailgun/doctype/mailgun_domain/test_mailgun_domain.py`

**Interfaces:**
- Produces doctype **Mailgun Domain**: `autoname = "field:domain"`; fields `domain` (Data, required, unique), `region` (Select `EU\nUS`, default EU), `api_key` (Password, required), `webhook_signing_key` (Password), `last_events_at` (Datetime, read only), `webhook_urls` (Small Text, read only). Whitelisted doc methods `test_connection() -> dict` (keys `state`, `spam_action`, `mx_valid: bool`, `dkim_valid: bool`, `spf_valid: bool`, `webhooks: dict[str, list[str]]`) and `register_webhooks() -> dict[str, list[str]]`.
- Produces `MailgunClient(domain: "MailgunDomain")` in `client.py` with:
  - `base_url: str` by region
  - `_request(method: str, path: str, *, params=None, data=None, files=None, headers=None, timeout=(10, 60)) -> requests.Response` (the only call to `requests`; raises `MailgunError(status, text)` on non-2xx, except where a method says otherwise)
  - `get_domain() -> dict` (`GET /v4/domains/<domain>`)
  - `send_mime(to: str, mime: bytes, options: dict[str, str]) -> dict` (`POST /v3/<domain>/messages.mime`, `files={"message": ("message.mime", mime)}`, `data={"to": to, **options}`)
  - `get_stored_message(url: str) -> bytes` (`GET url` with `Accept: message/rfc2822`, returns the JSON's `body-mime` encoded utf-8)
  - `list_events(params: dict) -> Iterator[dict]` (`GET /v3/<domain>/events`, follows `paging.next` while `items` is non-empty)
  - `create_route(expression: str, actions: list[str], description: str, priority: int = 0) -> str` (returns the route id), `update_route(route_id, expression, actions, description, priority) -> None`, `delete_route(route_id) -> None` (a 404 on delete is not an error)
  - `get_webhooks() -> dict[str, list[str]]` (`GET /v3/domains/<domain>/webhooks` → `{event_id: urls}`), `put_webhook(event_id: str, urls: list[str]) -> None` (`PUT /v3/domains/<domain>/webhooks/<id>` when it exists, else `POST /v3/domains/<domain>/webhooks` with `id` and `url`)
  - `class MailgunError(Exception)` with `status: int` and `text: str`.
- `fixtures.py`: `make_domain(name="mg.example.test", region="EU") -> Document` (keys `key-test`, `sign-test`, inserted if missing), `mock_request(responses: list[tuple[int, dict | bytes]])` context manager that patches `MailgunClient._request` to return fake `requests.Response` objects in order and records calls on `.calls` as `(method, path, kwargs)`.

- [ ] **Step 1: Write the failing tests**

```python
# test_mailgun_domain.py
class TestMailgunDomain(IntegrationTestCase):
    def test_base_url_by_region(self):
        self.assertEqual(MailgunClient(make_domain(region="EU")).base_url, "https://api.eu.mailgun.net")
        self.assertEqual(MailgunClient(make_domain("us.example.test", region="US")).base_url, "https://api.mailgun.net")

    def test_test_connection_reads_domain_state(self):
        with mock_request([(200, DOMAIN_JSON), (200, {"webhooks": {"delivered": {"urls": ["https://x"]}}})]) as m:
            result = make_domain().test_connection()
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["spam_action"], "tag")
        self.assertTrue(result["mx_valid"])
        self.assertEqual(result["webhooks"], {"delivered": ["https://x"]})
        self.assertEqual(m.calls[0][:2], ("GET", "/v4/domains/mg.example.test"))

    def test_register_webhooks_adds_this_site_without_removing_others(self):
        with mock_request([(200, {"webhooks": {"delivered": {"urls": ["https://other"]}}})] + [(200, {})] * 8) as m:
            urls = make_domain().register_webhooks()
        site_url = frappe.utils.get_url("/api/method/frappe_mailgun.events.webhook")
        self.assertEqual(urls["delivered"], ["https://other", site_url])
        puts = [c for c in m.calls if c[0] == "PUT"]
        posts = [c for c in m.calls if c[0] == "POST"]
        self.assertEqual(len(puts), 1)      # delivered existed
        self.assertEqual(len(posts), 7)     # the other seven are created

    def test_send_mime_request_shape(self):
        with mock_request([(200, {"id": "<x@mg>", "message": "Queued"})]) as m:
            MailgunClient(make_domain()).send_mime("a@b.test", b"raw", {"o:tag": "frappe"})
        method, path, kw = m.calls[0]
        self.assertEqual((method, path), ("POST", "/v3/mg.example.test/messages.mime"))
        self.assertEqual(kw["data"]["to"], "a@b.test")
        self.assertEqual(kw["files"]["message"][1], b"raw")

    def test_non_2xx_raises_mailgun_error(self):
        with mock_request([(400, {"message": "'to' parameter is not a valid address"})]):
            with self.assertRaises(MailgunError) as cm:
                MailgunClient(make_domain()).send_mime("bad", b"raw", {})
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("not a valid address", cm.exception.text)

    def test_list_events_follows_paging(self):
        page1 = {"items": [{"id": "1"}], "paging": {"next": "https://api.eu.mailgun.net/v3/mg.example.test/events/next"}}
        page2 = {"items": [], "paging": {"next": "https://api.eu.mailgun.net/v3/mg.example.test/events/end"}}
        with mock_request([(200, page1), (200, page2)]):
            ids = [e["id"] for e in MailgunClient(make_domain()).list_events({"begin": 0})]
        self.assertEqual(ids, ["1"])
```

The eight webhook event ids are `accepted, delivered, opened, clicked, unsubscribed, complained, permanent_fail, temporary_fail`.

- [ ] **Step 2: Run them to verify they fail**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun --module frappe_mailgun.frappe_mailgun.doctype.mailgun_domain.test_mailgun_domain`
Expected: FAIL on import of `frappe_mailgun.client`.

- [ ] **Step 3: Write the doctype, the client and the fixtures**

`mailgun_domain.js`: two custom buttons, Test connection and Register webhooks, each calling the doc method and showing the result in a `frappe.msgprint` (the dict as a definition list). `test_connection` also writes `webhook_urls` as one URL per line.

`MailgunClient._request` builds the URL as `base_url + path` unless `path` starts with `https://` (stored-message and paging URLs are absolute).

- [ ] **Step 4: Run the tests**

Run: the Step 2 command, then `bench --site mailgun.localhost migrate`
Expected: PASS, 6 tests; the migrate creates the doctype.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "domain: the Mailgun Domain doctype and the client"
```

---

### Task 3: Signature check and the replay store

**Files:**
- Create: `frappe_mailgun/signature.py`, `frappe_mailgun/frappe_mailgun/doctype/mailgun_webhook_token/mailgun_webhook_token.{json,py}`, `frappe_mailgun/retention.py`
- Modify: `frappe_mailgun/hooks.py` (`scheduler_events = {"daily": ["frappe_mailgun.retention.run"]}`)
- Test: `frappe_mailgun/tests/test_signature.py`, `frappe_mailgun/tests/test_retention.py`

**Interfaces:**
- Produces `verify_signature(signing_key: str, timestamp: str, token: str, signature: str, now: int | None = None) -> bool`: false on a missing field, a non-numeric or stale timestamp (older than 900 seconds against `now`, default `time.time()`), or a digest mismatch.
- Produces `consume_token(token: str) -> bool`: inserts **Mailgun Webhook Token** (`autoname = "field:token"`, fields `token` Data unique, `seen_at` Datetime) and returns true; returns false when the token exists (`frappe.DuplicateEntryError`).
- Produces `retention.run()`: deletes Mailgun Webhook Token older than 1 day, Mailgun Inbound Message older than 7 days, Mailgun Event older than 90 days, by `creation`. The two later doctypes are deleted only if they exist (`frappe.db.exists("DocType", ...)`), so this task's tests pass before Tasks 6 and 7.
- `fixtures.signed(signing_key: str, now: int | None = None) -> dict` returns `{"timestamp", "token", "signature"}` for tests.

- [ ] **Step 1: Write the failing tests**

```python
class TestSignature(IntegrationTestCase):
    def test_valid_signature(self):
        s = signed("sign-test")
        self.assertTrue(verify_signature("sign-test", **s))

    def test_wrong_key(self):
        self.assertFalse(verify_signature("other", **signed("sign-test")))

    def test_stale_timestamp(self):
        s = signed("sign-test", now=1_000_000)
        self.assertFalse(verify_signature("sign-test", **s, now=1_000_000 + 901))

    def test_missing_field(self):
        self.assertFalse(verify_signature("sign-test", "", "t", "s"))

    def test_token_is_consumed_once(self):
        self.assertTrue(consume_token("tok-1"))
        self.assertFalse(consume_token("tok-1"))


class TestRetention(IntegrationTestCase):
    def test_old_tokens_are_deleted(self):
        consume_token("tok-old")
        frappe.db.set_value("Mailgun Webhook Token", "tok-old", "creation", frappe.utils.add_days(frappe.utils.now_datetime(), -2))
        consume_token("tok-new")
        run()
        self.assertFalse(frappe.db.exists("Mailgun Webhook Token", "tok-old"))
        self.assertTrue(frappe.db.exists("Mailgun Webhook Token", "tok-new"))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun --module frappe_mailgun.tests.test_signature` and the same for `test_retention`
Expected: FAIL on import.

- [ ] **Step 3: Implement `signature.py`, the doctype, `retention.py`, the hook**

- [ ] **Step 4: Run the tests**

Run: `bench --site mailgun.localhost migrate && bench --site mailgun.localhost run-tests --app frappe_mailgun`
Expected: PASS, all tests so far.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "signature: Mailgun's HMAC check, the replay store and daily retention"
```

---

### Task 4: The Email Account override

**Files:**
- Create: `frappe_mailgun/overrides/__init__.py`, `frappe_mailgun/overrides/email_account.py`
- Modify: `frappe_mailgun/hooks.py` (`override_doctype_class = {"Email Account": "frappe_mailgun.overrides.email_account.MailgunEmailAccount"}`), `frappe_mailgun/tests/fixtures.py` (`make_account`)
- Test: `frappe_mailgun/tests/test_email_account.py`

**Interfaces:**
- Produces `MailgunEmailAccount(EmailAccount)` with:
  - `is_mailgun -> bool` property (`self.service == "Mailgun"`)
  - `validate()`: for Mailgun accounts the parent's `validate()` is **not** called (outside tests it would throw "Password is required" for an incoming account with no password, and `awaiting_password = 1` would exclude the account from `pull()`). Instead: `validate_email_address(self.email_id, True)`; require `mailgun_domain`; throw `frappe.ValidationError` with `_("The address {0} is not on the Mailgun domain {1}")` when the part after `@` differs from the domain (case-insensitive); set `use_imap = 0`, `always_use_account_email_id_as_sender = 1`, `awaiting_password = 0`, `password = None`, `no_smtp_authentication = 1`, `auth_method = "Basic"`, `login_id = None`; check `append_to` is among `get_append_to()`'s doctypes when set; run the parent's `notify_if_unreplied` check and `validate_reply_to_addresses()` (the two non-network validations at the end of core's `validate()`). Non-Mailgun accounts: the parent's `validate()` untouched.
  - `on_update()`: for Mailgun accounts, `sync_route()`; then the parent's `on_update()`.
  - `sync_route()`: incoming enabled and account enabled → create or update the route `match_recipient("<email_id>")` with actions `[f'store(notify="{notify_url}")', "stop()"]`, description `f"Frappe Mailgun: {frappe.utils.get_url()} {self.name}"`, priority 0, saving `mailgun_route_id` with `db_set`; otherwise, with a route id, delete it and clear the field. `notify_url = frappe.utils.get_url("/api/method/frappe_mailgun.inbound.notify")`.
  - `on_trash()`: delete the route when there is one, then the parent's.
  - `get_smtp_server()`: for Mailgun accounts return `NullSMTPServer()`; else the parent's.
  - `get_inbound_mails()`: for Mailgun accounts, `frappe_mailgun.inbound.get_inbound_mails(self)` (Task 6); else the parent's.
- Produces `NullSMTPServer` with `session = None` as an attribute, `quit()` no-op, `is_session_active() -> False`.
- `fixtures.make_account(email_id="support@mg.example.test", *, incoming=True, outgoing=True, append_to="ToDo", domain=None) -> Document` inserts an Email Account with `service = "Mailgun"`, `mailgun_domain` the fixture domain, `enable_incoming`, `enable_outgoing`, `append_to`, `email_account_name` derived, deleting an existing one of the same name first. Route calls are mocked by the caller.

- [ ] **Step 1: Write the failing tests**

```python
class TestMailgunEmailAccount(IntegrationTestCase):
    def test_class_is_overridden(self):
        self.assertIs(type(frappe.new_doc("Email Account")), MailgunEmailAccount)

    def test_requires_matching_domain(self):
        with self.assertRaises(frappe.ValidationError):
            make_account("support@other.test")

    def test_validate_forces_mailgun_settings(self):
        with mock_request([(200, {"route": {"id": "r1"}})]):
            acc = make_account()
        self.assertEqual(acc.use_imap, 0)
        self.assertEqual(acc.always_use_account_email_id_as_sender, 1)
        self.assertEqual(acc.no_smtp_authentication, 1)

    def test_saving_an_incoming_account_creates_the_route(self):
        with mock_request([(200, {"route": {"id": "r1"}})]) as m:
            acc = make_account()
        self.assertEqual(acc.mailgun_route_id, "r1")
        method, path, kw = m.calls[0]
        self.assertEqual((method, path), ("POST", "/v3/routes"))
        self.assertEqual(kw["data"]["expression"], 'match_recipient("support@mg.example.test")')
        self.assertIn(f'store(notify="{frappe.utils.get_url("/api/method/frappe_mailgun.inbound.notify")}")', kw["data"]["action"])
        self.assertIn("stop()", kw["data"]["action"])

    def test_disabling_incoming_deletes_the_route(self):
        with mock_request([(200, {"route": {"id": "r1"}}), (200, {})]) as m:
            acc = make_account()
            acc.enable_incoming = 0
            acc.save()
        self.assertEqual(m.calls[1][:2], ("DELETE", "/v3/routes/r1"))
        self.assertFalse(acc.mailgun_route_id)

    def test_outgoing_only_account_creates_no_route(self):
        with mock_request([]) as m:
            make_account(incoming=False)
        self.assertEqual(m.calls, [])

    def test_smtp_server_is_a_null_object(self):
        with mock_request([(200, {"route": {"id": "r1"}})]):
            acc = make_account()
        server = acc.get_smtp_server()
        self.assertIsNone(server.session)
        server.quit()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun --module frappe_mailgun.tests.test_email_account`
Expected: FAIL on import.

- [ ] **Step 3: Implement the override and the hook**

Until Task 6 exists, `get_inbound_mails()` for Mailgun accounts returns `[]` with a comment naming Task 6.

- [ ] **Step 4: Run the tests**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun`
Expected: PASS, all so far.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "email account: the Mailgun service, its validation and its route"
```

---

### Task 5: Sending

**Files:**
- Create: `frappe_mailgun/send.py`
- Modify: `frappe_mailgun/hooks.py` (`override_email_send = ["frappe_mailgun.send.send"]`)
- Test: `frappe_mailgun/tests/test_send.py`

**Interfaces:**
- Consumes `MailgunClient.send_mime`, `MailgunError`, `MailgunEmailAccount.is_mailgun`.
- Produces `send(email_queue, sender: str, recipient: str, message: str | bytes) -> None`:
  1. `if frappe.in_test and not frappe.flags.testing_email: return`.
  2. `account = email_queue.get_email_account(raise_error=True)`.
  3. Not Mailgun: `account.get_smtp_server().session.sendmail(from_addr=sender, to_addrs=recipient, msg=message encoded utf-8)`.
  4. Mailgun: `MailgunClient(frappe.get_cached_doc("Mailgun Domain", account.mailgun_domain)).send_mime(recipient, message encoded, options)` with options `{"o:tag": tags, "v:site": frappe.local.site, "v:email_queue": email_queue.name, "v:communication": email_queue.communication or "", "o:tracking-opens": yes/no, "o:tracking-clicks": yes/no}` where tracking follows `account.track_email_status` and tags are `["frappe", slug(email_queue.reference_doctype)]` with the slug lower-cased, spaces to hyphens, the list sent as repeated `o:tag` values (a list value in `data`).
  5. A `MailgunError` is re-raised as `frappe.OutgoingEmailError(_("Mailgun refused the message: {0}").format(error.text))`, so `SendMailContext` records it on the queue.

- [ ] **Step 1: Write the failing tests**

```python
class TestSend(IntegrationTestCase):
    def setUp(self):
        frappe.flags.testing_email = True
        with mock_request([(200, {"route": {"id": "r1"}})]):
            self.account = make_account(append_to=None)

    def tearDown(self):
        frappe.flags.testing_email = False

    def make_queue(self, recipients=("to@x.test",), reference_doctype="ToDo"):
        return frappe.get_doc({
            "doctype": "Email Queue", "sender": "support@mg.example.test", "email_account": self.account.name,
            "message": "Subject: hi\n\nbody", "reference_doctype": reference_doctype,
            "recipients": [{"recipient": r} for r in recipients],
        }).insert(ignore_permissions=True)

    def test_mailgun_account_posts_mime_per_recipient(self):
        q = self.make_queue(("a@x.test", "b@x.test"))
        with mock_request([(200, {}), (200, {})]) as m:
            send(q, q.sender, "a@x.test", q.message)
            send(q, q.sender, "b@x.test", q.message)
        self.assertEqual([c[2]["data"]["to"] for c in m.calls], ["a@x.test", "b@x.test"])
        data = m.calls[0][2]["data"]
        self.assertEqual(data["v:email_queue"], q.name)
        self.assertEqual(data["v:site"], frappe.local.site)
        self.assertEqual(data["o:tag"], ["frappe", "todo"])
        self.assertEqual(data["o:tracking-opens"], "no")

    def test_mailgun_refusal_becomes_an_outgoing_email_error(self):
        q = self.make_queue()
        with mock_request([(400, {"message": "'to' parameter is not a valid address"})]):
            with self.assertRaises(frappe.OutgoingEmailError) as cm:
                send(q, q.sender, "to@x.test", q.message)
        self.assertIn("not a valid address", str(cm.exception))

    def test_non_mailgun_account_uses_smtp(self):
        smtp_account = make_smtp_account()          # service GMail, awaiting_password=1, in fixtures
        q = self.make_queue(); q.db_set("email_account", smtp_account.name); q.reload()
        session = MagicMock()
        with patch.object(EmailAccount, "get_smtp_server", return_value=SimpleNamespace(session=session)):
            send(q, q.sender, "to@x.test", q.message)
        session.sendmail.assert_called_once()
        self.assertEqual(session.sendmail.call_args.kwargs["to_addrs"], "to@x.test")

    def test_in_test_guard_without_flag_sends_nothing(self):
        frappe.flags.testing_email = False
        q = self.make_queue()
        with mock_request([]) as m:
            send(q, q.sender, "to@x.test", q.message)
        self.assertEqual(m.calls, [])

    def test_end_to_end_through_the_queue(self):
        with mock_request([(200, {})]) as m:
            frappe.sendmail(recipients=["to@x.test"], sender="support@mg.example.test", subject="hi", message="body", now=True)
        self.assertEqual(m.calls[0][2]["data"]["to"], "to@x.test")
        q = frappe.get_last_doc("Email Queue")
        self.assertEqual(q.status, "Sent")
```

`make_smtp_account()` goes into `fixtures.py`: an Email Account with `service = "GMail"`, `email_id = "smtp@example.test"`, `awaiting_password = 1`, outgoing only.

- [ ] **Step 2: Run them to verify they fail**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun --module frappe_mailgun.tests.test_send`
Expected: FAIL on import.

- [ ] **Step 3: Implement `send.py` and the hook**

- [ ] **Step 4: Run the tests**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun`
Expected: PASS. If `test_end_to_end_through_the_queue` fails because core marks the queue before the hook, read `SendMailContext.__exit__` and adjust the assertion to the status core sets after a successful hook, with a comment.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "send: Mailgun accounts post MIME per recipient, others keep SMTP"
```

---

### Task 6: Receiving

**Files:**
- Create: `frappe_mailgun/inbound.py`, `frappe_mailgun/frappe_mailgun/doctype/mailgun_inbound_message/mailgun_inbound_message.{json,py}`
- Modify: `frappe_mailgun/overrides/email_account.py` (`get_inbound_mails` delegates), `frappe_mailgun/tests/fixtures.py` (`RAW_MAIL`, `notify_form()`)
- Test: `frappe_mailgun/tests/test_inbound.py`

**Interfaces:**
- Consumes `verify_signature`, `consume_token`, `MailgunClient.get_stored_message`, `MailgunClient.list_events`, `MailgunEmailAccount`.
- Produces doctype **Mailgun Inbound Message**: `autoname = "field:storage_key"`, fields `storage_key` (Data, unique), `email_account` (Link Email Account), `recipient` (Data), `message_id` (Data), `communication` (Link Communication), `received_at` (Datetime), `spf` (Data), `dkim` (Data), `spam_score` (Data).
- Produces in `inbound.py`:
  - `@frappe.whitelist(allow_guest=True, methods=["POST"]) notify()`: reads `frappe.form_dict` keys `recipient`, `timestamp`, `token`, `signature`, `message-url`; resolves the Mailgun Domain by the recipient's domain part (404 → `frappe.throw(..., frappe.DoesNotExistError)` mapped to HTTP 404); a failed signature → `frappe.throw(_("Invalid signature"), frappe.AuthenticationError)` (HTTP 401); a seen token → return `{"status": "duplicate"}`; otherwise `frappe.enqueue("frappe_mailgun.inbound.fetch", queue="short", domain=<name>, message_url=..., recipient=...)` and return `{"status": "queued"}`.
  - `fetch(domain: str, message_url: str, recipient: str) -> None`: finds the Email Account (`service = "Mailgun"`, `email_id = recipient` case-insensitive, `enable_incoming = 1`); none → `frappe.log_error(title="Mailgun inbound: no account", message=recipient)` and return; `storage_key = message_url.rstrip("/").rsplit("/", 1)[-1]`; seen key → return; raw = `get_stored_message(message_url)`; `account.flags.mailgun_raw_messages = [(storage_key, raw)]`; `account.receive()`.
  - `get_inbound_mails(account) -> list[InboundMail]`: from `account.flags.mailgun_raw_messages` when set, else from the sweep: `list_events({"event": "stored", "recipient": account.email_id, "begin": <cursor>, "ascending": "yes"})` where the cursor is `mailgun_last_received_at` or now minus 3 days, each unseen `storage.key` fetched by `storage.url`; afterwards `db_set("mailgun_last_received_at", <latest event timestamp>)`. Every raw message becomes `MailgunInboundMail(raw, account, uid=None, seen_status=None, append_to=account.append_to)` with `storage_key` set; the list is also kept on `account.flags.mailgun_mails`.
  - `class MailgunInboundMail(InboundMail)` with `storage_key: str` and `communication: Document | None`; its `process()` calls the parent's and keeps the returned Communication on `self.communication`.
  - `MailgunEmailAccount.receive()` wraps the parent's: after it returns, for each mail in `self.flags.mailgun_mails` (the list `get_inbound_mails` built) insert the Mailgun Inbound Message row with `communication = mail.communication.name` when set (core's loop rolls back a failed mail, so a row without a Communication means "fetched, not stored"; the sweep will not fetch it again, and the Error Log has the traceback).
  - The row also records Mailgun's verdicts from the raw headers: `spf` from `X-Mailgun-Spf`, `dkim` from `X-Mailgun-Dkim-Check-Result`, `spam_score` from `X-Mailgun-Sscore` (each Data, empty when the header is missing). The Test connection result's `spam_action` tells the operator whether those headers exist at all.

Why wrap `receive()`: core's loop does the processing, the auto-reply, the participant notification and the per-mail error handling; the app only supplies the mails and records what became of them.

- [ ] **Step 1: Write the failing tests**

```python
RAW_MAIL = b"From: Customer <c@customer.test>\r\nTo: support@mg.example.test\r\nSubject: Help\r\nMessage-ID: <m1@customer.test>\r\nDate: Sun, 26 Oct 2026 10:00:00 +0530\r\nX-Mailgun-Spf: Pass\r\nX-Mailgun-Dkim-Check-Result: Pass\r\nContent-Type: text/plain\r\n\r\nMy order is late.\r\n"

class TestInbound(IntegrationTestCase):
    def setUp(self):
        with mock_request([(200, {"route": {"id": "r1"}})]):
            self.account = make_account()          # append_to ToDo

    def test_notify_refuses_a_bad_signature(self):
        form = notify_form("wrong-key", recipient="support@mg.example.test")
        with self.assertRaises(frappe.AuthenticationError):
            call_guest(notify, form)

    def test_notify_queues_once_per_token(self):
        form = notify_form("sign-test", recipient="support@mg.example.test")
        with patch("frappe.enqueue") as enqueue:
            self.assertEqual(call_guest(notify, form)["status"], "queued")
            self.assertEqual(call_guest(notify, form)["status"], "duplicate")
        enqueue.assert_called_once()

    def test_fetch_creates_a_todo_and_a_communication(self):
        with mock_request([(200, {"body-mime": RAW_MAIL.decode()})]):
            fetch("mg.example.test", "https://sw.api.mailgun.net/v3/domains/mg.example.test/messages/KEY1", "support@mg.example.test")
        comm = frappe.get_last_doc("Communication", {"message_id": "m1@customer.test"})
        self.assertEqual(comm.reference_doctype, "ToDo")
        self.assertEqual(comm.sender, "c@customer.test")
        row = frappe.get_doc("Mailgun Inbound Message", "KEY1")
        self.assertEqual(row.communication, comm.name)
        self.assertEqual((row.spf, row.dkim, row.spam_score), ("Pass", "Pass", ""))

    def test_the_same_storage_key_is_processed_once(self):
        with mock_request([(200, {"body-mime": RAW_MAIL.decode()})]) as m:
            fetch("mg.example.test", "https://x/messages/KEY2", "support@mg.example.test")
            fetch("mg.example.test", "https://x/messages/KEY2", "support@mg.example.test")
        self.assertEqual(len(m.calls), 1)
        self.assertEqual(frappe.db.count("Communication", {"message_id": "m1@customer.test"}), 1)

    def test_fetch_for_an_unknown_recipient_logs_and_returns(self):
        with mock_request([]) as m:
            fetch("mg.example.test", "https://x/messages/KEY3", "nobody@mg.example.test")
        self.assertEqual(m.calls, [])
        self.assertTrue(frappe.db.exists("Error Log", {"method": "Mailgun inbound: no account"}))

    def test_sweep_fetches_unseen_stored_events(self):
        events = {"items": [{"id": "e1", "event": "stored", "timestamp": 1_800_000_000, "storage": {"key": "KEY4", "url": "https://x/messages/KEY4"}}], "paging": {"next": "https://x/next"}}
        with mock_request([(200, events), (200, {"items": [], "paging": {}}), (200, {"body-mime": RAW_MAIL.decode()})]):
            self.account.reload().receive()
        self.assertTrue(frappe.db.exists("Mailgun Inbound Message", "KEY4"))
        self.assertIsNotNone(frappe.db.get_value("Email Account", self.account.name, "mailgun_last_received_at"))
```

`call_guest(fn, form)` in `fixtures.py` sets `frappe.set_user("Guest")`, `frappe.local.form_dict = frappe._dict(form)`, calls `fn()`, and restores the user in a `finally`. `notify_form(key, recipient)` returns `signed(key)` plus `recipient` and `message-url`.

- [ ] **Step 2: Run them to verify they fail**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun --module frappe_mailgun.tests.test_inbound`
Expected: FAIL on import.

- [ ] **Step 3: Implement `inbound.py`, the doctype, the `receive()` wrap**

- [ ] **Step 4: Run the tests**

Run: `bench --site mailgun.localhost migrate && bench --site mailgun.localhost run-tests --app frappe_mailgun`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "inbound: notify, fetch from storage, the sweep and the inbound record"
```

---

### Task 7: Events

**Files:**
- Create: `frappe_mailgun/events.py`, `frappe_mailgun/frappe_mailgun/doctype/mailgun_event/mailgun_event.{json,py}`
- Modify: `frappe_mailgun/hooks.py` (`scheduler_events["hourly"] = ["frappe_mailgun.events.sweep"]`), `frappe_mailgun/tests/fixtures.py` (`event_data(...)`)
- Test: `frappe_mailgun/tests/test_events.py`

**Interfaces:**
- Consumes `verify_signature`, `consume_token`, `MailgunClient.list_events`.
- Produces doctype **Mailgun Event**: `autoname = "field:event_id"`, fields `event_id` (Data, unique), `event` (Data), `recipient` (Data), `message_id` (Data), `email_queue` (Link Email Queue), `communication` (Link Communication), `timestamp` (Datetime), `severity` (Data), `reason` (Data), `data` (JSON).
- Produces in `events.py`:
  - `@frappe.whitelist(allow_guest=True, methods=["POST"]) webhook()`: body JSON `{"signature": {...}, "event-data": {...}}`; the domain is `event-data.message.headers.message-id`'s domain part after `@`, else `event-data.user-variables.site`'s domain; refuse a bad signature with 401; a seen token → `{"status": "duplicate"}`; otherwise `apply_event(event_data)` inline (events are cheap) and return `{"status": "ok"}`.
  - `apply_event(event_data: dict) -> str | None`: returns the stored Mailgun Event name, or `None` when the event id exists already or `user-variables.site` names another site. Finds the Email Queue by `user-variables.email_queue`, else by `message.headers.message-id` against `Email Queue.message_id`; a missing queue stores the event with no links and returns. Then the status moves of the table below.
  - `STATUS_RANK = {"Sending": 1, "Sent": 2, "Delayed": 2, "Error": 3, "Bounced": 3, "Rejected": 3, "Opened": 4, "Clicked": 5, "Marked As Spam": 6, "Recipient Unsubscribed": 6}` and `set_delivery_status(communication_name, status)` which writes only when the new rank is at least the current one.
  - `sweep()`: for every Mailgun Domain, `list_events({"begin": cursor, "ascending": "yes"})` with the cursor `last_events_at` or now minus 1 day, `apply_event` on each item whose `event != "stored"`, then `db_set("last_events_at", <latest timestamp>)`.

Status moves per event, where `recipient` is the event's recipient and the queue row is the Email Queue Recipient with that address:

| event | queue row | queue | communication |
| --- | --- | --- | --- |
| accepted | | | |
| delivered | status Sent | unchanged (events never write Partially Sent) | from `set_delivery_status()` (core) |
| failed, severity temporary | | | Delayed |
| failed, severity permanent | error = `delivery-status.message` or `reason` | Error with that text when every row has an error and the queue is Sent, else unchanged | Bounced when `reason == "bounce"`, else Rejected, only when every row has an error |
| opened | | | Opened |
| clicked | | | Clicked |
| complained | | | Marked As Spam |
| unsubscribed | | | Recipient Unsubscribed, plus an Email Unsubscribe (`email`, `reference_doctype`, `reference_name` from the queue, `global_unsubscribe = 0`) when the queue has a reference |

- [ ] **Step 1: Write the failing tests**

```python
class TestEvents(IntegrationTestCase):
    def setUp(self):
        frappe.flags.testing_email = True
        with mock_request([(200, {"route": {"id": "r1"}})]):
            self.account = make_account(append_to=None)
        self.todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert()
        with mock_request([(200, {})]):
            frappe.sendmail(recipients=["to@x.test"], sender="support@mg.example.test", subject="hi", message="b",
                            reference_doctype="ToDo", reference_name=self.todo.name, now=True)
        self.queue = frappe.get_last_doc("Email Queue")
        self.comm = frappe.get_last_doc("Communication")

    def ev(self, event, **extra):
        return event_data(event, recipient="to@x.test", email_queue=self.queue.name, site=frappe.local.site, **extra)

    def test_delivered_marks_recipient_and_queue_sent(self):
        apply_event(self.ev("delivered"))
        self.queue.reload()
        self.assertEqual(self.queue.recipients[0].status, "Sent")
        self.assertEqual(self.queue.status, "Sent")

    def test_permanent_failure_sets_error_and_bounced(self):
        apply_event(self.ev("failed", severity="permanent", reason="bounce", message="550 no such user"))
        self.queue.reload()
        self.assertEqual(self.queue.status, "Error")
        self.assertIn("550", self.queue.recipients[0].error)
        self.assertEqual(frappe.db.get_value("Communication", self.comm.name, "delivery_status"), "Bounced")

    def test_one_bounced_cc_is_partially_sent(self):
        with mock_request([(200, {}), (200, {})]):
            frappe.sendmail(recipients=["a@x.test"], cc=["b@x.test"], sender="support@mg.example.test", subject="hi", message="b", now=True)
        q = frappe.get_last_doc("Email Queue")
        apply_event(event_data("delivered", recipient="a@x.test", email_queue=q.name, site=frappe.local.site))
        apply_event(event_data("failed", recipient="b@x.test", email_queue=q.name, site=frappe.local.site, severity="permanent", reason="bounce"))
        q.reload()
        self.assertEqual(q.status, "Partially Sent")

    def test_statuses_only_move_forward(self):
        apply_event(self.ev("opened"))
        apply_event(self.ev("delivered"))
        self.assertEqual(frappe.db.get_value("Communication", self.comm.name, "delivery_status"), "Opened")

    def test_unsubscribe_is_scoped_to_the_reference(self):
        apply_event(self.ev("unsubscribed"))
        unsub = frappe.get_last_doc("Email Unsubscribe", {"email": "to@x.test"})
        self.assertEqual((unsub.reference_doctype, unsub.reference_name, unsub.global_unsubscribe), ("ToDo", self.todo.name, 0))

    def test_another_sites_event_is_ignored(self):
        self.assertIsNone(apply_event(self.ev("delivered", site="other.localhost")))

    def test_missing_queue_is_stored_without_links(self):
        name = apply_event(event_data("delivered", recipient="x@x.test", email_queue="EQ-gone", site=frappe.local.site))
        self.assertFalse(frappe.db.get_value("Mailgun Event", name, "email_queue"))

    def test_duplicate_event_id_is_ignored(self):
        e = self.ev("delivered")
        self.assertIsNotNone(apply_event(e))
        self.assertIsNone(apply_event(e))

    def test_webhook_refuses_a_bad_signature(self):
        body = {"signature": signed("wrong"), "event-data": self.ev("delivered")}
        with self.assertRaises(frappe.AuthenticationError):
            call_guest_json(webhook, body)

    def test_sweep_applies_unseen_events_and_moves_the_cursor(self):
        items = {"items": [self.ev("delivered", id="sw1", timestamp=1_800_000_000)], "paging": {"next": "https://x/n"}}
        with mock_request([(200, items), (200, {"items": [], "paging": {}})]):
            sweep()
        self.assertTrue(frappe.db.exists("Mailgun Event", "sw1"))
        self.assertIsNotNone(frappe.db.get_value("Mailgun Domain", "mg.example.test", "last_events_at"))
```

`event_data(event, *, recipient, email_queue, site, id=None, timestamp=None, severity=None, reason=None, message=None)` in `fixtures.py` builds Mailgun's event-data shape: `id` (random when None), `event`, `timestamp`, `recipient`, `user-variables: {"site", "email_queue"}`, `message: {"headers": {"message-id": <queue.message_id>}}`, `severity`, `reason`, `delivery-status: {"message": message}`. `call_guest_json(fn, body)` sets `frappe.local.request` data to the JSON (or `frappe.form_dict` as the parsed JSON, whichever `webhook()` reads; the implementer picks one and the helper matches it).

- [ ] **Step 2: Run them to verify they fail**

Run: `bench --site mailgun.localhost run-tests --app frappe_mailgun --module frappe_mailgun.tests.test_events`
Expected: FAIL on import.

- [ ] **Step 3: Implement `events.py`, the doctype, the hourly hook**

- [ ] **Step 4: Run the tests**

Run: `bench --site mailgun.localhost migrate && bench --site mailgun.localhost run-tests --app frappe_mailgun`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "events: the webhook, the status map, the hourly sweep and the event store"
```

---

### Task 8: CI and the README

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`

- [ ] **Step 1: The workflow**

On push and pull request: `ubuntu-latest`, services `mariadb:10.11` (root password `root`, health check) and `redis:7`; `actions/setup-python` with `3.14`; `actions/setup-node` with `24`; `pip install frappe-bench`; `bench init --skip-redis-config-generation --frappe-branch version-16 frappe-bench`; `bench get-app frappe_mailgun $GITHUB_WORKSPACE`; `bench new-site test_site --db-root-password root --admin-password admin --mariadb-user-host-login-scope='%'`; `bench --site test_site install-app frappe_mailgun`; `bench --site test_site set-config allow_tests true`; `bench --site test_site run-tests --app frappe_mailgun`. Redis in CI: write `redis_cache`, `redis_queue` and `redis_socketio` into `sites/common_site_config.json` pointing at `redis://localhost:6379` before `new-site`.

- [ ] **Step 2: Push a branch and confirm the run is green**

Run: `git push -u origin HEAD && gh run watch --exit-status`
Expected: the workflow passes. Fix the workflow until it does; a red CI is not done.

- [ ] **Step 3: The README**

Sections, as lists:
- What it does, in four lines.
- Install: add the app to a bench (`bench get-app https://github.com/alexheidl/frappe_mailgun`, `bench --site <site> install-app frappe_mailgun`); on Frappe Cloud: bench → Apps → Add App from GitHub → deploy → site → install.
- Mailgun side: a domain with sending DNS valid; the spam filter set to "Mark spam with MIME headers"; an API key with routes, messages and events scopes; the HTTP webhook signing key from Settings → API Security.
- Setup in Frappe: a Mailgun Domain record, Test connection, Register webhooks; then an Email Account per address with service Mailgun. Note that only one app on a site may own the `override_email_send` hook, and that Frappe Cloud's Email Delivery Service must not be installed on the same site.
- A worked setup: `support@mg.example.com` incoming and outgoing, Append To Issue, auto-reply off; `hello@mg.example.com` incoming and outgoing, "Create lead from incoming email" on (Frappe CRM), Reply-To `hello@example.com` through the Reply-To Addresses table with "Add Reply-To header"; the Google Workspace routing rule forwarding `hello@example.com` to `hello@mg.example.com`.
- What you see: delivery status on Communications, the Mailgun Event list, Error Log titles to search for.
- Troubleshooting: 401 on notify (signing key), "no account" errors (route left behind), a stale route (disable and re-enable incoming), the sweeps and their cursors.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "ci and readme: tests on a fresh Frappe 16 bench, install and setup guide"
```

---

### Task 9: On Alex's bench (Alex's part)

**Files:** none.

- [ ] **Step 1 (Alex): add the app to the bench "Alex's Sauce"** from `https://github.com/alexheidl/frappe_mailgun`, branch `main`, deploy, and install it on `alexssauce.nvi.frappe.cloud`.
- [ ] **Step 2: a Mailgun Domain `mg.alexssauce.com`**, region EU, the account API key and the webhook signing key (the same values the shop's webhook uses, from Secret Manager), Test connection green, Register webhooks.
- [ ] **Step 3: the Email Accounts.** `support@mg.alexssauce.com`: incoming and outgoing, Append To Issue, default outgoing for the Support module. `hello@mg.alexssauce.com`: incoming and outgoing, Create lead from incoming email on, Reply-To `hello@alexssauce.com`.

  The shop's existing Mailgun route for `support@` forwards to the shop's own webhook. Saving the Email Account creates a second route for the same address, and Mailgun runs both when neither stops first: both routes get priority 0, so set the shop's route's priority to 1 and remove its `stop()` before saving the account, or decide that the ERP's route replaces the shop's. Decide with Alex before Step 3.
- [ ] **Step 4: the end-to-end check.** Send a mail from the ERP to Alex's own address through `hello@`: it arrives with From `hello@mg.alexssauce.com` and Reply-To `hello@alexssauce.com`; reply to it: the reply appears on the Communication's thread in the ERP within a minute, the Mailgun Event list shows `accepted` and `delivered`, and the Communication's delivery status reads Sent, then Opened when the mail is opened.
- [ ] **Step 5:** remove Email Delivery Service from the bench's Apps (it is not installed on the site).
