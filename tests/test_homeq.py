"""Tests for the HomeQ collector. No internet: the parser runs on a saved response
and collect() runs against a fake client.

tests/samples/homeq_search.json is a trimmed copy of a real search response
(saved 2026-09-19, image lists removed): four apartments in the Göteborg region
(two short leases, one student flat, one without a move-in date), plus a
"project" and an apartment in Eslöv, which the collector must drop.
"""

import json
from pathlib import Path

import pytest
import requests

from collectors import homeq

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture(scope="module")
def listings():
    data = json.loads((SAMPLES / "homeq_search.json").read_text(encoding="utf-8"))
    return homeq.parse_search_response(data)


# --- search response -------------------------------------------------------

def test_keeps_only_apartments_in_the_goteborg_region(listings):
    assert [item["id"] for item in listings] == [
        "homeq:281903", "homeq:281846", "homeq:274188", "homeq:281320",
    ]  # the project (1330) and the Eslöv flat (281921) are gone


def test_first_listing_has_all_fields(listings):
    assert listings[0] == {
        "id": "homeq:281903",
        "source": "homeq",
        "url": "https://www.homeq.se/lagenhet/281903-2rum-västra-frölunda-västra-götalands-län-bergkristallsgatan-6",
        "address": "Bergkristallsgatan 6",
        "area": "Västra Frölunda",
        "kommun": "Göteborg",
        "lat": 57.6575282,
        "lon": 11.8935041,
        "rent_sek": 9002,
        "size_m2": 66.0,
        "rooms": 2.0,
        "move_in": "2026-11-01",
        "is_short_lease": False,
    }


def test_short_lease_is_flagged(listings):
    assert [item["is_short_lease"] for item in listings] == [False, True, False, True]


def test_missing_move_in_date_becomes_none(listings):
    assert listings[2]["move_in"] is None


def test_messy_values_are_tolerated():
    [item] = homeq.parse_search_response({"results": [
        {"id": 1, "municipality": "Göteborg ", "rent": "7 200", "title": "  ", "date_access": "2026-10-15T00:00:00"},
    ]})
    assert item["kommun"] == "Göteborg"  # trailing space removed
    assert item["rent_sek"] == 7200
    assert item["address"] is None
    assert item["move_in"] == "2026-10-15"
    assert (item["lat"], item["lon"], item["rooms"], item["size_m2"]) == (None, None, None, None)
    assert item["is_short_lease"] is None  # not stated: unknown, not "no"


@pytest.mark.parametrize("bad", [None, [], {}, {"results": "nope"}, {"results": [{"title": "no id"}]}])
def test_unexpected_response_raises(bad):
    with pytest.raises(homeq.HomeQError):
        homeq.parse_search_response(bad)


# --- token -----------------------------------------------------------------

def test_token_is_found_inside_user_info():
    assert homeq.parse_token({"user_info": {"token": "abc123", "ssn": "x"}}) == "abc123"


def test_missing_token_lists_field_names_but_not_values():
    with pytest.raises(homeq.HomeQError) as error:
        homeq.parse_token({"user_info": {"ssn": "secret-value"}, "other": 1})
    assert "user_info" in str(error.value)
    assert "secret-value" not in str(error.value)


# --- credentials -----------------------------------------------------------

@pytest.fixture(autouse=True)
def no_real_env(monkeypatch):
    monkeypatch.delenv("HOMEQ_EMAIL", raising=False)
    monkeypatch.delenv("HOMEQ_PASSWORD", raising=False)


def test_credentials_from_env_file(tmp_path):
    env = tmp_path / ".env"
    # utf-8-sig writes the invisible byte-order mark some Windows editors add
    env.write_text('# comment\nHOMEQ_EMAIL=jakob@example.com\nHOMEQ_PASSWORD="p=ss word"\n', encoding="utf-8-sig")
    assert homeq.load_credentials(env) == ("jakob@example.com", "p=ss word")


def test_environment_variables_win_over_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("HOMEQ_EMAIL=file@example.com\nHOMEQ_PASSWORD=file\n", encoding="utf-8")
    monkeypatch.setenv("HOMEQ_EMAIL", "secret@example.com")
    assert homeq.load_credentials(env) == ("secret@example.com", "file")


