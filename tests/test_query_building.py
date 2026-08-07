"""Offline tests for the pure half: operator assembly and URL construction.

Deliberately no browser. These are the parts that are cheap to get subtly wrong and cheap
to pin -- an agent passing `after='2026-01-01'` and silently getting `daterange:` would
produce plausible, wrong results forever.

The parse layer is NOT tested here. It can only be tested against live markup that rotates
by design, which is what `schema_drift` exists to surface at runtime instead.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from google_search_mcp.client import (
    MAX_PAGES,
    RESULTS_PER_PAGE,
    VERTICALS,
    _cdr,
    build_query,
    build_url,
    parse_result_stats,
)


def _params(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class TestBuildQuery:
    def test_plain_query_passes_through(self):
        assert build_query("mcp server") == "mcp server"

    def test_all_operators_compose(self):
        q = build_query(
            "mcp server",
            site="arxiv.org",
            filetype="pdf",
            exact="model context protocol",
            exclude=["tutorial", "beginner"],
            after="2026-01-01",
            before="2026-08-01",
        )
        assert '"model context protocol"' in q
        assert "site:arxiv.org" in q
        assert "filetype:pdf" in q
        assert "-tutorial" in q and "-beginner" in q
        assert "after:2026-01-01" in q
        assert "before:2026-08-01" in q

    def test_empty_exclusions_are_dropped(self):
        assert build_query("x", exclude=["", "  ", "y"]) == "x -y"

    def test_none_operators_add_nothing(self):
        assert build_query("x", site=None, filetype=None, exclude=None) == "x"


class TestBuildUrl:
    def test_defaults(self):
        p = _params(build_url("x"))
        assert p["q"] == "x"
        assert p["hl"] == "en"
        assert "start" not in p       # page 0 must not send start=0
        assert "pws" not in p         # personalized is the default; absent means on

    def test_pagination_uses_start_not_num(self):
        # num= stopped being honoured 11 Sept 2025; depth is start= only.
        p = _params(build_url("x", page=3))
        assert p["start"] == str(3 * RESULTS_PER_PAGE)
        assert "num" not in p

    def test_personalization_off_sends_pws_zero(self):
        assert _params(build_url("x", personalized=False))["pws"] == "0"

    def test_freshness_maps_to_tbs(self):
        assert _params(build_url("x", freshness="week"))["tbs"] == "qdr:w"

    def test_hour_freshness_is_supported(self):
        assert _params(build_url("x", freshness="hour"))["tbs"] == "qdr:h"

    def test_vertical_maps_to_udm(self):
        # tbm is dead as a scheme -- Google redirects tbm=isch to udm=2 itself.
        assert _params(build_url("x", vertical="news"))["udm"] == "12"

    def test_web_vertical_sends_no_udm(self):
        assert "udm" not in _params(build_url("x", vertical="web"))

    def test_web_only_is_a_distinct_vertical(self):
        assert _params(build_url("x", vertical="web_only"))["udm"] == "web"

    def test_country_sets_both_cr_and_gl(self):
        p = _params(build_url("x", country="de"))
        assert p["cr"] == "countryDE"
        assert p["gl"] == "de"

    @pytest.mark.parametrize("bad", ["decade", "minute", "qdr:d"])
    def test_bad_freshness_raises(self, bad):
        with pytest.raises(ValueError):
            build_url("x", freshness=bad)

    @pytest.mark.parametrize("field", ["freshness", "lang", "country", "vertical"])
    def test_empty_string_means_unset_not_invalid(self, field):
        # Agents and CLI flags both emit "" for "not given". It must not reach validation
        # as a supplied value, and it must not raise.
        url = build_url("x", **{field: ""})
        assert "tbs" not in _params(url)
        assert "udm" not in _params(url)

    def test_bad_vertical_raises(self):
        with pytest.raises(ValueError):
            build_url("x", vertical="podcasts")

    def test_shopping_is_not_silently_accepted(self):
        # udm=28 renders product cards with no external anchors and no headings. It was
        # left unmapped on purpose; accepting it would ship a vertical that always
        # returns zero, which is the exact bug this release fixed.
        with pytest.raises(ValueError):
            build_url("x", vertical="shopping")


class TestTbsOrdering:
    """The order of tbs directives decides whether they all apply.

    Measured 2026-08-07 on `renewable energy storage`:

        qdr:w        ->     514,000   electrek.co, the-european.eu
        li:1         -> 124,000,000   nationalgrid, iso.org
        qdr:w,li:1   -> 124,000,000   identical to li:1 -- the freshness was DROPPED
        li:1,qdr:w   ->     453,000   electrek.co, dyness -- both applied

    Freshness placed after verbatim is discarded with no error. A caller asking for the
    past week would silently get the whole index, which is the worst class of bug this
    tool can have: plausible, wrong, and invisible.
    """

    def test_verbatim_precedes_freshness(self):
        assert _params(build_url("x", freshness="week", verbatim=True))["tbs"] == "li:1,qdr:w"

    def test_verbatim_alone(self):
        assert _params(build_url("x", verbatim=True))["tbs"] == "li:1"

    def test_custom_range_supersedes_a_preset_window(self):
        # Both are date filters; emitting them together is contradictory, and cdr is the
        # more specific request, so it wins rather than being appended alongside.
        tbs = _params(build_url("x", freshness="year", tbs_extra="cdr:1,cd_min:1/1/2026"))["tbs"]
        assert tbs == "cdr:1,cd_min:1/1/2026"
        assert "qdr" not in tbs

    def test_bad_freshness_still_raises_when_superseded(self):
        with pytest.raises(ValueError):
            build_url("x", freshness="decade", tbs_extra="cdr:1")


class TestCustomRange:
    def test_iso_dates_become_us_format(self):
        # Tools > Custom range wants M/D/YYYY. Sending ISO silently filters nothing.
        assert _cdr("2026-01-05", "2026-03-09") == "cdr:1,cd_min:1/5/2026,cd_max:3/9/2026"

    def test_open_ended_range(self):
        assert _cdr("2026-01-05", None) == "cdr:1,cd_min:1/5/2026"
        assert _cdr(None, "2026-03-09") == "cdr:1,cd_max:3/9/2026"


class TestResultStats:
    """Separators follow the BROWSER locale, not `hl` -- this box renders German
    grouping under hl=en. So digits are extracted and separators dropped wholesale."""

    def test_german_grouping(self):
        got = parse_result_stats("About 187.000.000 results (0,24s)")
        assert got["total"] == 187_000_000
        assert got["seconds"] == 0.24

    def test_english_grouping(self):
        assert parse_result_stats("About 187,000,000 results (0.24 seconds)")["total"] == 187_000_000

    def test_small_exact_count(self):
        assert parse_result_stats("10 results (0,42s)")["total"] == 10

    def test_absent_stats_is_not_an_error(self):
        assert parse_result_stats(None) == {"total": None, "seconds": None, "raw": None}

    def test_unparseable_keeps_the_raw_string(self):
        got = parse_result_stats("something else entirely")
        assert got["total"] is None
        assert got["raw"] == "something else entirely"


class TestVerticalTable:
    def test_every_vertical_names_an_anchor(self):
        # A vertical without an anchor silently falls back to h3 and returns zero on the
        # verticals that have no h3 -- the original bug.
        for name, spec in VERTICALS.items():
            assert spec["anchor"] in {"h3", "role", "image"}, name

    def test_books_allows_google_hosts(self):
        # Every Google Books link is google-hosted, so the usual drop-google-hostnames
        # rule would zero the vertical out.
        assert VERTICALS["books"].get("google_hosts") is True

    def test_web_stays_h3_anchored(self):
        # Load-bearing: the ad-exclusion guarantee on web is that ads carry no <h3>.
        # Broadening this to [role=heading] admitted 2/2 ads on a commercial query.
        assert VERTICALS["web"]["anchor"] == "h3"

    def test_query_is_encoded(self):
        assert "q=a%2Bb+%26+c" in build_url("a+b & c")


def test_max_pages_is_bounded():
    # Guards the throttle budget: each page is a separate round trip against a rate limit.
    assert 1 <= MAX_PAGES <= 10
