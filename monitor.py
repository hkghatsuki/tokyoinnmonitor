#!/usr/bin/env python3
"""Toyoko Inn multi-area vacancy monitor with built-in scheduler."""

from __future__ import annotations

import hashlib
import json
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

API_URL = "https://www.toyoko-inn.com/api/trpc/public.areas.byId,hotels.availabilities.prices"

BROWSER_HEADERS = {
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


@dataclass
class Config:
    area_ids: list[int]
    checkin_date: str
    checkout_date: str
    number_of_people: int
    number_of_room: int
    smoking_type: str
    preferred_hotel_codes: list[str]
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


class RequestPacer:
    def __init__(self, min_interval: float, jitter: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self.jitter = max(0.0, jitter)
        self.last_request_ts: float | None = None

    def pace(self) -> None:
        jitter = random.uniform(0.0, self.jitter)
        target_interval = self.min_interval + jitter
        if self.last_request_ts is not None:
            elapsed = time.time() - self.last_request_ts
            wait_s = target_interval - elapsed
            if wait_s > 0:
                time.sleep(wait_s)
        self.last_request_ts = time.time()


def load_env_file(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def must_get_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


def parse_area_ids() -> list[int]:
    raw = os.getenv("AREA_IDS", "").strip() or os.getenv("AREA_ID", "").strip()
    if not raw:
        raise ValueError("Missing required env var: AREA_IDS (or AREA_ID)")

    ids: list[int] = []
    for item in raw.split(","):
        token = item.strip()
        if token:
            ids.append(int(token))

    unique_sorted = sorted(set(ids))
    if not unique_sorted:
        raise ValueError("AREA_IDS must include at least one area id")
    return unique_sorted


def parse_date_to_iso(value: str) -> str:
    value = value.strip()
    if value.endswith("Z"):
        return value
    if len(value) == 10 and value.count("-") == 2:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(hour=16, tzinfo=timezone.utc)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_config() -> Config:
    load_env_file()
    checkin_raw = must_get_env("CHECKIN_DATE")
    checkout_raw = os.getenv("CHECKOUT_DATE", "").strip()
    if not checkout_raw:
        checkin_dt = datetime.fromisoformat(parse_date_to_iso(checkin_raw).replace("Z", "+00:00"))
        checkout_raw = (checkin_dt + timedelta(days=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    preferred = [x.strip() for x in os.getenv("HOTEL_CODES", "").split(",") if x.strip()]

    return Config(
        area_ids=parse_area_ids(),
        checkin_date=parse_date_to_iso(checkin_raw),
        checkout_date=parse_date_to_iso(checkout_raw),
        number_of_people=int(os.getenv("NUMBER_OF_PEOPLE", "2")),
        number_of_room=int(os.getenv("NUMBER_OF_ROOM", "1")),
        smoking_type=os.getenv("SMOKING_TYPE", "all").strip() or "all",
        preferred_hotel_codes=preferred,
        state_file=Path(os.getenv("STATE_FILE", ".toyoko_state.json")),
        notify_on_first_run=os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() == "true",
        notify_when_available_always=os.getenv("NOTIFY_WHEN_AVAILABLE_ALWAYS", "true").lower() == "true",
        min_request_interval_seconds=float(os.getenv("MIN_REQUEST_INTERVAL_SECONDS", "1.5")),
        request_jitter_seconds=float(os.getenv("REQUEST_JITTER_SECONDS", "1.2")),
        area_loop_delay_seconds=float(os.getenv("AREA_LOOP_DELAY_SECONDS", "2.0")),
        schedule_interval_seconds=int(os.getenv("SCHEDULE_INTERVAL_SECONDS", "900")),
        schedule_jitter_seconds=int(os.getenv("SCHEDULE_JITTER_SECONDS", "30")),
        run_once=os.getenv("RUN_ONCE", "false").lower() == "true",
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        line_bot_channel_access_token=os.getenv("LINE_BOT_CHANNEL_ACCESS_TOKEN", "").strip(),
        line_bot_to=os.getenv("LINE_BOT_TO", "").strip(),
    )


def get_json(url: str, params: dict[str, str], headers: dict[str, str], pacer: RequestPacer | None = None) -> Any:
    if pacer:
        pacer.pace()
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=45) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


def extract_trpc_payload(resp: Any, index: int) -> Any:
    node = {}
    if isinstance(resp, list) and len(resp) > index:
        node = resp[index]
    elif isinstance(resp, dict):
        node = resp.get(str(index), {})
    return node.get("result", {}).get("data", {}).get("json", {}) if isinstance(node, dict) else {}


def hotel_name_map_from_area(area_payload: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not isinstance(area_payload, dict):
        return mapping

    regions = area_payload.get("regions", [])
    if not isinstance(regions, list):
        return mapping

    for region in regions:
        if not isinstance(region, dict):
            continue
        hotels = region.get("hotels", [])
        if not isinstance(hotels, list):
            continue
        for hotel in hotels:
            if not isinstance(hotel, dict):
                continue
            code = str(hotel.get("code") or hotel.get("hotelCode") or "").strip()
            name = str(hotel.get("name") or hotel.get("hotelName") or code).strip()
            if code:
                mapping[code] = name
    return mapping


def has_stock_signal(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("available") is True or node.get("isAvailable") is True:
            return True
        if node.get("soldOut") is False or node.get("isSoldOut") is False:
            return True
        if node.get("full") is False or node.get("isFull") is False:
            return True

        for k in ("remaining", "remainingRooms", "remainingRoomCount", "stock", "stocks"):
            v = node.get(k)
            if isinstance(v, int) and v > 0:
                return True

        return any(has_stock_signal(v) for v in node.values())

    if isinstance(node, list):
        return any(has_stock_signal(v) for v in node)
    return False


def parse_available_hotels(prices_payload: Any, target_codes: list[str]) -> list[str]:
    targets = set(target_codes)
    available: set[str] = set()

    if isinstance(prices_payload, dict):
        for code in targets:
            if code in prices_payload and has_stock_signal(prices_payload[code]):
                available.add(code)

        for key in ("hotels", "items", "results", "availabilities"):
            items = prices_payload.get(key)
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("hotelCode") or item.get("code") or item.get("hotelCd") or "").strip()
                    if code in targets and has_stock_signal(item):
                        available.add(code)

    elif isinstance(prices_payload, list):
        for item in prices_payload:
            if not isinstance(item, dict):
                continue
            code = str(item.get("hotelCode") or item.get("code") or item.get("hotelCd") or "").strip()
            if code in targets and has_stock_signal(item):
                available.add(code)

    return sorted(available)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_state_key(cfg: Config, area_id: int, target_codes: list[str]) -> str:
    raw = json.dumps(
        {
            "area_id": area_id,
            "hotel_codes": sorted(target_codes),
            "checkin": cfg.checkin_date,
            "checkout": cfg.checkout_date,
            "people": cfg.number_of_people,
            "rooms": cfg.number_of_room,
            "smoking": cfg.smoking_type,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_message(cfg: Config, area_id: int, checked_hotel_count: int, available_codes: list[str], name_map: dict[str, str]) -> str:
    lines = [
        "Toyoko Inn 區域空房通知",
        f"Area ID: {area_id}",
        f"入住: {cfg.checkin_date}",
        f"退房: {cfg.checkout_date}",
        f"人數: {cfg.number_of_people} / 房間: {cfg.number_of_room}",
        f"此區域查詢飯店數: {checked_hotel_count}",
    ]

    if available_codes:
        lines.append(f"有空房飯店數: {len(available_codes)}")
        for code in available_codes:
            lines.append(f"- {name_map.get(code, code)} ({code})")
    else:
        lines.append("目前此區域無空房")
    return "\n".join(lines)


def notify_telegram(cfg: Config, message: str) -> None:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    post_json(url, {"chat_id": cfg.telegram_chat_id, "text": message})


def notify_line_bot(cfg: Config, message: str) -> None:
    if not (cfg.line_bot_channel_access_token and cfg.line_bot_to):
        return

    payload = {
        "to": cfg.line_bot_to,
        "messages": [{"type": "text", "text": message}],
    }
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg.line_bot_channel_access_token}",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_HEADERS["User-Agent"],
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):  # nosec B310
        pass


def process_area(cfg: Config, area_id: int, state: dict[str, Any], pacer: RequestPacer) -> dict[str, Any]:
    meta_input = {"0": {"json": {"id": area_id}}}
    meta_resp = get_json(
        API_URL,
        {"batch": "1", "input": json.dumps(meta_input, separators=(",", ":"))},
        headers=BROWSER_HEADERS,
        pacer=pacer,
    )

    area_payload = extract_trpc_payload(meta_resp, 0)
    name_map = hotel_name_map_from_area(area_payload)
    area_codes = sorted(name_map.keys())
    if not area_codes:
        raise ValueError(f"No hotel list found for AREA_ID={area_id}")

    target_codes = sorted(set(cfg.preferred_hotel_codes) & set(area_codes)) if cfg.preferred_hotel_codes else area_codes
    if not target_codes:
        raise ValueError(f"HOTEL_CODES provided but none belong to AREA_ID={area_id}")

    input_payload = {
        "0": {"json": {"id": area_id}},
        "1": {
            "json": {
                "hotelCodes": target_codes,
                "checkinDate": cfg.checkin_date,
                "checkoutDate": cfg.checkout_date,
                "numberOfPeople": cfg.number_of_people,
                "numberOfRoom": cfg.number_of_room,
                "smokingType": cfg.smoking_type,
            },
            "meta": {"values": {"checkinDate": ["Date"], "checkoutDate": ["Date"]}},
        },
    }

    resp = get_json(
        API_URL,
        {"batch": "1", "input": json.dumps(input_payload, separators=(",", ":"))},
        headers=BROWSER_HEADERS,
        pacer=pacer,
    )
    prices_payload = extract_trpc_payload(resp, 1)
    available_codes = parse_available_hotels(prices_payload, target_codes)

    state_key = build_state_key(cfg, area_id, target_codes)
    prev_hash = state.get(state_key, {}).get("availability_hash")
    current_hash = hashlib.sha256(json.dumps(available_codes).encode("utf-8")).hexdigest()
    first_run = prev_hash is None
    changed = prev_hash != current_hash

    should_notify = False
    if available_codes and cfg.notify_when_available_always:
        should_notify = True
    elif changed and (cfg.notify_on_first_run or not first_run):
        should_notify = True

    if should_notify:
        message = build_message(cfg, area_id, len(target_codes), available_codes, name_map)
        notify_telegram(cfg, message)
        notify_line_bot(cfg, message)
        notify_result = "Notification sent."
    else:
        notify_result = "No notification sent."

    state[state_key] = {
        "availability_hash": current_hash,
        "available_codes": available_codes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "area_id": area_id,
    }

    return {
        "area_id": area_id,
        "area_hotels": len(area_codes),
        "checked_hotels": len(target_codes),
        "available_hotels": len(available_codes),
        "notify_result": notify_result,
    }


def run_single_cycle(cfg: Config, state: dict[str, Any]) -> None:
    pacer = RequestPacer(cfg.min_request_interval_seconds, cfg.request_jitter_seconds)
    for i, area_id in enumerate(cfg.area_ids):
        result = process_area(cfg, area_id, state, pacer)
        print(
            f"Area {result['area_id']}: total={result['area_hotels']}, "
            f"checked={result['checked_hotels']}, available={result['available_hotels']}"
        )
        print(result["notify_result"])

        if i < len(cfg.area_ids) - 1 and cfg.area_loop_delay_seconds > 0:
            time.sleep(cfg.area_loop_delay_seconds)


def sleep_until_next_cycle(cfg: Config) -> None:
    jitter = random.randint(0, max(0, cfg.schedule_jitter_seconds))
    wait_seconds = max(1, cfg.schedule_interval_seconds + jitter)
    print(f"Next cycle in {wait_seconds} seconds...")
    time.sleep(wait_seconds)


def main() -> int:
    try:
        cfg = load_config()
        state = load_state(cfg.state_file)

        while True:
            print(f"=== Cycle start: {datetime.now(timezone.utc).isoformat()} ===")
            run_single_cycle(cfg, state)
            save_state(cfg.state_file, state)

            if cfg.run_once:
                print("RUN_ONCE=true, exiting after one cycle.")
                break

            sleep_until_next_cycle(cfg)

        return 0
    except (ValueError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