@pytest.mark.parametrize("content", [None, "", "HOMEQ_EMAIL=\nHOMEQ_PASSWORD=\n", "HOMEQ_EMAIL=a@b.se\n"])
def test_missing_credentials_raise(tmp_path, content):
    env = tmp_path / ".env"
    if content is not None:
        env.write_text(content, encoding="utf-8")
    with pytest.raises(homeq.HomeQError):
        homeq.load_credentials(env)


# --- collect() with a fake client ------------------------------------------

LOGIN_OK = {"user_info": {"token": "T"}}


PAGE = ('<html><script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"objectAd":'
        '{"candidate_sorting_mode":"queue_points","landlord_company":"Acme","floor":2,"date_publish":"2026-09-01"}}}}'
        '</script></html>')


def result(*ids, total=None, kommun="Göteborg"):
    items = [{"id": i, "title": f"Street {i}", "municipality": kommun, "type": "individual", "uri": f"/lagenhet/{i}"}
             for i in ids]
    return {"results": items, "total_hits": total if total is not None else len(items)}


class FakeClient:
    """Stands in for PoliteClient: hands out the queued responses and records requests."""

    responses = []
    calls = []
    pages = {}  # url -> html, or an exception to raise; anything else gets PAGE
    page_calls = []
    insights = {}  # url -> answer, or an exception to raise; anything else gets a "first_to_apply" answer
    insight_calls = []  # (url, token)

    def __init__(self, pause=0):
        pass

    def get_json(self, url, token):
        FakeClient.insight_calls.append((url, token))
        answer = FakeClient.insights.get(url, {"frame": "first_to_apply"})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get_text(self, url):
        FakeClient.page_calls.append(url)
        page = FakeClient.pages.get(url, PAGE)
        if isinstance(page, Exception):
            raise page
        return page

    def post_json(self, url, body, token=None):
        FakeClient.calls.append((url, body, token))
        response = FakeClient.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def fake(monkeypatch):
    FakeClient.calls = []
    FakeClient.page_calls = []
    FakeClient.pages = {}
    FakeClient.insights = {}
    FakeClient.insight_calls = []
    monkeypatch.setattr(homeq, "PoliteClient", FakeClient)
    monkeypatch.setattr(homeq, "load_credentials", lambda: ("me@example.com", "pw"))
    return FakeClient


def test_collect_logs_in_then_searches_once_when_everything_arrives_at_once(fake):
    fake.responses = [LOGIN_OK, result(1, 2, 3)]
    found = homeq.collect(pause=0)

    assert [item["id"] for item in found] == ["homeq:1", "homeq:2", "homeq:3"]
    assert fake.calls == [
        (homeq.LOGIN_URL, {"email": "me@example.com", "password": "pw"}, None),
        (homeq.SEARCH_URL, {"selectedShapes": "metropolitan_area.8", "page": 1}, "T"),
    ]  # exactly two requests: login and one search


def test_collect_asks_for_more_pages_only_if_total_hits_says_so(fake):
    fake.responses = [LOGIN_OK, result(1, 2, total=3), result(3, total=3)]
    assert len(homeq.collect(pause=0)) == 3
    assert [call[1].get("page") for call in fake.calls[1:]] == [1, 2]


def test_collect_stops_when_a_page_repeats(fake):
    fake.responses = [LOGIN_OK, result(1, 2, total=99), result(1, 2, total=99)]
    assert len(homeq.collect(pause=0)) == 2


def test_collect_drops_listings_outside_the_region(fake):
    both = result(1, kommun="Göteborg")["results"] + result(2, kommun="Eslöv")["results"]
    fake.responses = [LOGIN_OK, {"results": both, "total_hits": 2}]
    assert [item["id"] for item in homeq.collect(pause=0)] == ["homeq:1"]


def test_collect_refuses_a_result_with_nothing_in_the_region(fake):
    fake.responses = [LOGIN_OK, result(1, 2, kommun="Eslöv")]
    with pytest.raises(homeq.HomeQError):
        homeq.collect(pause=0)


def test_wrong_password_gives_a_clear_error(fake):
    refused = requests.HTTPError(response=requests.Response())
    refused.response.status_code = 403
    fake.responses = [refused]
    with pytest.raises(homeq.HomeQError, match="wrong email or password"):
        homeq.collect(pause=0)


