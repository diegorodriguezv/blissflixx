"""
bin/bt_serve.py

The HTTP and piece arithmetic that decides what a play costs. Everything that
needs libtorrent is exercised on the box; what is testable here is the part that
is easy to get wrong and expensive to get wrong in production: which bytes a
Range header asks for, which pieces a reader's position makes urgent, and which
file an out-of-range index falls back to.

The module is imported directly, which also proves it can be read without a
libtorrent install: the import of libtorrent is inside load_libtorrent().
"""

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "bt_serve.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bt_serve", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bt_serve"] = module
    spec.loader.exec_module(module)
    return module


bt = load_module()

SIZE = 1000


class TestRange:
    def test_no_header_is_the_whole_file(self):
        assert bt.parse_range(None, SIZE) is None
        assert bt.parse_range("", SIZE) is None

    def test_open_ended_range_is_to_the_end(self):
        """What VLC asks for when it opens a film."""
        assert bt.parse_range("bytes=0-", SIZE) == (0, SIZE - 1)
        assert bt.parse_range("bytes=100-", SIZE) == (100, SIZE - 1)

    def test_closed_range(self):
        assert bt.parse_range("bytes=100-199", SIZE) == (100, 199)

    def test_end_past_the_file_is_clamped(self):
        assert bt.parse_range("bytes=900-99999", SIZE) == (900, SIZE - 1)

    def test_suffix_range(self):
        """The last N bytes, which is how an index at EOF can be read alone."""
        assert bt.parse_range("bytes=-500", SIZE) == (SIZE - 500, SIZE - 1)

    def test_suffix_longer_than_the_file_is_the_whole_file(self):
        assert bt.parse_range("bytes=-5000", SIZE) == (0, SIZE - 1)

    def test_a_range_starting_at_the_length_is_unsatisfiable(self):
        """
        VLC's size probe: a range that begins exactly at the length. Answering
        200 would stream the whole film to a client that wanted one number;
        416 carries the size in Content-Range, which is what the player uses.
        """
        with pytest.raises(bt.Unsatisfiable):
            bt.parse_range("bytes=1000-", SIZE)

    def test_a_range_past_the_end_is_unsatisfiable(self):
        with pytest.raises(bt.Unsatisfiable):
            bt.parse_range("bytes=1000-1010", SIZE)

    def test_a_backwards_range_is_unsatisfiable(self):
        with pytest.raises(bt.Unsatisfiable):
            bt.parse_range("bytes=200-100", SIZE)

    def test_only_the_first_of_several_ranges_is_served(self):
        """Multi-range responses need multipart bodies; no player asks for one."""
        assert bt.parse_range("bytes=0-9,20-29", SIZE) == (0, 9)

    def test_other_units_are_ignored(self):
        assert bt.parse_range("items=0-9", SIZE) is None

    def test_rubbish_is_ignored_rather_than_failing(self):
        """HTTP says an unparseable Range is ignored, not rejected."""
        assert bt.parse_range("bytes=abc", SIZE) is None
        assert bt.parse_range("bytes=", SIZE) is None


class TestWindow:
    def test_a_window_is_the_piece_and_the_seven_after_it(self):
        assert bt.window_for(10, 100) == (10, 17)

    def test_a_window_at_the_end_is_clamped(self):
        assert bt.window_for(98, 100) == (98, 100)

    def test_a_position_past_the_end_is_the_last_piece(self):
        """A reader cannot get here -- stream() clamps -- but a bad offset must
        not produce a window outside the torrent."""
        assert bt.window_for(500, 100) == (100, 100)

    def test_a_negative_position_clamps_to_the_first(self):
        assert bt.window_for(-5, 100) == (0, 7)

    def test_a_one_piece_torrent_is_one_piece(self):
        assert bt.window_for(0, 0) == (0, 0)


class TestFileChoice:
    def test_the_index_asked_for_is_played(self):
        assert bt.pick_file(3, 2) == (2, False)

    def test_no_index_plays_the_first_file(self):
        assert bt.pick_file(3, None) == (0, False)
        assert bt.pick_file(3, -1) == (0, False)

    def test_an_index_past_the_end_falls_back_and_says_so(self):
        """
        A saved play of a pack whose file list has changed. Serving nothing is
        the old source's failure; the first file is at least a film.
        """
        assert bt.pick_file(3, 9) == (0, True)


