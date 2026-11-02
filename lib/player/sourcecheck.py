"""
A quick look at a channel's other sources, while one of them plays.

The IPTV catalogue gives a channel many addresses and no reliable verdict on
any of them. Checking every channel's every source ahead of time would be a
lot of requests for channels nobody will open, so the check is done for the
channel the person actually clicked -- and only for the sources that have no
real play on this box yet. It runs in parallel with the first source, so it
never delays the picture, and its answers are used only to choose what to try
next if that source fails or freezes.

These are hints, not plays: nothing here is written to the quality journal.
A playlist that answers may still fail in the player, and only a real play is
evidence about the channel's health.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

#: The UA VLC uses. A server that turns away an unknown client turns away the
#: probe for the wrong reason, and the hint would be worse than none.
_USER_AGENT = "VLC/3.0.21 LibVLC/3.0.21"

#: A handful at a time. The whole list of a channel is tens of small requests,
#: not the catalogue.
_WORKERS = 6

#: Per request. Long enough for a slow CDN, short enough that a dead host is
#: known while the first source is still starting.
_TIMEOUT = 3

#: How much of the answer to read. A playlist is small; a server that answers
#: with something else is not worth downloading.
_MAX_BODY = 256 * 1024


def _looks_playable(content_type, body):
    """
    Whether the answer looks like something the player can open.

    An m3u8 with no segments and no variants -- what a "Not 24/7" channel
    serves when it is off the air -- is not playable, even though it is a 200
    with the right content type. Anything else with a 200 is given the benefit
    of the doubt only when it looks like media rather than an error page.
    """
    text = body.decode("utf-8", "replace")
    if "#EXTM3U" in text:
        return "#EXTINF" in text or "#EXT-X-STREAM-INF" in text
    kind = (content_type or "").lower()
    return (
        kind.startswith("video/")
        or kind.startswith("audio/")
        or "mpegurl" in kind
        or "octet-stream" in kind
    )


class SourceCheck:
    """One channel's unrated sources, probed in parallel."""

    def __init__(self, urls):
        self.urls = [url for url in urls if url]
        self._lock = threading.Lock()
        self._wait = threading.Condition(self._lock)
        self._results = {}
        self._cancelled = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=_WORKERS)
        for url in self.urls:
            self._pool.submit(self._probe, url)

    def _probe(self, url):
        if self._cancelled.is_set():
            return
        started = time.time()
        ok = False
        response = None
        try:
            response = requests.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=(_TIMEOUT, _TIMEOUT),
                stream=True,
            )
            if response.status_code == 200:
                body = b""
                for chunk in response.iter_content(8192):
                    body += chunk
                    if len(body) >= _MAX_BODY:
                        break
                ok = _looks_playable(response.headers.get("Content-Type"), body)
        except Exception:
            ok = False
        finally:
            if response is not None:
                response.close()
        self._store(url, ok, time.time() - started)

    def _store(self, url, ok, seconds):
        with self._wait:
            if self._cancelled.is_set():
                return
            self._results[url] = (ok, seconds)
            self._wait.notify_all()

    def result(self, url):
        """True, False, or None while it is still in flight."""
        with self._lock:
            found = self._results.get(url)
            return None if found is None else found[0]

    def seconds(self, url):
        with self._lock:
            found = self._results.get(url)
            return None if found is None else found[1]

    def good(self):
        """The sources that answered a playlist, fastest first."""
        with self._lock:
            found = [url for url, (ok, _s) in self._results.items() if ok]
            return sorted(found, key=lambda url: self._results[url][1])

    def done(self):
        with self._lock:
            return len(self._results) >= len(self.urls)

    def wait(self, timeout):
        """
        Wait until a good source is known, every probe has answered, or time.

        Used only when the first source failed instantly: a second of waiting
        can save trying a dead URL, but the person is already waiting and this
        must not become another timeout of its own.
        """
        deadline = time.time() + timeout
        with self._wait:
            while time.time() < deadline:
                if self._cancelled.is_set():
                    break
                if any(ok for ok, _s in self._results.values()):
                    break
                if len(self._results) >= len(self.urls):
                    break
                self._wait.wait(deadline - time.time())
        return self.good()

    def cancel(self):
        self._cancelled.set()
        with self._wait:
            self._wait.notify_all()
        self._pool.shutdown(wait=False, cancel_futures=True)
