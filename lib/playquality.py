"""
What this box has seen of the streams it played.

The IPTV catalogue is a list of URLs, and most of them are not ours. A channel
can have thirty-five mirrors, and the first one is often dead; a stream that
worked an hour ago can be gone now. Nothing upstream says which of them are
alive -- the `quality` field is a resolution, not a verdict, and the labels
("Geo-blocked", "Not 24/7") are hints that go stale -- so the only honest
source of a reliability mark is what happened here.

The player writes one entry per URL when a picture starts, when a play fails,
and when the picture stops moving. The IPTV channel reads them to mark and
order its rows. Everything is timestamped and shown with its age: "failed 10m
ago" is a fact about this box, not a claim that the stream is dead.

Kept in data/ rather than in a database because it is one small dictionary
read on every feed request and written a few times per play.
"""

import json
import os
import threading
import time

from .locations import DATA_PATH

#: How long an outcome is kept. A stream that worked last month says nothing
#: about the URL there now, and the file is meant to stay small.
KEEP_SECONDS = 30 * 24 * 60 * 60

#: A ceiling for the file even if a box plays more streams than this. The
#: oldest entries go first.
MAX_ENTRIES = 2000

#: The failure reason is for the log, not the row, so it is only capped.
MAX_REASON = 200


class Journal:
    """One file of outcomes, keyed by stream URL."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._loaded = False
        self._entries = {}

    def record_ok(self, url, seconds):
        if not url:
            return
        with self._lock:
            entry = self._entry(url)
            entry["ok_at"] = time.time()
            entry["ok_seconds"] = round(float(seconds), 1)
            self._save()

    def record_failed(self, url, reason=None):
        if not url:
            return
        with self._lock:
            entry = self._entry(url)
            entry["bad_at"] = time.time()
            entry["bad_reason"] = str(reason or "")[:MAX_REASON]
            self._save()

    def record_froze(self, url):
        if not url:
            return
        with self._lock:
            entry = self._entry(url)
            entry["froze_at"] = time.time()
            self._save()

    def summary(self, url):
        with self._lock:
            self._load()
            entry = self._entries.get(url)
            return dict(entry) if entry else None

    def summaries(self, urls):
        with self._lock:
            self._load()
            return {
                url: dict(self._entries[url])
                for url in urls
                if url in self._entries
            }

    def _entry(self, url):
        self._load()
        return self._entries.setdefault(url, {})

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # Absent is the normal case on a new card, and a corrupt file is
            # not worth failing a play over. Both mean "nothing known".
            return
        if isinstance(data, dict):
            self._entries = {
                str(url): entry
                for url, entry in data.items()
                if isinstance(entry, dict)
            }

    def _save(self):
        now = time.time()
        kept = {
            url: entry
            for url, entry in self._entries.items()
            if self._newest(entry) is not None
            and now - self._newest(entry) < KEEP_SECONDS
        }
        if len(kept) > MAX_ENTRIES:
            newest = sorted(
                kept.items(),
                key=lambda pair: self._newest(pair[1]) or 0,
                reverse=True,
            )[:MAX_ENTRIES]
            kept = dict(newest)
        self._entries = kept
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self._entries, fh)
            os.replace(tmp, self.path)
        except OSError:
            # A full or read-only card must not fail a play. The mark is a
            # convenience; the film is the point.
            return

    @staticmethod
    def _newest(entry):
        stamps = [
            entry.get("ok_at"),
            entry.get("bad_at"),
            entry.get("froze_at"),
        ]
        stamps = [s for s in stamps if isinstance(s, (int, float))]
        return max(stamps) if stamps else None


_DEFAULT = Journal(os.path.join(DATA_PATH, "play-quality.json"))


def record_ok(url, seconds):
    _DEFAULT.record_ok(url, seconds)


def record_failed(url, reason=None):
    _DEFAULT.record_failed(url, reason)


def record_froze(url):
    _DEFAULT.record_froze(url)


def summary(url):
    return _DEFAULT.summary(url)


def summaries(urls):
    return _DEFAULT.summaries(urls)