class TestResumeNames:
    def test_metadata_and_resume_live_together_under_the_buffer(self):
        torrent, resume = bt.resume_names("/data/downloads", "abc123")
        assert torrent == "/data/downloads/.bf-torrent/abc123.torrent"
        assert resume == "/data/downloads/.bf-torrent/abc123.fastresume"

    def test_the_directory_is_hidden_so_the_buffer_reads_as_downloads(self):
        torrent, _resume = bt.resume_names("/data/downloads", "abc123")
        assert pathlib.Path(torrent).parent.name.startswith(".")


class FakeHashes:
    def __init__(self, v1=None, v2=None):
        self.v1, self.v2 = v1, v2

    def has_v1(self):
        return self.v1 is not None


class TestInfoHash:
    def test_v1_is_preferred(self):
        assert bt.hash_text(FakeHashes("aa", "bb")) == "aa"

    def test_a_v2_only_torrent_uses_its_v2_hash(self):
        assert bt.hash_text(FakeHashes(None, "bb")) == "bb"


class FakeError:
    def __init__(self, value, message=""):
        self._value = value
        self._message = message

    def value(self):
        return self._value

    def message(self):
        return self._message


class TestErrorText:
    def test_success_is_silence(self):
        """An error_code with value 0 is truthy in this binding, so it must be
        read by value or every successful read_piece would look like a failure."""
        assert bt.error_text(FakeError(0)) == ""

    def test_a_failure_says_what_happened(self):
        assert bt.error_text(FakeError(2, "no such file")) == "no such file"

    def test_a_failure_without_a_message_still_names_its_code(self):
        assert bt.error_text(FakeError(7)) == "error 7"

    def test_nothing_at_all_is_silence(self):
        assert bt.error_text(None) == ""


class TestAtomicWrite:
    def test_creates_the_directory_it_holds(self, tmp_path):
        """
        The resume directory lives under the buffer and does not exist on a
        first play. Failing here lost the .torrent and then, on the periodic
        save, killed the process and the stream with it.
        """
        target = tmp_path / "downloads" / ".bf-torrent" / "abc.torrent"
        bt.atomic_write(str(target), b"metadata")
        assert target.read_bytes() == b"metadata"

    def test_an_old_file_is_replaced_whole(self, tmp_path):
        target = tmp_path / "abc.fastresume"
        bt.atomic_write(str(target), b"one")
        bt.atomic_write(str(target), b"two")
        assert target.read_bytes() == b"two"
        assert not (tmp_path / "abc.fastresume.part").exists()


class TestPriorities:
    def test_only_the_played_file_is_wanted(self):
        assert bt.file_priorities(3, 1, []) == [0, 1, 0]

    def test_the_subtitles_are_wanted_too(self):
        assert bt.file_priorities(4, 2, [3]) == [0, 0, 1, 1]

    def test_a_file_named_out_of_range_is_ignored(self):
        assert bt.file_priorities(2, 9, [7]) == [0, 0]

    def test_the_played_file_stays_wanted_though_its_pieces_do_not(self):
        """
        The file priority is what makes libtorrent write the film into its own
        file instead of a hidden part file. Switching it off to control pieces
        one by one left a play that worked and no film on the card.
        """
        assert bt.file_priorities(2, 0, [])[0] == 1

    def test_no_pieces_are_wanted_without_subtitles(self):
        assert bt.subtitle_pieces(4, []) == [0, 0, 0, 0]

    def test_only_the_subtitle_ranges_are_wanted(self):
        assert bt.subtitle_pieces(10, [(6, 7)]) == [0, 0, 0, 0, 0, 0, 1, 1, 0, 0]

    def test_ranges_are_clamped_to_the_torrent(self):
        assert bt.subtitle_pieces(3, [(2, 9)]) == [0, 0, 1]
        assert bt.subtitle_pieces(3, [(-2, 0)]) == [1, 0, 0]
