"""
The CEC remote listener, tested without a TV, a Pi or /dev/cec0.

The module is imported directly, as test_bt_serve does, so it is checked as the
file that runs on the box.
"""

import importlib.util
import json
import pathlib
import struct

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "cec_listener.py"


@pytest.fixture(scope="module")
def cl():
    spec = importlib.util.spec_from_file_location("cec_listener", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEVICES = """\
I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="vc4-hdmi"
P: Phys=vc4-hdmi/input0
S: Sysfs=/devices/platform/soc/3f902000.hdmi/rc/rc0/input4
U: Uniq=
H: Handlers=kbd event4
B: PROP=20

N: Name="vc4-hdmi HDMI Jack"
P: Phys=ALSA
H: Handlers=event5
B: PROP=0
"""


class TestKeyMapping:
    @pytest.mark.parametrize(
        "code, action",
        [
            (200, "pause"),     # play
            (201, "pause"),     # pause
            (352, "pause"),     # select
            (105, "minus30"),   # left
            (106, "plus30"),    # right
            (103, "plus600"),   # up
            (108, "minus600"),  # down
            (174, "stop"),      # back/exit
        ],
    )
    def test_each_forwarded_key_maps_to_its_action(self, cl, code, action):
        assert cl.action_for(1, code, 1) == action

    def test_a_key_release_does_nothing(self, cl):
        assert cl.action_for(1, 200, 0) is None

    def test_a_key_repeat_does_nothing(self, cl):
        assert cl.action_for(1, 200, 2) is None

    def test_an_unmapped_key_does_nothing(self, cl):
        """Colour keys and numbers are not mapped yet."""
        assert cl.action_for(1, 398, 1) is None
        assert cl.action_for(1, 2, 1) is None

    def test_a_non_key_event_does_nothing(self, cl):
        assert cl.action_for(2, 103, 1) is None

    def test_every_mapped_action_exists_in_the_player(self, cl):
        """Each action name must be one the player's control() understands."""
        known = {
            "pause", "resume", "stop", "plus30", "minus30",
            "plus600", "minus600", "volup", "voldown", "mute",
        }
        assert set(cl.ACTIONS.values()) <= known


class TestEventFormat:
    def test_the_layout_matches_the_platform(self, cl):
        """A 64-bit kernel uses 24-byte events; a 32-bit one uses 16."""
        assert cl.EVENT.size == struct.calcsize("@llHHi")
        assert cl.EVENT.size in (16, 24)

    def test_an_event_round_trips(self, cl):
        raw = cl.EVENT.pack(10, 20, 1, 352, 1)
        sec, usec, event_type, code, value = cl.EVENT.unpack(raw)
        assert (sec, usec, event_type, code, value) == (10, 20, 1, 352, 1)


class TestFindInputDevice:
    def test_finds_the_cec_remote_by_name(self, cl):
        assert cl.find_input_device(DEVICES) == "/dev/input/event4"

    def test_does_not_pick_the_audio_jack(self, cl):
        text = DEVICES.split("\n\n")[1]
        assert cl.find_input_device(text) is None

    def test_no_device_is_none(self, cl):
        assert cl.find_input_device("") is None


class TestPhysicalAddress:
    def test_reads_the_address_the_adapter_reports(self, cl):
        text = "\tPhysical Address           : 1.0.0.0\n"
        assert cl.physical_address(text) == "1.0.0.0"

    def test_an_unset_address_is_none(self, cl):
        assert cl.physical_address("\tPhysical Address : f.f.f.f\n") is None

    def test_missing_line_is_none(self, cl):
        assert cl.physical_address("nothing here") is None


class TestOptIn:
    def test_disabled_when_the_setting_is_missing(self, cl, tmp_path):
        assert cl.enabled(str(tmp_path / "cec.json")) is False

    def test_disabled_when_the_setting_is_false(self, cl, tmp_path):
        path = tmp_path / "cec.json"
        path.write_text(json.dumps({"enabled": False}))
        assert cl.enabled(str(path)) is False

    def test_enabled_when_the_setting_is_true(self, cl, tmp_path):
        path = tmp_path / "cec.json"
        path.write_text(json.dumps({"enabled": True}))
        assert cl.enabled(str(path)) is True

    def test_a_broken_file_is_disabled(self, cl, tmp_path):
        path = tmp_path / "cec.json"
        path.write_text("{not json")
        assert cl.enabled(str(path)) is False

    def test_main_exits_cleanly_when_off(self, cl, tmp_path):
        """Off means exit 0 at once, so the boot unit can stay installed."""
        assert cl.main(["--settings", str(tmp_path / "cec.json")]) == 0
