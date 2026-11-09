"""Shared builders for the test suite (not a test module)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pixel_event(event_id="EVT-T0001", anon_id="ANON-0001", user_id=None,
                timestamp="2026-01-10T12:00:00Z", channel="paid_search",
                revenue=100.0):
    return {"event_id": event_id, "source": "pixel", "anon_id": anon_id,
            "user_id": user_id, "timestamp": timestamp, "channel": channel,
            "revenue": revenue}


def server_event(event_id="EVT-T0001", anon_id=None, user_id="HH-U00001",
                 timestamp="2026-01-10T12:05:00Z", channel="paid_search",
                 revenue=100.0):
    return {"event_id": event_id, "source": "server", "anon_id": anon_id,
            "user_id": user_id, "timestamp": timestamp, "channel": channel,
            "revenue": revenue}


def identify(anon_id="ANON-0001", user_id="HH-U00001",
             timestamp="2026-01-10T11:00:00Z"):
    return {"anon_id": anon_id, "user_id": user_id, "timestamp": timestamp}


def consent_row(anon_id="ANON-0001", user_id="HH-U00001", consented=True):
    return {"anon_id": anon_id, "user_id": user_id, "consented": consented}


def truth_row(conversion_id="CNV-T0001", event_id="EVT-T0001",
              anon_id="ANON-0001", user_id="HH-U00001",
              timestamp="2026-01-10T12:00:00Z", channel="paid_search",
              revenue=100.0, consented=True):
    return {"conversion_id": conversion_id, "event_id": event_id,
            "anon_id": anon_id, "user_id": user_id, "timestamp": timestamp,
            "channel": channel, "revenue": revenue, "consented": consented}


def dataset(pixel=None, server=None, truth=None, identifies=None, consent=None):
    return {"ground_truth": truth or [], "pixel": pixel or [],
            "server": server or [], "identifies": identifies or [],
            "consent": consent or []}
