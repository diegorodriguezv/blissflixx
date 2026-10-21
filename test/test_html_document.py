"""
The build stamped into the document the server hands out.

The browser was weeks behind and nothing could tell. This app is a PWA by
virtue of its meta tags, so browsers keep the document, and a kept document
loads the JavaScript it was saved with -- so the server could be three weeks
ahead and the browser would never show it, reloading included.

Two things are therefore done on the way out, because neither can be done from
the page:

- every same-origin asset gets the build on its URL, so a new build means new
  URLs and a stale copy is simply never asked for again
- the build is written into the document, so the number on the screen comes from
  the server that served it rather than from JavaScript that might be old

And one set of files cannot be stamped from here at all -- pages/*.html is
fetched by riot at runtime, once per route -- so the document hands the build
down and app.html puts it on those requests.
"""

import re

import pytest

import blissflixx
from lib import locations


@pytest.fixture(scope="module")
def served():
    return blissflixx.HtmlDocument().index()


def assets(page):
    return re.findall(r'(?:src|href)="([^"]+)"', page)


class TestTheDocumentCarriesTheBuild:
    def test_it_says_what_is_checked_out(self, served):
        """
        The number on the screen, written by the server that served the page
        rather than fetched by JavaScript that might be weeks old. A diagnostic
        that can be stale is not a diagnostic.
        """
        import subprocess

        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=locations.ROOT_PATH,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if sha.returncode != 0:
            pytest.skip("no git checkout here")
        build = sha.stdout.strip()
        assert '<meta name="build" content="%s">' % build in served

    def test_it_hands_the_build_to_the_page(self, served):
        """
        For the files it cannot stamp. pages/*.html is fetched by riot at
        runtime, once per route, and has to be asked for by name later -- so the
        document is the only thing that knows the build at load time.
        """
        # Name the tag, not just the attribute: the document already has a
        # content="IE=edge" on the X-UA-Compatible meta, and picking that up
        # first is a test that passes for the wrong reason.
        build = re.search(r'<meta name="build" content="([^"]+)"', served).group(1)
        assert "window.BF_BUILD = '%s';" % build in served

    def test_and_the_page_uses_it_on_the_requests_it_makes(self):
        source = locations.HTML_PATH + "/app.html"
        with open(source) as handle:
            app = handle.read()
        assert "window.BF_BUILD" in app, (
            "app.html fetches pages/*.html at runtime and never puts the build "
            "on those requests, so a cached copy of a page is still possible"
        )


class TestEveryAssetCarriesTheBuild:
    def test_no_local_script_or_stylesheet_is_left_untouched(self, served):
        """
        The point of the whole thing: a document that names yesterday's scripts
        is the failure being fixed, so there must not be one.
        """
        build = re.search(r'content="([^"]+)"', served).group(1)
        missed = [
            url
            for url in assets(served)
            if not url.startswith(("//", "http://", "https://")) and "?v=" not in url
        ]
        assert missed == [], "local assets with no build on them: %s" % missed

    def test_and_each_one_has_the_right_build(self, served):
        build = re.search(r'<meta name="build" content="([^"]+)"', served).group(1)
        for url in assets(served):
            if "?v=" in url:
                assert url.endswith("?v=" + build), url

    def test_somebody_elses_cache_is_left_alone(self, served):
        """
        Appending a query to a CDN URL defeats the caching it is there for and
        tells us nothing about this build. The four that go to a CDN are the
        stylesheets and jQuery, and every one of them must come through
        untouched.
        """
        remote = [
            url
            for url in assets(served)
            if url.startswith(("//", "http://", "https://"))
        ]
        assert remote, "no CDN assets found -- the filter has stopped matching"
        for url in remote:
            assert "?v=" not in url, url

    def test_it_does_not_stamp_a_url_that_already_has_a_query(self):
        """
        The pattern matches the path and stops at the "?", so "?v=1" or
        "?cachebust=x" is left exactly as written rather than gaining a second.
        """
        pattern = blissflixx.HtmlDocument._local_asset
        out = pattern.sub(
            lambda m: m.group(1) + m.group(2) + "?v=BUILD" + m.group(3),
            '<script src="js/a.js?cachebust=1"></script>',
        )
        assert out == '<script src="js/a.js?cachebust=1"></script>'


class TestItStillWorksWithoutGit:
    def test_it_falls_back_when_git_is_not_there(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("no git")

        monkeypatch.setattr(blissflixx.subprocess, "run", boom)
        assert blissflixx.HtmlDocument._server_build() == "unknown"
        assert (
            '<meta name="build" content="unknown">' in blissflixx.HtmlDocument().index()
        )
