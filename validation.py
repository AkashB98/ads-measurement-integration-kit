"""Data-quality gate for measurement datasets.

Returns a list of human-readable issue strings; an empty list means
the dataset is clean. Validation never raises on bad *data* — it
reports — so the CLI ``check`` gate can map issues to exit code 2.
(Raw ad-platform exports are famously messy; a pipeline that crashes
on the first malformed row never survives contact with one.)

Checked: required fields, channel vocabulary, timestamp
parseability, revenue >= 0, duplicate ground-truth ids, duplicate
consent rows, identify rows that link nothing, and event rows whose
source disagrees with the stream they arrived in. A null ``event_id``
is legal (it is the planted fallback case) — an empty-string id is
not, because loaders coerce "" to None and a surviving "" means the
row bypassed the loader.
"""

from __future__ import annotations

from typing import Dict, List

from generator import (CHANNELS, CONSENT_FIELDS, EVENT_FIELDS,
                       GROUND_TRUTH_FIELDS, IDENTIFY_FIELDS, parse_timestamp)

_TABLE_FIELDS = {"ground_truth": GROUND_TRUTH_FIELDS, "event": EVENT_FIELDS,
                 "identify": IDENTIFY_FIELDS, "consent": CONSENT_FIELDS}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_fields(row: Dict, kind: str, label: str) -> List[str]:
    issues = []
    for field in _TABLE_FIELDS[kind]:
        if field not in row:
            issues.append(f"{label}: missing field '{field}'")
    return issues


def _parses(ts) -> bool:
    try:
        parse_timestamp(ts)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def validate_event(event: Dict, stream: str, index: int = 0) -> List[str]:
    label = f"<{stream} row {index}>" if not event.get("event_id") \
        else str(event["event_id"])
    issues = _check_fields(event, "event", label)
    if issues:
        return issues
    if event.get("event_id") == "":
        issues.append(f"{label}: event_id must be null or non-empty")
    if event["source"] != stream:
        issues.append(f"{label}: source '{event['source']}' does not match "
                      f"stream '{stream}'")
    if event["source"] not in ("pixel", "server"):
        issues.append(f"{label}: invalid source '{event['source']}'")
    if event["channel"] not in CHANNELS:
        issues.append(f"{label}: invalid channel '{event['channel']}'")
    if not _parses(event["timestamp"]):
        issues.append(f"{label}: unparseable timestamp '{event['timestamp']}'")
    if not _is_number(event["revenue"]):
        issues.append(f"{label}: revenue must be a number")
    elif event["revenue"] < 0:
        issues.append(f"{label}: revenue must be >= 0")
    if not event.get("anon_id") and not event.get("user_id"):
        issues.append(f"{label}: event carries no identity (anon_id or user_id required)")
    return issues


def validate_ground_truth(row: Dict, index: int = 0) -> List[str]:
    label = row.get("conversion_id") or f"<ground_truth row {index}>"
    issues = _check_fields(row, "ground_truth", label)
    if issues:
        return issues
    if row["channel"] not in CHANNELS:
        issues.append(f"{label}: invalid channel '{row['channel']}'")
    if not _parses(row["timestamp"]):
        issues.append(f"{label}: unparseable timestamp '{row['timestamp']}'")
    if not _is_number(row["revenue"]):
        issues.append(f"{label}: revenue must be a number")
    elif row["revenue"] < 0:
        issues.append(f"{label}: revenue must be >= 0")
    if not isinstance(row["consented"], bool):
        issues.append(f"{label}: consented must be a boolean")
    return issues


def validate_dataset(dataset: Dict[str, List[Dict]]) -> List[str]:
    issues: List[str] = []
    truth = dataset.get("ground_truth", [])
    pixel = dataset.get("pixel", [])
    server = dataset.get("server", [])
    identifies = dataset.get("identifies", [])
    consent = dataset.get("consent", [])
    if not truth and not pixel and not server:
        issues.append("dataset is empty")
        return issues

    seen_conv, seen_evt = set(), set()
    for i, row in enumerate(truth):
        issues.extend(validate_ground_truth(row, index=i))
        cid, eid = row.get("conversion_id"), row.get("event_id")
        if cid is not None:
            if cid in seen_conv:
                issues.append(f"{cid}: duplicate conversion_id")
            seen_conv.add(cid)
        if eid is not None:
            if eid in seen_evt:
                issues.append(f"{eid}: duplicate ground-truth event_id")
            seen_evt.add(eid)

    for stream, rows in (("pixel", pixel), ("server", server)):
        for i, event in enumerate(rows):
            issues.extend(validate_event(event, stream, index=i))

    seen_consent = set()
    for i, row in enumerate(consent):
        label = row.get("anon_id") or f"<consent row {i}>"
        issues.extend(_check_fields(row, "consent", label))
        if "consented" in row and not isinstance(row["consented"], bool):
            issues.append(f"{label}: consented must be a boolean")
        key = row.get("anon_id")
        if key is not None:
            if key in seen_consent:
                issues.append(f"{key}: duplicate consent row")
            seen_consent.add(key)

    for i, row in enumerate(identifies):
        label = row.get("anon_id") or f"<identify row {i}>"
        issues.extend(_check_fields(row, "identify", label))
        if not row.get("anon_id") or not row.get("user_id"):
            issues.append(f"{label}: identify must link anon_id and user_id")
        if row.get("timestamp") and not _parses(row["timestamp"]):
            issues.append(f"{label}: unparseable timestamp '{row['timestamp']}'")
    return issues