def test_token_is_sent_as_jwt_not_bearer():
    sent = {}

    class FakeSession:
        headers = {}

        def post(self, url, json=None, headers=None, timeout=None):
            sent["headers"] = headers
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response

    client = homeq.PoliteClient(pause=0)
    client.session = FakeSession()
    client.post_json("https://example.com", {}, token="T")
    assert sent["headers"] == {"Authorization": "JWT T"}


# --- listing pages ---------------------------------------------------------
# Three real pages saved 2026-09-19: one listing per selection method.

def page(name):
    return (SAMPLES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("sample, expected", [
    ("homeq_detail_queue.html", {"allocation": "queue", "landlord": "Gunnar Lövgren Fastigheter", "floor": 4, "published": "2026-09-19"}),
    ("homeq_detail_lottery.html", {"allocation": "lottery", "landlord": "Kjellberg & Möller", "floor": 1, "published": "2026-09-18"}),
    ("homeq_detail_firstcome.html", {"allocation": "first_come", "landlord": "Varekilhem AB", "floor": 2, "published": "2026-04-07"}),
])
def test_listing_page_gives_allocation_landlord_floor_and_publish_date(sample, expected):
    assert homeq.parse_detail_page(page(sample)) == expected


def test_page_without_a_selection_method_is_marked_unknown_not_empty():
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"objectAd":{"floor":null,"candidate_sorting_mode":"something_new"}}}}</script>')
    assert homeq.parse_detail_page(html) == {"allocation": "unknown", "landlord": None, "floor": None, "published": None}


@pytest.mark.parametrize("html", ["<html>redesigned</html>", "", '<script id="__NEXT_DATA__">not json</script>',
                                  '<script id="__NEXT_DATA__">{"props":{}}</script>'])
def test_page_that_looks_different_raises(html):
    with pytest.raises(homeq.HomeQError):
        homeq.parse_detail_page(html)


def test_collect_reads_the_page_of_new_listings_only(fake):
    fake.responses = [LOGIN_OK, result(1, 2, 3)]
    found = {item["id"]: item for item in homeq.collect(known_details={"homeq:2"}, pause=0)}
    assert fake.page_calls == ["https://www.homeq.se/lagenhet/1", "https://www.homeq.se/lagenhet/3"]
    assert found["homeq:1"]["allocation"] == "queue" and found["homeq:1"]["landlord"] == "Acme"
    assert "allocation" not in found["homeq:2"]  # already read in an earlier run


def test_a_page_that_fails_leaves_the_listing_in_place_without_details(fake):
    fake.responses = [LOGIN_OK, result(1, 2)]
    fake.pages = {"https://www.homeq.se/lagenhet/1": requests.ConnectionError("boom")}
    found = {item["id"]: item for item in homeq.collect(pause=0)}
    assert set(found) == {"homeq:1", "homeq:2"}  # still returned, so it is not marked closed
    assert "allocation" not in found["homeq:1"]  # so it is tried again next run
    assert found["homeq:2"]["allocation"] == "queue"


def test_repeated_page_failures_stop_the_reading_for_this_run(fake):
    fake.responses = [LOGIN_OK, result(*range(1, 11))]
    fake.pages = {f"https://www.homeq.se/lagenhet/{i}": requests.ConnectionError("down") for i in range(1, 11)}
    assert len(homeq.collect(pause=0)) == 10
    assert len(fake.page_calls) == homeq.MAX_PAGE_FAILURES


def test_a_redesigned_listing_page_fails_the_whole_run(fake):
    fake.responses = [LOGIN_OK, result(1)]
    fake.pages = {"https://www.homeq.se/lagenhet/1": "<html>new design</html>"}
    with pytest.raises(homeq.HomeQError):
        homeq.collect(pause=0)


# --- points figure ("free insights") -----------------------------------------
# Two real answers saved 2026-09-19: a strict listing (has the figure) and a guidance one (has none).

