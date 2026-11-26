"""
The Rotten Tomatoes rows and search.

The browse page is server-rendered tiles, each carrying a film's poster, its
streaming date and both of the site's scores -- so the rows can say what a
rating is made of, linked to the page that shows it. The structured item list
the page also publishes is the fallback, for the day the tiles change shape.

The search answers with its own server-rendered rows: tomatometer, year and
cast per film. Everything plays as a trailer, so the channel offers no
subtitles: the films' subtitles do not match the trailers' cuts.
"""

import blissflixx.channels.bfch_rotten_tomatoes as rt

BROWSE = """
<html><body>
<div class="flex-container"><media-info-tile>
  <poster-tile slot="poster" media-url="/m/madden" data-qa="discovery-media-list-item">
    <rt-img alt="Madden" src="https://img/madden.jpg"></rt-img>
  </poster-tile>
  <a class="js-tile-link" href="/m/madden" data-qa="discovery-media-list-item-caption" slot="meta-data">
    <score-pairs-deprecated>
      <score-icon-critics certified="false" sentiment="negative"></score-icon-critics>
      <rt-text slot="criticsScore" context="label" size="1"> 16%</rt-text>
      <score-icon-audience certified="false" sentiment="negative"></score-icon-audience>
      <rt-text slot="audienceScore" context="label" size="1"> 51%</rt-text>
    </score-pairs-deprecated>
    <rt-text size="0.875" data-qa="discovery-media-list-item-title">
      Madden
    </rt-text>
    <rt-text size="0.75" data-qa="discovery-media-list-item-start-date">
      Streaming Nov 18, 2026
    </rt-text>
  </a>
</media-info-tile></div>
<div class="flex-container"><media-info-tile>
  <poster-tile slot="poster" media-url="/m/blue_film" data-qa="discovery-media-list-item">
    <rt-img alt="Blue Film" src="https://img/blue.jpg"></rt-img>
  </poster-tile>
  <a class="js-tile-link" href="/m/blue_film" data-qa="discovery-media-list-item-caption" slot="meta-data">
    <score-pairs-deprecated>
      <score-icon-critics certified="true" sentiment="positive"></score-icon-critics>
      <rt-text slot="criticsScore" context="label" size="1"> 91%</rt-text>
      <score-icon-audience certified="false" sentiment="positive"></score-icon-audience>
      <rt-text slot="audienceScore" context="label" size="1"> 66%</rt-text>
    </score-pairs-deprecated>
    <rt-text size="0.875" data-qa="discovery-media-list-item-title">
      Blue Film
    </rt-text>
    <rt-text size="0.75" data-qa="discovery-media-list-item-start-date">
      Streaming Jun 12, 2026
    </rt-text>
  </a>
</media-info-tile></div>
</body></html>
"""

LDJSON_ONLY = """
<html><body>
<script type="application/ld+json">
{"itemListElement": {"itemListElement": [
  {"@type": "Movie", "name": "Old Shape", "image": "https://img/old.jpg",
   "url": "/m/old_shape", "dateCreated": "2020",
   "aggregateRating": {"ratingValue": "88"}}]}}
</script>
</body></html>
"""

SEARCH = """
<html><body>
<search-page-media-row cast="Nicolas Cage,Christian Bale" release-year="2026"
    tomatometer-score="16" tomatometer-is-certified="false" tomatometer-sentiment="NEGATIVE">
  <a href="https://www.rottentomatoes.com/m/madden" class="unset" slot="thumbnail">
    <img alt="Madden" src="https://img/madden80.jpg">
  </a>
  <a href="https://www.rottentomatoes.com/m/madden" class="unset" slot="title"> Madden </a>
</search-page-media-row>
<search-page-media-row cast="Steve Madden" release-year="2017">
  <a href="https://www.rottentomatoes.com/m/maddman_the_steve_madden_story" class="unset" slot="thumbnail">
    <img alt="Maddman: The Steve Madden Story" src="https://img/maddman80.jpg">
  </a>
  <a href="https://www.rottentomatoes.com/m/maddman_the_steve_madden_story" class="unset" slot="title">
    Maddman: The Steve Madden Story
  </a>
</search-page-media-row>
<search-page-media-row release-year="1980">
  <a href="https://www.rottentomatoes.com/celebrity/john_madden" class="unset" slot="title">
    John Madden
  </a>
</search-page-media-row>
</body></html>
"""


class TestTheBrowseRows:
    def test_a_row_says_both_scores_and_links_them(self):
        rows = rt._extract(BROWSE).to_list()
        assert len(rows) == 2
        madden = rows[0]
        assert madden.title == "Madden"
        assert madden.url == "https://www.rottentomatoes.com/m/madden"
        assert madden.img == "https://img/madden.jpg"
        assert madden.subtitle == "Streaming Nov 18, 2026"
        assert (
            madden.synopsis
            == '<a target="_blank" href="https://www.rottentomatoes.com/m/madden">Tomatometer: 16%</a>'
            ' · <a target="_blank" href="https://www.rottentomatoes.com/m/madden">Popcornmeter: 51%</a>'
        )

    def test_a_certified_fresh_film_says_so(self):
        rows = rt._extract(BROWSE).to_list()
        assert "Tomatometer (Certified Fresh): 91%" in rows[1].synopsis
        assert "Popcornmeter: 66%" in rows[1].synopsis

    def test_the_structured_list_is_the_fallback(self):
        """The tiles changed shape once already; the page's own data still answers."""
        rows = rt._extract(LDJSON_ONLY).to_list()
        assert len(rows) == 1
        assert rows[0].title == "Old Shape"
        assert rows[0].url == "https://www.rottentomatoes.com/m/old_shape"
        assert "Tomatometer: 88%" in rows[0].synopsis
        assert 'href="https://www.rottentomatoes.com/m/old_shape"' in rows[0].synopsis


class TestTheSearch:
    def test_rows_carry_the_year_and_the_cast(self):
        rows = rt._search_rows(SEARCH).to_list()
        assert rows[0].title == "Madden"
        assert rows[0].subtitle == "2026 · Nicolas Cage, Christian Bale"
        assert (
            rows[0].synopsis
            == '<a target="_blank" href="https://www.rottentomatoes.com/m/madden">Tomatometer: 16%</a>'
        )

    def test_a_film_without_a_score_still_links_its_page(self):
        rows = rt._search_rows(SEARCH).to_list()
        assert rows[1].title == "Maddman: The Steve Madden Story"
        assert 'href="https://www.rottentomatoes.com/m/maddman_the_steve_madden_story"' in rows[1].synopsis

    def test_people_are_not_films(self):
        rows = rt._search_rows(SEARCH).to_list()
        assert all("John Madden" != row.title for row in rows)


class TestTheChannel:
    def test_trailers_offer_no_subtitles(self):
        assert rt.SUPPORTS_SUBTITLES is False
