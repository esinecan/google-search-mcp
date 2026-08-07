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

from google_search_mcp.client import MAX_PAGES, RESULTS_PER_PAGE, build_query, build_url


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

    def test_vertical_maps_to_tbm(self):
        assert _params(build_url("x", vertical="news"))["tbm"] == "nws"

    def test_web_vertical_sends_no_tbm(self):
        assert "tbm" not in _params(build_url("x", vertical="web"))

    def test_country_sets_both_cr_and_gl(self):
        p = _params(build_url("x", country="de"))
        assert p["cr"] == "countryDE"
        assert p["gl"] == "de"

    @pytest.mark.parametrize("bad", ["decade", "hour", "qdr:d"])
    def test_bad_freshness_raises(self, bad):
        with pytest.raises(ValueError):
            build_url("x", freshness=bad)

    @pytest.mark.parametrize("field", ["freshness", "lang", "country", "vertical"])
    def test_empty_string_means_unset_not_invalid(self, field):
        # Agents and CLI flags both emit "" for "not given". It must not reach validation
        # as a supplied value, and it must not raise.
        url = build_url("x", **{field: ""})
        assert "tbs" not in _params(url)
        assert "tbm" not in _params(url)

    def test_bad_vertical_raises(self):
        with pytest.raises(ValueError):
            build_url("x", vertical="podcasts")

    def test_query_is_encoded(self):
        assert "q=a%2Bb+%26+c" in build_url("a+b & c")


def test_max_pages_is_bounded():
    # Guards the throttle budget: each page is a separate round trip against a rate limit.
    assert 1 <= MAX_PAGES <= 10