def insight(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def test_insights_with_a_figure_give_the_points_for_top_10():
    assert homeq.parse_insights(insight("homeq_insights_strict.json")) == {
        "insight_frame": "queue_points_info", "points_needed_top10": 2546}


def test_insights_without_a_figure_keep_the_frame_and_give_no_points():
    assert homeq.parse_insights(insight("homeq_insights_first_to_apply.json")) == {
        "insight_frame": "first_to_apply", "points_needed_top10": None}


@pytest.mark.parametrize("points", ["2546", True, 12.5, None])
def test_a_figure_that_is_not_a_whole_number_is_ignored(points):
    assert homeq.parse_insights({"frame": "queue_points_info", "queue_points_top_10": points})["points_needed_top10"] is None


@pytest.mark.parametrize("bad", [None, [], {}, {"frame": ""}, {"frame": 5}, "text"])
def test_insights_that_look_different_raise(bad):
    with pytest.raises(homeq.HomeQError):
        homeq.parse_insights(bad)


INSIGHT_URL = "https://api.homeq.se/api/v3/free_insights/{}/"


def test_no_cutoff_means_no_readings(fake):
    fake.responses = [LOGIN_OK, result(1, 2)]
    found = homeq.collect(pause=0)
    assert fake.insight_calls == []
    assert "insight_frame" not in found[0]


def test_only_stale_listings_are_read_and_never_read_ones_go_first(fake):
    fake.responses = [LOGIN_OK, result(1, 2, 3)]
    last = {"homeq:1": "2026-09-19T12:00:00+00:00", "homeq:2": "2026-09-18T01:00:00+00:00"}  # 1 fresh, 2 stale, 3 never
    found = {item["id"]: item for item in
             homeq.collect(pause=0, last_insight=last, insight_cutoff="2026-09-19T00:00:00+00:00")}
    assert fake.insight_calls == [(INSIGHT_URL.format(3), "T"), (INSIGHT_URL.format(2), "T")]  # sent as the login token
    assert "insight_frame" not in found["homeq:1"]
    assert found["homeq:2"]["insight_frame"] == "first_to_apply"


def test_the_figure_is_added_to_the_listing(fake):
    fake.responses = [LOGIN_OK, result(1)]
    fake.insights = {INSIGHT_URL.format(1): insight("homeq_insights_strict.json")}
    found = homeq.collect(pause=0, insight_cutoff="9")
    assert (found[0]["insight_frame"], found[0]["points_needed_top10"]) == ("queue_points_info", 2546)


def test_readings_per_run_are_capped(fake, monkeypatch):
    monkeypatch.setattr(homeq, "MAX_INSIGHTS_PER_RUN", 2)
    fake.responses = [LOGIN_OK, result(*range(1, 6))]
    homeq.collect(pause=0, insight_cutoff="9")
    assert len(fake.insight_calls) == 2


def test_a_failed_reading_is_skipped_and_the_rest_go_on(fake):
    fake.responses = [LOGIN_OK, result(1, 2)]
    fake.insights = {INSIGHT_URL.format(1): requests.ConnectionError("boom")}
    found = {item["id"]: item for item in homeq.collect(pause=0, insight_cutoff="9")}
    assert set(found) == {"homeq:1", "homeq:2"}  # still returned, so it is not marked closed
    assert "insight_frame" not in found["homeq:1"] and found["homeq:2"]["insight_frame"] == "first_to_apply"


def test_repeated_failed_readings_stop_for_this_run(fake):
    fake.responses = [LOGIN_OK, result(*range(1, 11))]
    fake.insights = {INSIGHT_URL.format(i): requests.ConnectionError("down") for i in range(1, 11)}
    assert len(homeq.collect(pause=0, insight_cutoff="9")) == 10
    assert len(fake.insight_calls) == homeq.MAX_PAGE_FAILURES


def test_a_changed_insights_answer_stops_the_readings_but_not_the_run(fake):
    fake.responses = [LOGIN_OK, result(1, 2, 3)]
    fake.insights = {INSIGHT_URL.format(1): {"something": "new"}}
    found = homeq.collect(pause=0, insight_cutoff="9")
    assert len(found) == 3  # the listings themselves are still collected
    assert len(fake.insight_calls) == 1 and all("insight_frame" not in item for item in found)


def test_get_json_sends_the_token_as_jwt():
    sent = {}

    class FakeSession:
        headers = {}

        def get(self, url, headers=None, timeout=None):
            sent["headers"] = headers
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"frame": "x"}'
            return response

    client = homeq.PoliteClient(pause=0)
    client.session = FakeSession()
    assert client.get_json("https://example.com", "T") == {"frame": "x"}
    assert sent["headers"] == {"Authorization": "JWT T"}
