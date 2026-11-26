"""Subtitle language codes and their names.

OpenSubtitles uses three-letter codes; this is the list a channel can choose a
default from and that the subtitle search offers. It lives here, apart from any
one API module, so both the channel settings schema and the subtitle search can
use it without importing each other.
"""

#: (code, English name), in the order a person is likely to look for them.
LANGUAGES = [
    ("eng", "English"),
    ("spa", "Spanish"),
    ("fre", "French"),
    ("ger", "German"),
    ("ita", "Italian"),
    ("por", "Portuguese"),
    ("dut", "Dutch"),
    ("pol", "Polish"),
    ("rus", "Russian"),
    ("ara", "Arabic"),
    ("chi", "Chinese"),
    ("jpn", "Japanese"),
    ("dan", "Danish"),
    ("fin", "Finnish"),
    ("ell", "Greek"),
    ("hun", "Hungarian"),
    ("bul", "Bulgarian"),
    ("hrv", "Croatian"),
    ("mac", "Macedonian"),
    ("srp", "Serbian"),
    ("slv", "Slovenian"),
]
