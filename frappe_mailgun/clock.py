from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import convert_utc_to_system_timezone, get_system_timezone


def to_unix(dt: datetime) -> float:
	"""Unix seconds of a naive datetime read in the System Settings time zone."""
	return dt.replace(tzinfo=ZoneInfo(get_system_timezone())).timestamp()


def from_unix(ts: float) -> datetime:
	"""A naive datetime in the System Settings time zone for unix seconds."""
	return convert_utc_to_system_timezone(datetime.fromtimestamp(float(ts), UTC)).replace(tzinfo=None)
