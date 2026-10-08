"""
Torrent URL helpers.

These back the "bf_torr_idx" query parameter, which is how BlissFlixx records
which file inside a torrent to play. A round trip through these functions has to
be lossless, otherwise playback silently starts the wrong file.
"""

import pytest

from lib.api.torrent import is_torrent_url, set_torridx, torrent2magnet, torrent_files
from lib.chanutils.torrent import is_main, is_torrent, torrent_idx

HASH = "A" * 40


class TestIsTorrent:
    def test_torrent_extension(self):
        assert is_torrent("http://example.com/file.torrent")

    def test_magnet(self):
        assert is_torrent("magnet:?xt=urn:btih:" + HASH)

    def test_html_page_is_not_a_torrent(self):
        assert not is_torrent("http://example.com/watch?v=abc")

    def test_path_ending_in_torrent_ignores_query(self):
        assert is_torrent("http://example.com/x.torrent?bf_torr_idx=2")


class TestSetTorridx:
    def test_adds_param_to_clean_url(self):
        assert set_torridx("http://x/f.torrent", 3) == (
            "http://x/f.torrent?bf_torr_idx=3"
        )

    def test_appends_to_url_with_existing_query(self):
        assert set_torridx("http://x/f.torrent?a=1", 3) == (
            "http://x/f.torrent?a=1&bf_torr_idx=3"
        )

    def test_replaces_existing_index_without_duplicating(self):
        first = set_torridx("http://x/f.torrent", 3)
        second = set_torridx(first, 7)
        assert second == "http://x/f.torrent?bf_torr_idx=7"
        assert second.count("bf_torr_idx=") == 1

    def test_main_index_is_minus_one(self):
        assert set_torridx("http://x/f.torrent") == "http://x/f.torrent?bf_torr_idx=-1"

    def test_preserves_other_params_when_reindexing(self):
        url = set_torridx("http://x/f.torrent?token=abc", 1)
        url = set_torridx(url, 5)
        assert "token=abc" in url
        assert torrent_idx(url) == 5

    def test_works_on_magnet(self):
        magnet = "magnet:?xt=urn:btih:" + HASH
        assert torrent_idx(set_torridx(magnet, 4)) == 4


class TestTorrentIdx:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("http://x/f.torrent", None),
            ("http://x/f.torrent?bf_torr_idx=0", 0),
            ("http://x/f.torrent?a=b&bf_torr_idx=12", 12),
            ("http://x/f.torrent?bf_torr_idx=-1", -1),
        ],
    )
    def test_extract(self, url, expected):
        assert torrent_idx(url) == expected

    def test_is_main_only_for_explicit_minus_one(self):
        assert is_main("http://x/f.torrent?bf_torr_idx=-1")
        assert not is_main("http://x/f.torrent?bf_torr_idx=0")
        assert not is_main("http://x/f.torrent")


class TestIsTorrentUrl:
    def test_detects_marker(self):
        assert is_torrent_url("http://x/f.torrent?bf_torr_idx=1")

    def test_rejects_plain_url(self):
        assert not is_torrent_url("http://x/f.torrent")


class TestTorrent2Magnet:
    def test_leaves_magnet_untouched(self):
        magnet = "magnet:?xt=urn:btih:" + HASH
        assert torrent2magnet(magnet) == magnet

    def test_builds_magnet_from_hash_with_trackers(self):
        magnet = torrent2magnet(HASH)
        assert magnet.startswith("magnet:?xt=urn:btih:")
        assert HASH in magnet
        assert "&tr=" in magnet

    def test_passes_through_input_without_a_hash(self):
        assert torrent2magnet("http://example.com/not-a-hash") == (
            "http://example.com/not-a-hash"
        )


class TestTorrentFiles:
    """
    peerflix is a node program installed by configure.sh and only present on
    the Pi, so subprocess.check_output is stubbed. What is under test is how
    lib.api.torrent.peerflix_metadata parses the -l listing.

    That parser uses fixed column offsets rather than splitting:

        name = line[20 : delim - 6]
        size = line[delim + 7 : -5]

    so it only works if peerflix's output layout stays exactly as it is. These
    tests build their input to those offsets, which documents the assumption.
    If peerflix ever changes its formatting, this is where it will show up.
    """

    @staticmethod
    def _peerflix_line(name, size):
        return "P" * 20 + name + " " * 6 + ":" + " " * 6 + size + "S" * 5

    def test_parses_name_and_size(self, monkeypatch):
        line = self._peerflix_line("Movie.Name.2020.1080p.mkv", "2147483648")

        def fake_check_output(cmd, **kwargs):
            assert cmd[0] == "peerflix"
            assert "-l" in cmd
            return (line + "\n").encode("utf-8")

        monkeypatch.setattr("subprocess.check_output", fake_check_output)
        files = torrent_files(HASH)
        assert len(files) == 1
        assert files[0][0] == "Movie.Name.2020.1080p.mkv"
        assert files[0][1] == "2147483648"

    def test_skips_verifying_lines_and_stops_at_malformed_line(self, monkeypatch):
        good1 = self._peerflix_line("a.mkv", "100")
        good2 = self._peerflix_line("b.mkv", "200")
        output = "Verifying downloaded: 100% (2 of 2)\n" + good1 + "\n" + good2 + "\n"

        monkeypatch.setattr(
            "subprocess.check_output",
            lambda cmd, **kw: output.encode("utf-8"),
        )
        files = torrent_files(HASH)
        assert [f[0] for f in files] == ["a.mkv", "b.mkv"]

    def test_magnet_is_converted_before_calling_peerflix(self, monkeypatch):
        seen = {}

        def fake_check_output(cmd, **kwargs):
            seen["cmd"] = cmd
            return b""

        monkeypatch.setattr("subprocess.check_output", fake_check_output)
        torrent_files(HASH)
        assert seen["cmd"][1].startswith("magnet:?xt=urn:btih:")
