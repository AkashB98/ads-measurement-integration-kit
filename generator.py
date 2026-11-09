"""Deterministic synthetic ad-measurement generator — Helios Home (fictional).

SYNTHETIC DATA ONLY. No real analytics, customer, or revenue data is ever
used. Every row is fabricated from a seeded PRNG so the same seed always
yields the same dataset, byte-for-byte.

A dataset has five tables:

  ground_truth — the true conversions that actually happened:
                 conversion_id, event_id, anon_id, user_id, timestamp,
                 channel, revenue, consented
  pixel        — browser pixel events: event_id (nullable), source,
                 anon_id, user_id (often null — the browser usually only
                 knows the anonymous id), timestamp, channel, revenue
  server       — server conversion-API events: event_id (nullable),
                 source, anon_id (often null), user_id, timestamp,
                 channel, revenue
  identifies   — explicit login / identify events: anon_id, user_id,
                 timestamp. The ONLY bridge from an anonymous id to a
                 user id (see reconcile.py — deterministic stitching).
  consent      — consent records: anon_id, user_id, consented

The two observed streams are *derived* from ground truth with planted
defects, so the reconciler's job is measurable against truth:

  - both-stream conversions (pixel + server describe the same
    conversion — counting both streams naively double-counts them)
  - pixel-only conversions (the server event was lost)
  - server-only conversions (the pixel was blocked)
  - duplicate deliveries (the same event_id delivered twice in one
    stream)
  - timestamp skew (server timestamps run minutes behind pixel ones)
  - missing shared ids (some server events arrive with no event_id and
    can only be recovered by the fallback match)
  - non-consented users' events are present in the raw streams — the
    raw feed does not gate; gating is the reconciler's job.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

CHANNELS = ["organic_search", "paid_search", "paid_social", "organic_social",
            "display", "email", "referral", "direct"]
CHANNEL_WEIGHTS = [0.18, 0.16, 0.14, 0.10, 0.12, 0.10, 0.08, 0.12]

PRODUCTS = [("Helios Thermo", 249.0, 0.22), ("Helios Cam", 129.0, 0.24),
            ("Helios Bell", 99.0, 0.18), ("Helios Lumen Kit", 79.0, 0.16),
            ("Helios Hub", 149.0, 0.12), ("Helios Bundle", 399.0, 0.08)]

WINDOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
WINDOW_DAYS = 14

# Planted-defect rates (the generator's knobs; the reconciler never sees
# them — it must recover the truth from the streams alone).
P_BOTH = 0.70           # conversion observed by both streams
P_PIXEL_ONLY = 0.15     # server event lost
# remainder (0.15) is server-only: the pixel was blocked
P_FALLBACK = 0.20       # of both-stream: server event_id missing
P_DUPLICATE = 0.12      # an emitted event is delivered twice
P_CONSENTED = 0.85      # share of people who consented
P_IDENTIFIED = 0.75     # share of people with an identify event

GROUND_TRUTH_FIELDS = ["conversion_id", "event_id", "anon_id", "user_id",
                       "timestamp", "channel", "revenue", "consented"]
EVENT_FIELDS = ["event_id", "source", "anon_id", "user_id", "timestamp",
                "channel", "revenue"]
IDENTIFY_FIELDS = ["anon_id", "user_id", "timestamp"]
CONSENT_FIELDS = ["anon_id", "user_id", "consented"]

DEFAULT_SEED = 42
DEFAULT_CONVERSIONS = 240


def format_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _weighted(rng: random.Random, items, weights):
    return rng.choices(items, weights=weights, k=1)[0]


def generate_dataset(conversions: int = DEFAULT_CONVERSIONS,
                     seed: int = DEFAULT_SEED) -> Dict[str, List[Dict]]:
    """Return the five-table dataset, deterministic for ``seed``.

    People are created on demand: each conversion is assigned to a
    person drawn from a growing pool (a person may convert more than
    once, spaced at least 2 hours apart so the fallback window in
    reconcile.py can never confuse two conversions of one person).
    A person who never identifies can still be exact-id matched, but
    never fallback matched — the generator therefore only plants a
    missing server id on identified people, mirroring the real
    constraint that identity-less fallback matching is impossible.
    """
    if conversions < 0:
        raise ValueError("conversions must be >= 0")
    rng = random.Random(seed)
    ground_truth: List[Dict] = []
    pixel: List[Dict] = []
    server: List[Dict] = []
    identifies: List[Dict] = []
    consent: List[Dict] = []

    people: List[Dict] = []  # {anon_id, user_id, consented, identified, last_ts}
    t_cursor = WINDOW_START + timedelta(hours=1)

    for i in range(conversions):
        # Reuse an existing person with spare spacing, else create one.
        reusable = [p for p in people
                    if t_cursor - p["last_ts"] >= timedelta(hours=2)]
        if reusable and rng.random() < 0.55:
            person = rng.choice(reusable)
        else:
            n = len(people) + 1
            person = {
                "anon_id": f"ANON-{n:04d}",
                "user_id": f"HH-U{n:05d}",
                "consented": rng.random() < P_CONSENTED,
                "identified": rng.random() < P_IDENTIFIED,
                "last_ts": t_cursor - timedelta(hours=3),
            }
            people.append(person)
            consent.append({"anon_id": person["anon_id"],
                            "user_id": person["user_id"],
                            "consented": person["consented"]})
            if person["identified"]:
                identifies.append({
                    "anon_id": person["anon_id"],
                    "user_id": person["user_id"],
                    "timestamp": format_ts(t_cursor - timedelta(minutes=30)),
                })
        # Advance the clock so timestamps strictly increase overall.
        t_cursor = t_cursor + timedelta(minutes=rng.randint(20, 180))
        if person["last_ts"] + timedelta(hours=2) > t_cursor:
            t_cursor = person["last_ts"] + timedelta(hours=2)
        true_ts = t_cursor
        person["last_ts"] = true_ts

        channel = _weighted(rng, CHANNELS, CHANNEL_WEIGHTS)
        product = _weighted(rng, PRODUCTS, [w for _, _, w in PRODUCTS])
        qty = 2 if rng.random() < 0.18 else 1
        revenue = round(product[1] * qty, 2)
        event_id = f"EVT-{i + 1:05d}"
        conversion_id = f"CNV-{i + 1:05d}"
        ground_truth.append({
            "conversion_id": conversion_id,
            "event_id": event_id,
            "anon_id": person["anon_id"],
            "user_id": person["user_id"],
            "timestamp": format_ts(true_ts),
            "channel": channel,
            "revenue": revenue,
            "consented": person["consented"],
        })

        roll = rng.random()
        if roll < P_BOTH:
            coverage = "both"
        elif roll < P_BOTH + P_PIXEL_ONLY:
            coverage = "pixel_only"
        else:
            coverage = "server_only"
        fallback = (coverage == "both" and person["identified"]
                    and rng.random() < P_FALLBACK)

        pixel_event = {
            "event_id": event_id,
            "source": "pixel",
            "anon_id": person["anon_id"],
            "user_id": person["user_id"] if person["identified"] else None,
            "timestamp": format_ts(true_ts + timedelta(seconds=rng.randint(0, 60))),
            "channel": channel,
            "revenue": revenue,
        }
        server_event = {
            "event_id": None if fallback else event_id,
            "source": "server",
            "anon_id": None,
            "user_id": person["user_id"],
            "timestamp": format_ts(true_ts + timedelta(seconds=rng.randint(120, 480))),
            "channel": channel,
            "revenue": revenue,
        }
        if coverage in ("both", "pixel_only"):
            pixel.append(pixel_event)
            if rng.random() < P_DUPLICATE:
                pixel.append(dict(pixel_event))
        if coverage in ("both", "server_only"):
            server.append(server_event)
            if rng.random() < P_DUPLICATE:
                server.append(dict(server_event))

    pixel.sort(key=lambda e: (e["timestamp"], e["event_id"] or ""))
    server.sort(key=lambda e: (e["timestamp"], e["event_id"] or ""))
    identifies.sort(key=lambda e: e["anon_id"])
    consent.sort(key=lambda e: e["anon_id"])
    return {"ground_truth": ground_truth, "pixel": pixel, "server": server,
            "identifies": identifies, "consent": consent}


# ------------------------------------------------------------ serializers

def _to_jsonl(rows: List[Dict]) -> str:
    import json
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def _to_csv(rows: List[Dict], fields: List[str]) -> str:
    import csv
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in fields})
    return buf.getvalue()


def _serializers(fmt: str):
    if fmt == "jsonl":
        return _to_jsonl, None
    raise ValueError(f"unknown format: {fmt}")


def ground_truth_to_jsonl(rows: List[Dict]) -> str:
    return _to_jsonl(rows)


def pixel_to_jsonl(rows: List[Dict]) -> str:
    return _to_jsonl(rows)


def server_to_jsonl(rows: List[Dict]) -> str:
    return _to_jsonl(rows)


def tables_to_jsonl(dataset: Dict[str, List[Dict]], table: str) -> str:
    return _to_jsonl(dataset[table])


# --------------------------------------------------------------- loaders
# CSV parses every value as a string, so numeric / boolean / null fields
# are coerced explicitly here (the sibling builds pin the same lesson:
# without this layer, revenue silently stays a string, an empty
# event_id stays "" instead of None, and report math breaks).

def _coerce_event(row: Dict) -> Dict:
    out = dict(row)
    if out.get("event_id") == "":
        out["event_id"] = None
    if out.get("user_id") == "":
        out["user_id"] = None
    if out.get("anon_id") == "":
        out["anon_id"] = None
    if isinstance(out.get("revenue"), str):
        try:
            out["revenue"] = float(out["revenue"])
        except ValueError:
            pass
    return out


def _coerce_truth(row: Dict) -> Dict:
    out = dict(row)
    if isinstance(out.get("revenue"), str):
        try:
            out["revenue"] = float(out["revenue"])
        except ValueError:
            pass
    if isinstance(out.get("consented"), str):
        out["consented"] = out["consented"].strip().lower() in ("true", "1", "yes")
    return out


def _coerce_consent(row: Dict) -> Dict:
    out = dict(row)
    if isinstance(out.get("consented"), str):
        out["consented"] = out["consented"].strip().lower() in ("true", "1", "yes")
    if out.get("user_id") == "":
        out["user_id"] = None
    return out


def _load_rows(path: str) -> List[Dict]:
    import csv
    import io
    import json
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    stripped = text.strip()
    if not stripped:
        return []
    if path.endswith(".csv"):
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    if stripped.startswith("["):
        return [dict(x) for x in json.loads(stripped)]
    return [json.loads(line) for line in stripped.splitlines() if line.strip()]


def load_ground_truth(path: str) -> List[Dict]:
    return [_coerce_truth(r) for r in _load_rows(path)]


def load_events(path: str) -> List[Dict]:
    return [_coerce_event(r) for r in _load_rows(path)]


def load_identifies(path: str) -> List[Dict]:
    rows = _load_rows(path)
    for r in rows:
        if r.get("user_id") == "":
            r["user_id"] = None
    return rows


def load_consent(path: str) -> List[Dict]:
    return [_coerce_consent(r) for r in _load_rows(path)]


_TABLE_LOADERS = {
    "ground_truth": (load_ground_truth, GROUND_TRUTH_FIELDS),
    "pixel": (load_events, EVENT_FIELDS),
    "server": (load_events, EVENT_FIELDS),
    "identifies": (load_identifies, IDENTIFY_FIELDS),
    "consent": (load_consent, CONSENT_FIELDS),
}
TABLES = ["ground_truth", "pixel", "server", "identifies", "consent"]


def _table_path(directory: str, table: str) -> str:
    import os
    for ext in ("jsonl", "csv"):
        candidate = os.path.join(directory, f"{table}.{ext}")
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        f"no {table}.jsonl or {table}.csv in dataset directory: {directory}")


def load_dataset(directory: str) -> Dict[str, List[Dict]]:
    """Load a dataset directory written by ``write_dataset`` (or the CLI)."""
    return {table: _TABLE_LOADERS[table][0](_table_path(directory, table))
            for table in TABLES}


def write_dataset(dataset: Dict[str, List[Dict]], directory: str,
                  fmt: str = "jsonl") -> None:
    import os
    os.makedirs(directory, exist_ok=True)
    if fmt not in ("jsonl", "csv"):
        raise ValueError(f"unknown format: {fmt}")
    for table in TABLES:
        fields = _TABLE_LOADERS[table][1]
        if fmt == "jsonl":
            text = _to_jsonl(dataset[table])
        else:
            text = _to_csv(dataset[table], fields)
        with open(os.path.join(directory, f"{table}.{fmt}"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)
