"""
bfch_yts_torrents

Two things matter here: a release with several qualities is played at the one
a television can use, and the row says which that is. The API is not called;
the payload is built to the shape the channel reads.
"""

import bfch_yts_torrents as yts


def torrent(quality, size, seeds=1, url=None):
    return {
        "quality": quality,
        "type": "bluray",
        "size": "1.0 GB",
        "size_bytes": size,
        "seeds": seeds,
        "peers": seeds * 2,
        "url": url or "https://yts.example/%s" % quality,
    }


class TestQuality:
    def test_1080p_wins_over_a_smaller_720p(self):
        """
        It used to take the smallest torrent, which is 720p whenever a release
        has both -- and often the one with fewer seeds: measured on one film,
        the 720p had 2 seeds and the 1080p had 9.
        """
        chosen = yts._choose(
            [torrent("720p", 700), torrent("1080p", 1500, seeds=9)]
        )
        assert chosen["quality"] == "1080p"

    def test_720p_is_the_step_down_when_there_is_no_1080p(self):
        assert yts._choose([torrent("720p", 700)])["quality"] == "720p"

    def test_4k_is_not_preferred_even_when_it_is_the_only_one(self):
        """
        A Pi 3 cannot decode 2160p; if a release offers nothing else, playing
        the smallest is better than promising a quality the box will refuse.
        """
        chosen = yts._choose([torrent("2160p", 4000), torrent("720p", 700)])
        assert chosen["quality"] == "720p"

    def test_nothing_preferred_falls_back_to_the_smallest(self):
        chosen = yts._choose(
            [torrent("2160p", 4000), torrent("3D", 900)]
        )
        assert chosen["quality"] == "3D"


class TestTheRowSaysWhatItIs:
    def _payload(self):
        return {
            "data": {
                "movies": [
                    {
                        "title_long": "A Film (2026)",
                        "medium_cover_image": "https://img.yts.lt/a/medium-cover.jpg",
                        "rating": "7.0",
                        "imdb_code": "tt1234567",
                        "torrents": [
                            torrent("720p", 700, seeds=2),
                            torrent("1080p", 1500, seeds=9),
                        ],
                    }
                ]
            }
        }

    def test_it_asks_for_the_1080p_torrent(self):
        item = yts._extract(self._payload()).to_dict()[0]
        assert item["url"].startswith("https://yts.example/1080p")

    def test_the_subtitle_names_the_quality(self):
        item = yts._extract(self._payload()).to_dict()[0]
        assert item["subtitle"].startswith("1080p · ")
        assert "Seeds: 9" in item["subtitle"]
