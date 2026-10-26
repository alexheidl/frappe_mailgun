from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING
from urllib.parse import unquote

import requests

if TYPE_CHECKING:
	from frappe_mailgun.frappe_mailgun.doctype.mailgun_domain.mailgun_domain import MailgunDomain

BASE_URLS = {"EU": "https://api.eu.mailgun.net", "US": "https://api.mailgun.net"}

# The authority must be a bare Mailgun host: no userinfo, port or backslash before the path.
_MAILGUN_URL = re.compile(
	r"https://(?:[a-z0-9-]+\.)*mailgun\.net(?P<rest>[/?#].*)?", re.IGNORECASE | re.DOTALL
)


def is_mailgun_url(url) -> bool:
	"""True only for an https URL on mailgun.net or a subdomain, without backslashes or dot segments."""
	if not isinstance(url, str) or "\\" in url:
		return False
	match = _MAILGUN_URL.fullmatch(url)
	if not match:
		return False
	path = re.split(r"[?#]", match.group("rest") or "", maxsplit=1)[0]
	return not any(unquote(segment) in (".", "..") for segment in path.split("/"))


class MailgunError(Exception):
	def __init__(self, status: int, text: str):
		super().__init__(f"Mailgun returned {status}: {text}")
		self.status = status
		self.text = text


def _raise_for_status(response: requests.Response) -> None:
	if not 200 <= response.status_code < 300:
		raise MailgunError(response.status_code, response.text)


class MailgunClient:
	def __init__(self, domain: MailgunDomain):
		self.domain = domain.domain
		self.base_url = BASE_URLS[domain.region or "EU"]
		self._api_key = domain.get_password("api_key")
		self._webhooks: dict[str, list[str]] | None = None

	def _request(
		self,
		method: str,
		path: str,
		*,
		params=None,
		data=None,
		files=None,
		headers=None,
		timeout=(10, 60),
	) -> requests.Response:
		if path.startswith("/"):
			url = self.base_url + path
		elif is_mailgun_url(path):
			url = path
		else:
			# Defence in depth: the API key must never travel to another host.
			raise MailgunError(0, "refused: host is not Mailgun")
		response = requests.request(
			method,
			url,
			auth=("api", self._api_key),
			params=params,
			data=data,
			files=files,
			headers=headers,
			timeout=timeout,
		)
		_raise_for_status(response)
		return response

	def get_domain(self) -> dict:
		return self._request("GET", f"/v4/domains/{self.domain}").json()

	def send_mime(self, to: str, mime: bytes, options: dict[str, str]) -> dict:
		return self._request(
			"POST",
			f"/v3/{self.domain}/messages.mime",
			files={"message": ("message.mime", mime)},
			data={"to": to, **options},
		).json()

	def get_stored_message(self, url: str) -> bytes:
		response = self._request("GET", url, headers={"Accept": "message/rfc2822"})
		return response.json()["body-mime"].encode("utf-8")

	def list_events(self, params: dict) -> Iterator[dict]:
		response = self._request("GET", f"/v3/{self.domain}/events", params=params)
		while True:
			body = response.json()
			if not body.get("items"):
				return
			yield from body["items"]
			next_url = body.get("paging", {}).get("next")
			if not next_url:
				return
			response = self._request("GET", next_url)

	def create_route(self, expression: str, actions: list[str], description: str, priority: int = 0) -> str:
		response = self._request(
			"POST",
			"/v3/routes",
			data={
				"expression": expression,
				"action": actions,
				"description": description,
				"priority": priority,
			},
		)
		return response.json()["route"]["id"]

	def update_route(
		self, route_id: str, expression: str, actions: list[str], description: str, priority: int
	) -> None:
		self._request(
			"PUT",
			f"/v3/routes/{route_id}",
			data={
				"expression": expression,
				"action": actions,
				"description": description,
				"priority": priority,
			},
		)

	def delete_route(self, route_id: str) -> None:
		try:
			self._request("DELETE", f"/v3/routes/{route_id}")
		except MailgunError as e:
			if e.status != 404:
				raise

	def get_webhooks(self) -> dict[str, list[str]]:
		body = self._request("GET", f"/v3/domains/{self.domain}/webhooks").json()
		self._webhooks = {
			event_id: list(hook.get("urls") or []) for event_id, hook in body.get("webhooks", {}).items()
		}
		return dict(self._webhooks)

	def put_webhook(self, event_id: str, urls: list[str]) -> None:
		if self._webhooks is None:
			self.get_webhooks()
		if event_id in self._webhooks:
			self._request("PUT", f"/v3/domains/{self.domain}/webhooks/{event_id}", data={"url": urls})
		else:
			self._request("POST", f"/v3/domains/{self.domain}/webhooks", data={"id": event_id, "url": urls})
		self._webhooks[event_id] = list(urls)
