"""Reconciliation: pixel stream + server stream -> one true count.

The pipeline runs four documented stages, in order. Every raw event
ends with exactly one audit record (``disposition`` + ``reason_code``),
and every unified conversion records how it was formed — nothing is
merged, kept, or dropped silently.

Stage 1 — duplicate deliveries. Within one stream, a second event
with the same non-null ``event_id`` is the same delivery retried:
kept once, the copies dropped as ``DUPLICATE_DELIVERY``. An id-less
event has no id to key on, so its duplicate is recognised as a
byte-identical row (same identity, timestamp, channel and revenue) —
two genuinely distinct conversions are never byte-identical here
because their identities or timestamps differ.

Stage 2 — exact-id merge. A pixel event and a server event carrying
the same non-null ``event_id`` describe the same conversion (the
event_id dedupe pattern pixel + conversion-API integrations use):
merged into one unified conversion, reason ``MERGED_EXACT_ID``.
Identity is not needed for this stage — the shared id *is* the link.

Stage 3 — fallback merge. A remaining server event with *no* event_id
can still be recovered when it agrees with an unmatched pixel event
on all three of: the same resolved identity key (see stitching
below), a timestamp within ``FALLBACK_WINDOW_SECONDS`` of the pixel
event (the window exists because the server stream runs minutes
behind the pixel stream — planted skew), and the same revenue and
channel. Candidates pair greedily by smallest timestamp gap, one to
one. Reason ``MERGED_FALLBACK_WINDOW``. If any condition fails, no
merge happens — a near miss stays two conversions rather than a
guessed one.

Stage 4 — singletons. Whatever is left was only ever seen by one
stream: ``KEPT_PIXEL_ONLY`` (the server event was lost) or
``KEPT_SERVER_ONLY`` (the pixel was blocked). They are real
conversions and they count — once.

Consent gating (in ``apply_consent_gate``) is a separate stage on
purpose: dedupe answers "how many distinct conversions were
observed?", gating answers "how many of those may we count?". A
unified conversion whose person did not consent — or has no consent
record at all (fail-closed) — is dropped with ``DROPPED_NO_CONSENT``
and *counted* in the reason tally, never silently removed and never
counted as a conversion.

Identity stitching is DETERMINISTIC ONLY, as a deliberate privacy
choice: an anonymous id resolves to a user id only when an explicit
login / identify event links them. There is no probabilistic
matching — no "probably the same person" from device, timing, or
behaviour — anywhere in this kit.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from generator import parse_timestamp

FALLBACK_WINDOW_SECONDS = 900  # 15 minutes: covers the planted skew

REASON_DUPLICATE = "DUPLICATE_DELIVERY"
REASON_EXACT = "MERGED_EXACT_ID"
REASON_FALLBACK = "MERGED_FALLBACK_WINDOW"
REASON_PIXEL_ONLY = "KEPT_PIXEL_ONLY"
REASON_SERVER_ONLY = "KEPT_SERVER_ONLY"
REASON_NO_CONSENT = "DROPPED_NO_CONSENT"
ALL_REASONS = [REASON_DUPLICATE, REASON_EXACT, REASON_FALLBACK,
               REASON_PIXEL_ONLY, REASON_SERVER_ONLY, REASON_NO_CONSENT]


def build_identity_map(identifies: List[Dict]) -> Dict[str, str]:
    """``{anon_id: user_id}`` from explicit identify events only."""
    mapping: Dict[str, str] = {}
    for row in identifies:
        anon, user = row.get("anon_id"), row.get("user_id")
        if anon and user:
            mapping[anon] = user
    return mapping


def resolve_user_id(event: Dict, identity_map: Dict[str, str]) -> Optional[str]:
    """The user id an event can deterministically claim, or None."""
    if event.get("user_id"):
        return event["user_id"]
    anon = event.get("anon_id")
    if anon and anon in identity_map:
        return identity_map[anon]
    return None


def identity_key(event: Dict, identity_map: Dict[str, str]) -> str:
    """Stable person key: user id when known, else the anonymous id."""
    user = resolve_user_id(event, identity_map)
    if user:
        return f"user:{user}"
    return f"anon:{event.get('anon_id')}"


def build_consent_index(consent: List[Dict]) -> Dict[str, bool]:
    """Consent lookup by both user id and anonymous id."""
    index: Dict[str, bool] = {}
    for row in consent:
        value = row.get("consented") is True
        if row.get("user_id"):
            index[f"user:{row['user_id']}"] = value
        if row.get("anon_id"):
            index[f"anon:{row['anon_id']}"] = value
    return index


def is_consented(unified: Dict, consent_index: Dict[str, bool]) -> bool:
    """Fail-closed: no consent record means not consented."""
    if unified.get("user_id") and f"user:{unified['user_id']}" in consent_index:
        return consent_index[f"user:{unified['user_id']}"]
    if unified.get("anon_id") and f"anon:{unified['anon_id']}" in consent_index:
        return consent_index[f"anon:{unified['anon_id']}"]
    return False


def _ref(source: str, idx: int, event: Dict) -> str:
    return f"{source}[{idx}]:{event.get('event_id') or 'no-id'}"


def _unified(members: List[Dict], reason: str,
             identity_map: Dict[str, str]) -> Dict:
    """Build one unified conversion from 1–2 stream events.

    The pixel event (when present) supplies the timestamp — it sits
    closest to the true conversion time; the server event runs behind.
    """
    pixel = next((e for e in members if e["source"] == "pixel"), None)
    base = pixel or members[0]
    user_ids = {resolve_user_id(e, identity_map) for e in members}
    user_ids.discard(None)
    anon_ids = {e.get("anon_id") for e in members}
    anon_ids.discard(None)
    return {
        "conversion_key": base.get("event_id") or
                          f"FB-{identity_key(base, identity_map)}-{base['timestamp']}",
        "event_id": base.get("event_id"),
        "identity_key": identity_key(base, identity_map),
        "anon_id": sorted(anon_ids)[0] if anon_ids else None,
        "user_id": sorted(user_ids)[0] if user_ids else None,
        "timestamp": base["timestamp"],
        "channel": base["channel"],
        "revenue": float(base["revenue"]),
        "sources": sorted({e["source"] for e in members}),
        "match_reason": reason,
    }


def reconcile(pixel: List[Dict], server: List[Dict],
              identifies: List[Dict]) -> Dict:
    """Dedupe/merge the two streams (stages 1–4, before consent gating).

    Returns ``{"unified": [...], "audit": [...], "reason_counts": {...}}``
    where audit holds one record per raw event, in stream order
    (pixel first, then server), each ``{ref, source, event_id,
    disposition, reason_code}`` with disposition in
    merged / kept / dropped.
    """
    identity_map = build_identity_map(identifies)
    audit: Dict[str, Dict] = {}
    reason_counts: Dict[str, int] = {r: 0 for r in ALL_REASONS}

    def mark(source: str, idx: int, event: Dict, disposition: str,
             reason: str) -> None:
        audit[_ref(source, idx, event)] = {
            "ref": _ref(source, idx, event), "source": source,
            "event_id": event.get("event_id"), "disposition": disposition,
            "reason_code": reason}
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    # Stage 1: within-stream duplicate deliveries (same non-null id,
    # or a byte-identical id-less row).
    def _dup_key(event: Dict) -> str:
        if event.get("event_id") is not None:
            return f"id:{event['event_id']}"
        return ("row:" + "|".join(str(event.get(k)) for k in
                ("anon_id", "user_id", "timestamp", "channel", "revenue")))

    def dedupe_stream(events: List[Dict], source: str):
        seen: Dict[str, int] = {}
        kept: List[Tuple[int, Dict]] = []
        for idx, event in enumerate(events):
            key = _dup_key(event)
            if key in seen:
                mark(source, idx, event, "dropped", REASON_DUPLICATE)
            else:
                seen[key] = idx
                kept.append((idx, event))
        return kept

    pixel_kept = dedupe_stream(pixel, "pixel")
    server_kept = dedupe_stream(server, "server")

    unified: List[Dict] = []
    used_pixel: set = set()
    used_server: set = set()

    # Stage 2: exact event_id merge across streams.
    server_by_id = {e["event_id"]: (i, e) for i, e in server_kept
                    if e.get("event_id") is not None}
    for p_idx, p_event in pixel_kept:
        eid = p_event.get("event_id")
        if eid is not None and eid in server_by_id:
            s_idx, s_event = server_by_id[eid]
            unified.append(_unified([p_event, s_event], REASON_EXACT,
                                    identity_map))
            mark("pixel", p_idx, p_event, "merged", REASON_EXACT)
            mark("server", s_idx, s_event, "merged", REASON_EXACT)
            used_pixel.add(p_idx)
            used_server.add(s_idx)

    # Stage 3: fallback — id-less server events vs unmatched pixel events.
    candidates: List[Tuple[float, int, int]] = []
    open_pixel = [(i, e) for i, e in pixel_kept if i not in used_pixel]
    open_server_noid = [(i, e) for i, e in server_kept
                        if i not in used_server and e.get("event_id") is None]
    for p_idx, p_event in open_pixel:
        p_key = identity_key(p_event, identity_map)
        p_ts = parse_timestamp(p_event["timestamp"])
        for s_idx, s_event in open_server_noid:
            if identity_key(s_event, identity_map) != p_key:
                continue
            if float(s_event["revenue"]) != float(p_event["revenue"]):
                continue
            if s_event["channel"] != p_event["channel"]:
                continue
            gap = abs((parse_timestamp(s_event["timestamp"]) - p_ts)
                      .total_seconds())
            if gap <= FALLBACK_WINDOW_SECONDS:
                candidates.append((gap, p_idx, s_idx))
    candidates.sort()
    fb_pixel: set = set()
    fb_server: set = set()
    pixel_by_idx = dict(pixel_kept)
    server_by_idx = dict(server_kept)
    for _gap, p_idx, s_idx in candidates:
        if p_idx in fb_pixel or s_idx in fb_server:
            continue
        fb_pixel.add(p_idx)
        fb_server.add(s_idx)
        unified.append(_unified([pixel_by_idx[p_idx], server_by_idx[s_idx]],
                                REASON_FALLBACK, identity_map))
        mark("pixel", p_idx, pixel_by_idx[p_idx], "merged", REASON_FALLBACK)
        mark("server", s_idx, server_by_idx[s_idx], "merged", REASON_FALLBACK)
        used_pixel.add(p_idx)
        used_server.add(s_idx)

    # Stage 4: singletons.
    for p_idx, p_event in pixel_kept:
        if p_idx not in used_pixel:
            unified.append(_unified([p_event], REASON_PIXEL_ONLY, identity_map))
            mark("pixel", p_idx, p_event, "kept", REASON_PIXEL_ONLY)
    for s_idx, s_event in server_kept:
        if s_idx not in used_server:
            unified.append(_unified([s_event], REASON_SERVER_ONLY, identity_map))
            mark("server", s_idx, s_event, "kept", REASON_SERVER_ONLY)

    unified.sort(key=lambda u: (u["timestamp"], u["conversion_key"]))
    ordered_audit = [audit[_ref("pixel", i, e)] for i, e in enumerate(pixel)]
    ordered_audit += [audit[_ref("server", i, e)] for i, e in enumerate(server)]
    return {"unified": unified, "audit": ordered_audit,
            "reason_counts": reason_counts}


def apply_consent_gate(unified: List[Dict],
                       consent: List[Dict]) -> Dict:
    """Split unified conversions into counted vs dropped-no-consent.

    Returns ``{"counted": [...], "dropped": [...]}``; every dropped
    record is the unified conversion plus ``reason_code`` so the drop
    is auditable and tallied, never silent.
    """
    index = build_consent_index(consent)
    counted: List[Dict] = []
    dropped: List[Dict] = []
    for conv in unified:
        if is_consented(conv, index):
            counted.append(conv)
        else:
            dropped.append({**conv, "reason_code": REASON_NO_CONSENT})
    return {"counted": counted, "dropped": dropped}
