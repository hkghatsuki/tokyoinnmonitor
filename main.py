#!/usr/bin/env python3
"""
Toyoko Inn hotel availability monitor.

Two-step fetch flow per area:
  1. GET _next/data search endpoint  →  collect hotelCode + hotelName
  2. GET tRPC hotels.availabilities.prices  →  parse stock signals

Notifications via Telegram and/or LINE Bot when availability changes
or when rooms are available (depending on config).  Error alerts are
sent through the same channels when an area check fails, so the
monitor never fails silently.

All configuration is loaded from environment variables (or a .env file).
No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Endpoints
# NOTE: SEARCH_URL contains a Next.js build hash that changes on each
#       site deployment.  Update it when requests start returning 404.
# ---------------------------------------------------------------------------
SEARCH_URL = "https://www.toyoko-inn.com/_next/data/Q26kEC5gXEbF5My2xy3e5/china/search/result.json"
AVAILABILITY_URL = (
    "https://www.toyoko-inn.com/api/trpc/hotels.availabilities.prices"
)

BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7",
    "Referer": "https://www.toyoko-inn.com/",
    "Origin": "https://www.toyoko-inn.com",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search target  (area or prefecture)
# ---------------------------------------------------------------------------
@dataclass
class SearchTarget:
    """Represents one unit of work: either an area ID or a prefecture code.

    kind    : "area" | "prefecture"
    value   : str representation used in the search URL param
               e.g. "463" for area, "13-all" for prefecture
    display : human-readable label, updated from the API response when available
    """

    kind: str
    value: str
    display: str = ""

    def __post_init__(self) -> None:
        if not self.display:
            self.display = self.value

    @property
    def search_param(self) -> dict[str, str]:
        """Return the single query-param dict for the _next/data search URL."""
        return {self.kind: self.value}

    @property
    def is_area(self) -> bool:
        return self.kind == "area"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Config:
    area_ids: list[int]          # may be empty when only PREFECTURES is used
    prefecture_ids: list[str]    # may be empty when only AREA_IDS is used
    checkin_date: str  # ISO-8601 UTC, e.g. "2026-03-18T16:00:00.000Z"
    checkout_date: str
    number_of_people: int
    number_of_room: int
    smoking_type: str
    preferred_hotel_codes: list[str]  # empty → monitor all hotels in area
    state_file: Path
    notify_on_first_run: bool
    notify_when_available_always: bool
    min_request_interval_seconds: float
    request_jitter_seconds: float
    area_loop_delay_seconds: float
    schedule_interval_seconds: int
    schedule_jitter_seconds: int
    run_once: bool
    telegram_bot_token: str
    telegram_chat_id: str
    line_bot_channel_access_token: str
    line_bot_to: str


# ---------------------------------------------------------------------------
# Environment / config loading
# ---------------------------------------------------------------------------
def _load_env_file(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _must_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


def _parse_area_ids() -> list[int]:
    """Parse AREA_IDS / AREA_ID env var.  Returns empty list when unset."""
    raw = os.getenv("AREA_IDS", "").strip() or os.getenv("AREA_ID", "").strip()
    if not raw:
        return []
    ids: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if token:
            ids.append(int(token))
    return sorted(set(ids))


def _parse_prefectures() -> list[str]:
    """Parse PREFECTURES env var (comma-separated, e.g. '13-all,27-all')."""
    raw = os.getenv("PREFECTURES", "").strip()
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


# UTC offset for the target timezone (GMT+8 = HKT / CST / JST-1)
_LOCAL_UTC_OFFSET_HOURS = 8


def _date_to_iso(value: str) -> str:
    """Normalise a date string to UTC ISO-8601 with milliseconds and Z suffix.

    Accepts:
      - "YYYY-MM-DD"  → interpreted as 00:00 GMT+8 on that date, converted to UTC
                        e.g. "2026-04-04" → "2026-04-03T16:00:00.000Z"
      - any ISO-8601 / datetime str  → converted to UTC as-is
      - already-valid "...Z" strings → returned unchanged
    """
    value = value.strip()
    if value.endswith("Z"):
        return value
    if len(value) == 10 and value.count("-") == 2:
        # Treat as midnight local time (GMT+8), subtract offset to get UTC
        local_midnight = datetime.strptime(value, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        utc_dt = local_midnight - timedelta(hours=_LOCAL_UTC_OFFSET_HOURS)
        return (
            utc_dt.replace(tzinfo=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def load_config() -> Config:
    _load_env_file()

    checkin_raw = _must_env("CHECKIN_DATE")
    checkin_iso = _date_to_iso(checkin_raw)

    checkout_raw = os.getenv("CHECKOUT_DATE", "").strip()
    if checkout_raw:
        checkout_iso = _date_to_iso(checkout_raw)
    else:
        checkin_dt = datetime.fromisoformat(checkin_iso.replace("Z", "+00:00"))
        checkout_iso = (
            (checkin_dt + timedelta(days=1))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    area_ids = _parse_area_ids()
    prefecture_ids = _parse_prefectures()
    if not area_ids and not prefecture_ids:
        raise ValueError(
            "At least one of AREA_IDS (or AREA_ID) or PREFECTURES must be set"
        )

    preferred = [
        x.strip() for x in os.getenv("HOTEL_CODES", "").split(",") if x.strip()
    ]

    return Config(
        area_ids=area_ids,
        prefecture_ids=prefecture_ids,
        checkin_date=checkin_iso,
        checkout_date=checkout_iso,
        number_of_people=int(os.getenv("NUMBER_OF_PEOPLE", "2")),
        number_of_room=int(os.getenv("NUMBER_OF_ROOM", "1")),
        smoking_type=os.getenv("SMOKING_TYPE", "all").strip() or "all",
        preferred_hotel_codes=preferred,
        state_file=Path(os.getenv("STATE_FILE", ".toyoko_state.json")),
        notify_on_first_run=os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() == "true",
        notify_when_available_always=os.getenv(
            "NOTIFY_WHEN_AVAILABLE_ALWAYS", "true"
        ).lower()
        == "true",
        min_request_interval_seconds=float(
            os.getenv("MIN_REQUEST_INTERVAL_SECONDS", "1.5")
        ),
        request_jitter_seconds=float(os.getenv("REQUEST_JITTER_SECONDS", "1.2")),
        area_loop_delay_seconds=float(os.getenv("AREA_LOOP_DELAY_SECONDS", "2.0")),
        schedule_interval_seconds=int(os.getenv("SCHEDULE_INTERVAL_SECONDS", "900")),
        schedule_jitter_seconds=int(os.getenv("SCHEDULE_JITTER_SECONDS", "30")),
        run_once=os.getenv("RUN_ONCE", "false").lower() == "true",
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        line_bot_channel_access_token=os.getenv(
            "LINE_BOT_CHANNEL_ACCESS_TOKEN", ""
        ).strip(),
        line_bot_to=os.getenv("LINE_BOT_TO", "").strip(),
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
class RequestPacer:
    """Enforces a minimum interval (+ random jitter) between HTTP requests."""

    def __init__(self, min_interval: float, jitter: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self.jitter = max(0.0, jitter)
        self._last_ts: float | None = None

    def pace(self) -> None:
        target = self.min_interval + random.uniform(0.0, self.jitter)
        if self._last_ts is not None:
            wait = target - (time.monotonic() - self._last_ts)
            if wait > 0:
                time.sleep(wait)
        self._last_ts = time.monotonic()


def _get_json(
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    pacer: RequestPacer | None = None,
) -> Any:
    if pacer:
        pacer.pace()
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    log.debug("GET %s", full_url)
    req = urllib.request.Request(full_url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=45) as resp:  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def _http_post(
    url: str, payload: dict[str, Any], extra_headers: dict[str, str] | None = None
) -> None:
    headers = {"Content-Type": "application/json", **(extra_headers or {})}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):  # nosec B310
        pass


# ---------------------------------------------------------------------------
# Step 1 — fetch hotel list from the _next/data search endpoint
# ---------------------------------------------------------------------------
def fetch_hotels(
    target: SearchTarget, cfg: Config, pacer: RequestPacer
) -> tuple[dict[str, str], str]:
    """Return ({hotelCode: hotelName}, display_label) for every hotel in *target*.

    For area targets the display label is updated from the API response
    (e.g. "東京、日本橋周邊 (463)").
    For prefecture targets the display label stays as the prefecture code
    because the response gives no distinct area metadata.
    """
    checkin_dt = datetime.fromisoformat(cfg.checkin_date.replace("Z", "+00:00"))
    checkout_dt = datetime.fromisoformat(cfg.checkout_date.replace("Z", "+00:00"))

    params: dict[str, str] = {
        **target.search_param,
        "people": str(cfg.number_of_people),
        "room": str(cfg.number_of_room),
        "smoking": cfg.smoking_type,
        "start": checkin_dt.strftime("%Y-%m-%d"),
        "end": checkout_dt.strftime("%Y-%m-%d"),
    }
    data = _get_json(SEARCH_URL, params, BROWSER_HEADERS, pacer)

    name_map: dict[str, str] = {}
    # Response shape: result.pageProps.searchResponse.hotels
    page_props = (data.get("pageProps") or {}) if isinstance(data, dict) else {}
    search_response = page_props.get("searchResponse") or {}

    # Derive display label — only meaningful for area targets
    if target.is_area:
        area_node = search_response.get("area") or {}
        area_name = str(
            (area_node.get("areaName") or area_node.get("name") or "")
            or search_response.get("areaName")
            or ""
        ).strip()
        display_label = f"{area_name} ({target.value})" if area_name else target.value
    else:
        display_label = target.value  # no area info for prefecture searches

    hotels = search_response.get("hotels", [])
    if not isinstance(hotels, list):
        return name_map, display_label

    for hotel in hotels:
        if not isinstance(hotel, dict):
            continue
        code = str(hotel.get("hotelCode") or hotel.get("code") or "").strip()
        name = str(hotel.get("hotelName") or hotel.get("name") or code).strip()
        if code:
            name_map[code] = name

    return name_map, display_label


# ---------------------------------------------------------------------------
# Step 2 — fetch availability / prices via tRPC
# ---------------------------------------------------------------------------
def fetch_availability(
    hotel_codes: list[str], cfg: Config, pacer: RequestPacer
) -> Any:
    """Return the prices dict keyed by hotelCode from the tRPC batch response.

    Response shape (batch index 1):
      {prices: {"00095": {lowestPrice, existEnoughVacantRooms, isUnderMaintenance}, ...}}
    """
    trpc_input = {
        "0": {
            "json": {
                "hotelCodes": hotel_codes,
                "checkinDate": cfg.checkin_date,
                "checkoutDate": cfg.checkout_date,
                "numberOfPeople": cfg.number_of_people,
                "numberOfRoom": cfg.number_of_room,
                "smokingType": cfg.smoking_type,
            },
            "meta": {"values": {"checkinDate": ["Date"], "checkoutDate": ["Date"]}},
        },
    }
    params = {
        "batch": "1",
        "input": json.dumps(trpc_input, separators=(",", ":")),
    }

    print(params)
    resp = _get_json(AVAILABILITY_URL, params, BROWSER_HEADERS, pacer)
    print(resp)
    # Batch response is a list; prices are in slot 1
    node = resp[0] if isinstance(resp, list) and len(resp) > 0 else {}
    payload = (
        node.get("result", {}).get("data", {}).get("json", {})
        if isinstance(node, dict)
        else {}
    )
    # Return the inner prices dict, or the whole payload as fallback
    return payload.get("prices", payload)


# ---------------------------------------------------------------------------
# Availability parsing
# ---------------------------------------------------------------------------
def _has_stock(node: Any) -> bool:
    """Recursively search *node* for any signal that a room is available."""
    if isinstance(node, dict):
        if node.get("available") is True or node.get("isAvailable") is True:
            return True
        if node.get("soldOut") is False or node.get("isSoldOut") is False:
            return True
        if node.get("full") is False or node.get("isFull") is False:
            return True
        for k in (
            "remaining",
            "remainingRooms",
            "remainingRoomCount",
            "stock",
            "stocks",
        ):
            v = node.get(k)
            if isinstance(v, int) and v > 0:
                return True
        return any(_has_stock(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_stock(v) for v in node)
    return False


def parse_available(prices: Any, target_codes: list[str]) -> list[str]:
    """Return sorted list of hotel codes that have rooms available.

    Expects *prices* to be a dict keyed by hotelCode:
      {"00095": {"lowestPrice": 12800, "existEnoughVacantRooms": True, "isUnderMaintenance": False}, ...}

    A hotel is considered available when:
      - existEnoughVacantRooms is True, AND
      - isUnderMaintenance is False (or absent)
    """
    if not isinstance(prices, dict):
        return []

    targets = set(target_codes)
    available: list[str] = []
    for code in sorted(targets):
        entry = prices.get(code)
        if not isinstance(entry, dict):
            continue
        if entry.get("existEnoughVacantRooms") is True and not entry.get("isUnderMaintenance", False):
            available.append(code)
    return available


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read state file %s; starting fresh.", path)
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _state_key(cfg: Config, target_kind: str, target_value: str) -> str:
    raw = json.dumps(
        {
            "target_kind": target_kind,
            "target": target_value,
            "checkin": cfg.checkin_date,
            "checkout": cfg.checkout_date,
            "people": cfg.number_of_people,
            "rooms": cfg.number_of_room,
            "smoking": cfg.smoking_type,
            "preferred_hotel_codes": sorted(cfg.preferred_hotel_codes),
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def _label(code: str, name_map: dict[str, str]) -> str:
    """Return 'Hotel Name (code)' or just 'code' when the name is unknown."""
    name = name_map.get(code, "")
    return f"{name} ({code})" if name and name != code else code


def _utc_iso_to_local_date(iso_utc: str) -> str:
    """Convert a UTC ISO-8601 string back to a YYYY-MM-DD local date (GMT+8).

    e.g. "2026-04-03T16:00:00.000Z" → "2026-04-04"
    """
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    local_dt = dt + timedelta(hours=_LOCAL_UTC_OFFSET_HOURS)
    return local_dt.strftime("%Y-%m-%d")


def _build_availability_message(
    cfg: Config,
    target_label: str,
    checked: int,
    available_codes: list[str],
    name_map: dict[str, str],
) -> str:
    lines = [
        "東橫INN空房通知",
        f"區域     : {target_label}",
        f"入住     : {_utc_iso_to_local_date(cfg.checkin_date)}",
        f"退房     : {_utc_iso_to_local_date(cfg.checkout_date)}",
        f"人數/房間: {cfg.number_of_people} / {cfg.number_of_room}",
        f"查詢飯店 : {checked}",
    ]
    if available_codes:
        lines.append(f"有空房   : {len(available_codes)} 間")
        for code in available_codes:
            lines.append(f"  • {name_map.get(code, code)} ({code})")
    else:
        lines.append("結果     : 目前無空房")
    return "\n".join(lines)


def _build_error_message(target_label: str, error: Exception) -> str:
    return (
        f"[ERROR] Toyoko Inn 監控失敗\n"
        f"區域: {target_label}\n"
        f"{type(error).__name__}: {error}"
    )


def _send_telegram(cfg: Config, message: str) -> None:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return
    try:
        _http_post(
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
            {"chat_id": cfg.telegram_chat_id, "text": message},
        )
        log.info("Telegram notification sent.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram notification failed: %s", exc)


def _send_line(cfg: Config, message: str) -> None:
    if not (cfg.line_bot_channel_access_token and cfg.line_bot_to):
        return
    try:
        req = urllib.request.Request(
            "https://api.line.me/v2/bot/message/push",
            data=json.dumps(
                {
                    "to": cfg.line_bot_to,
                    "messages": [{"type": "text", "text": message}],
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {cfg.line_bot_channel_access_token}",
                "Content-Type": "application/json",
                "User-Agent": BROWSER_HEADERS["User-Agent"],
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30):  # nosec B310
            pass
        log.info("LINE notification sent.")
    except Exception as exc:  # noqa: BLE001
        log.warning("LINE notification failed: %s", exc)


def notify(cfg: Config, message: str) -> None:
    """Dispatch *message* to every configured notification channel."""
    # _send_telegram(cfg, message)
    _send_line(cfg, message)


# ---------------------------------------------------------------------------
# Per-target processing
# ---------------------------------------------------------------------------
def process_target(
    cfg: Config,
    target: SearchTarget,
    state: dict[str, Any],
    pacer: RequestPacer,
) -> dict[str, Any]:
    """
    Run the two-step fetch for *target*, diff against saved state, and
    send notifications if warranted.  Returns a summary dict.

    On any HTTP or parsing error, an error alert is sent via all configured
    channels and the exception is re-raised so the caller can continue with
    the next target.
    """
    display_label = target.display  # updated after step 1
    try:
        # ── Step 1: hotel list ──────────────────────────────────────────
        name_map, display_label = fetch_hotels(target, cfg, pacer)
        area_codes = sorted(name_map.keys())
        if not area_codes:
            raise ValueError(f"No hotels returned for {target.kind}={target.value}")

        target_codes = (
            sorted(set(cfg.preferred_hotel_codes) & set(area_codes))
            if cfg.preferred_hotel_codes
            else area_codes
        )
        if not target_codes:
            raise ValueError(
                f"HOTEL_CODES specified but none belong to {target.kind}={target.value}"
            )

        log.info(
            "%s: %d hotels in area, %d to check.",
            display_label,
            len(area_codes),
            len(target_codes),
        )

        # ── Step 2: availability ────────────────────────────────────────
        availability_payload = fetch_availability(target_codes, cfg, pacer)
        available_codes = parse_available(availability_payload, target_codes)

        if available_codes:
            labels = ", ".join(_label(c, name_map) for c in available_codes)
            log.info(
                "%s: %d available — %s",
                display_label,
                len(available_codes),
                labels,
            )
        else:
            log.info("%s: no availability.", display_label)

    except Exception as exc:
        log.error("%s check failed: %s", display_label, exc)
        notify(cfg, _build_error_message(display_label, exc))
        raise

    # ── Diff & notify ───────────────────────────────────────────────────
    key = _state_key(cfg, target.kind, target.value)
    prev_hash = state.get(key, {}).get("availability_hash")
    current_hash = hashlib.sha256(json.dumps(available_codes).encode()).hexdigest()
    first_run = prev_hash is None
    changed = prev_hash != current_hash

    # Deduplication: only fire when the available-hotel set has actually changed.
    # notify_when_available_always=True  → notify on any change that yields rooms
    #                                      (suppresses "no rooms" change alerts)
    # notify_when_available_always=False → notify on every change (rooms or none)
    should_notify = changed and (cfg.notify_on_first_run or not first_run) and (
        bool(available_codes) or not cfg.notify_when_available_always
    )

    if should_notify:
        msg = _build_availability_message(
            cfg, display_label, len(target_codes), available_codes, name_map
        )
        notify(cfg, msg)
        notify_result = "notified"
    else:
        notify_result = "no notification"

    state[key] = {
        "availability_hash": current_hash,
        "available_codes": available_codes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "target_kind": target.kind,
        "target_value": target.value,
        "display_label": display_label,
    }

    return {
        "display_label": display_label,
        "area_hotels": len(area_codes),
        "checked_hotels": len(target_codes),
        "available_hotels": len(available_codes),
        "available_labels": [_label(c, name_map) for c in available_codes],
        "notify_result": notify_result,
    }


# ---------------------------------------------------------------------------
# Scheduler / main loop
# ---------------------------------------------------------------------------
def run_cycle(cfg: Config, state: dict[str, Any]) -> None:
    pacer = RequestPacer(cfg.min_request_interval_seconds, cfg.request_jitter_seconds)

    targets: list[SearchTarget] = [
        SearchTarget(kind="area", value=str(aid)) for aid in cfg.area_ids
    ] + [
        SearchTarget(kind="prefecture", value=pref) for pref in cfg.prefecture_ids
    ]

    for i, target in enumerate(targets):
        try:
            result = process_target(cfg, target, state, pacer)
            available_summary = (
                ", ".join(result["available_labels"])
                if result["available_labels"]
                else "none"
            )
            log.info(
                "%s done — area=%d checked=%d available=%d [%s] hotels: %s",
                result["display_label"],
                result["area_hotels"],
                result["checked_hotels"],
                result["available_hotels"],
                result["notify_result"],
                available_summary,
            )
        except Exception:
            # Error already logged and notified inside process_target; continue
            # with the remaining targets in this cycle.
            pass

        if i < len(targets) - 1 and cfg.area_loop_delay_seconds > 0:
            time.sleep(cfg.area_loop_delay_seconds)


def _sleep_until_next(cfg: Config) -> None:
    jitter = random.randint(0, max(0, cfg.schedule_jitter_seconds))
    wait = max(1, cfg.schedule_interval_seconds + jitter)
    log.info("Next cycle in %d seconds.", wait)
    time.sleep(wait)


def main() -> int:
    try:
        cfg = load_config()
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    state = load_state(cfg.state_file)

    while True:
        log.info("=== Cycle start %s ===", datetime.now(timezone.utc).isoformat())
        run_cycle(cfg, state)
        save_state(cfg.state_file, state)

        if cfg.run_once:
            log.info("RUN_ONCE=true — exiting.")
            break

        _sleep_until_next(cfg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
