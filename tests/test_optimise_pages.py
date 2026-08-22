"""video-optimise page rendering tests. Pure string/structure checks — no
browser, no server: :mod:`icloud_photo_sync.optimise_review` covers the wire
protocol these pages are served over."""

import json
import re

import pytest

from icloud_photo_sync.optimise_pages import render_compare_page, render_select_page

ITEMS_RE = re.compile(r"const ITEMS = (.*);\n")


def _extract_items(html: str) -> list:
    m = ITEMS_RE.search(html)
    assert m is not None, "const ITEMS = ...; line not found"
    return json.loads(m.group(1))


def _select_item(**overrides) -> dict:
    base = {
        "index": 0,
        "rel": "clips/a.mov",
        "name": "a.mov",
        "bytes": 1_000_000,
        "size": "1.0 MB",
        "dur": "00:00:10",
        "dims": "3840×2160",
        "out": "1920×1080",
        "saving": "500.0 KB",
        "percent": 50,
        "hdr": False,
        "slomo": False,
        "fps": 0,
        "keepsFps": False,
        "skip": "",
    }
    base.update(overrides)
    return base


def _compare_pair(**overrides) -> dict:
    base = {
        "index": 0,
        "rel": "clips/a.mov",
        "name": "a.mov",
        "srcSize": "100.0 MB",
        "outSize": "40.0 MB",
        "srcLabel": "3840x2160 · 48 Mbps",
        "outLabel": "1920x1080 · 5.8 Mbps",
        "colour": "HLG HDR → HLG HDR",
        "percent": 60,
        "dur": "00:01:00",
        "hdr": False,
        "slomo": False,
    }
    base.update(overrides)
    return base


# --- basic shape ---------------------------------------------------------


def test_select_page_is_html_string_with_token():
    html = render_select_page("SECRET1", [_select_item()])
    assert isinstance(html, str)
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "SECRET1" in html


def test_compare_summary_page_is_html_string_with_token():
    html = render_compare_page("SECRET2", [_compare_pair()], review_all=False, total=1)
    assert isinstance(html, str)
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "SECRET2" in html


def test_compare_grid_page_is_html_string_with_token():
    html = render_compare_page("SECRET3", [_compare_pair()], review_all=True, total=1)
    assert isinstance(html, str)
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "SECRET3" in html


def test_token_appears_as_js_string_literal():
    html = render_select_page("tok-xyz", [])
    assert 'const TOKEN = "tok-xyz";' in html


# --- placeholder substitution ---------------------------------------------


def test_select_page_placeholders_fully_substituted():
    html = render_select_page("tok", [_select_item()])
    assert "__TOKEN__" not in html
    assert "__ITEMS_JSON__" not in html


@pytest.mark.parametrize("review_all", [False, True])
def test_compare_page_placeholders_fully_substituted(review_all):
    html = render_compare_page("tok", [_compare_pair()], review_all=review_all, total=5)
    assert "__TOKEN__" not in html
    assert "__ITEMS_JSON__" not in html
    assert "__REVIEW_ALL__" not in html
    assert "__TOTAL__" not in html


def test_compare_page_embeds_review_all_flag():
    html_false = render_compare_page("tok", [], review_all=False, total=0)
    html_true = render_compare_page("tok", [], review_all=True, total=0)
    assert "const REVIEW_ALL = false;" in html_false
    assert "const REVIEW_ALL = true;" in html_true


def test_compare_page_embeds_total():
    html = render_compare_page("tok", [], review_all=False, total=42)
    assert "const TOTAL = 42;" in html
    assert "Review all 42 side by side" in html


# --- payload round trip -----------------------------------------------------


def test_select_items_round_trip():
    items = [_select_item(index=0, rel="a.mov"), _select_item(index=1, rel="b.mov", skip="too dark")]
    html = render_select_page("tok", items)
    assert _extract_items(html) == items


def test_compare_pairs_round_trip_summary():
    pairs = [_compare_pair(index=0), _compare_pair(index=1, hdr=True, slomo=True)]
    html = render_compare_page("tok", pairs, review_all=False, total=2)
    assert _extract_items(html) == pairs


def test_compare_pairs_round_trip_grid():
    pairs = [_compare_pair(index=0), _compare_pair(index=1, hdr=True, slomo=True)]
    html = render_compare_page("tok", pairs, review_all=True, total=2)
    assert _extract_items(html) == pairs


# --- hostile payload content: quotes, backslashes, unicode, </script> -------


_HOSTILE_NAME = "weird's \\ name </script><script>alert(1)</script> 日本語.mov"


def test_select_page_survives_hostile_filename():
    items = [_select_item(rel="clips/" + _HOSTILE_NAME, name=_HOSTILE_NAME)]
    html = render_select_page("tok", items)
    # The raw closing-script-tag sequence must never appear verbatim in the
    # embedded payload region: it would terminate the <script> block early.
    m = ITEMS_RE.search(html)
    assert "</script>" not in m.group(1)
    assert _extract_items(html) == items


def test_compare_page_survives_hostile_filename():
    pairs = [_compare_pair(rel="clips/" + _HOSTILE_NAME, name=_HOSTILE_NAME)]
    html = render_compare_page("tok", pairs, review_all=False, total=1)
    m = ITEMS_RE.search(html)
    assert "</script>" not in m.group(1)
    assert _extract_items(html) == pairs


def test_no_script_sequence_anywhere_in_hostile_page():
    # Belt and braces: scan the whole page, not just the extracted JSON.
    items = [_select_item(rel=_HOSTILE_NAME, name=_HOSTILE_NAME)]
    html = render_select_page("tok", items)
    # There is exactly one legitimate "</script>" (the real closing tag);
    # anything from the payload must not have produced an *extra* one that
    # would close the script block early. Count instead of a blanket "not in".
    assert html.count("</script>") == 1


def test_unicode_filename_survives_round_trip():
    name = "日本語 🎬 café.mov"
    items = [_select_item(rel=name, name=name)]
    html = render_select_page("tok", items)
    round_tripped = _extract_items(html)
    assert round_tripped[0]["name"] == name
    assert round_tripped[0]["rel"] == name


# --- select page content -----------------------------------------------------


def test_skip_reason_appears_on_page():
    items = [_select_item(index=0, skip="rotation metadata unreadable")]
    html = render_select_page("tok", items)
    assert "rotation metadata unreadable" in html


def test_keeps_fps_note_present_when_flagged():
    html = render_select_page("tok", [_select_item(keepsFps=True, fps=120)])
    assert "keepsFps" in html
    assert "keeps " in html  # the JS builds "keeps " + it.fps + " fps"


def test_select_page_has_expected_buttons():
    html = render_select_page("tok", [_select_item()])
    assert "Select all" in html
    assert "4K only" in html
    assert "Clear" in html
    assert 'id="done"' in html


def test_select_page_hdr_and_slomo_badge_wiring():
    html = render_select_page("tok", [_select_item()])
    assert "SLO-MO" in html
    assert "badge hdr" in html
    assert '"HDR"' in html


def test_select_page_video_route_used_for_modal():
    html = render_select_page("tok", [_select_item()])
    assert '"/video/" + it.index' in html


def test_select_page_poster_route_used_for_grid():
    html = render_select_page("tok", [_select_item()])
    assert '"/poster/" + it.index' in html


def test_select_page_only_one_video_element_the_modal():
    html = render_select_page("tok", [_select_item(index=0), _select_item(index=1)])
    assert html.count("<video") == 1


def test_select_page_empty_list_does_not_raise():
    html = render_select_page("tok", [])
    assert "<!doctype html" in html
    assert _extract_items(html) == []


# --- compare page content -----------------------------------------------------


def test_compare_summary_has_all_three_footer_buttons_and_approve_all_choice():
    html = render_compare_page("tok", [_compare_pair()], review_all=False, total=1)
    assert "Review all" in html and "side by side" in html
    assert "Cancel — change nothing" in html
    assert "Finish — these are ready to upload" in html
    assert "approve-all" in html


def test_compare_summary_approve_button_is_not_styled_danger():
    # Nothing irreversible follows this click any more, so it must not be
    # styled as the destructive/danger button.
    html = render_compare_page("tok", [_compare_pair()], review_all=False, total=1)
    assert '<button class="danger" id="approveBtn">' not in html
    assert '<button class="primary" id="approveBtn">' in html


def test_compare_grid_has_done_upload_and_no_approve_all():
    html = render_compare_page("tok", [_compare_pair()], review_all=True, total=1)
    assert "Done — upload" in html
    assert "approve-all" not in html
    # nor the neutral/quiet review-all-summary controls, which belong only
    # to the other page
    assert "Cancel — change nothing" not in html


def test_compare_summary_has_no_upload_grid_controls():
    html = render_compare_page("tok", [_compare_pair()], review_all=False, total=1)
    assert "Done — upload" not in html


def test_compare_summary_video_elements_have_preload_none():
    # Without preload="none", opening the page starts one download per pane.
    html = render_compare_page("tok", [_compare_pair()], review_all=False, total=1)
    assert 'preload="none"' in html


def test_the_two_panes_do_not_share_a_poster():
    # Showing the converted frame above the word "Original" would be quietly
    # answering the question the screen exists to ask.
    html = render_compare_page("tok", [_compare_pair()], review_all=False, total=1)
    assert '"/src-poster/"' in html and '"/poster/"' in html
    assert html.index('"/src-poster/"') < html.index('"/poster/");')


def test_compare_summary_uses_original_and_converted_routes():
    html = render_compare_page("tok", [_compare_pair(index=7)], review_all=False, total=1)
    assert '"/original/" + it.index' in html
    assert '"/converted/" + it.index' in html


def test_compare_grid_uses_original_and_converted_routes_in_modal():
    html = render_compare_page("tok", [_compare_pair(index=7)], review_all=True, total=1)
    assert '"/original/" + it.index' in html
    assert '"/converted/" + it.index' in html


def test_compare_page_upload_instructions_present():
    for review_all in (False, True):
        html = render_compare_page("tok", [_compare_pair()], review_all=review_all, total=1)
        assert "icloud.com" in html
        assert "ready to upload" in html


def test_compare_grid_unticking_discards_text():
    html = render_compare_page("tok", [_compare_pair()], review_all=True, total=1)
    assert "discard — won't upload" in html


def test_compare_grid_starts_everything_selected():
    html = render_compare_page("tok", [_compare_pair(index=0), _compare_pair(index=1)], review_all=True, total=2)
    assert "selected.add(it.index)" in html


def test_compare_page_colour_pill_field_used():
    html = render_compare_page("tok", [_compare_pair()], review_all=False, total=1)
    assert "it.colour" in html


def test_compare_grid_empty_list_does_not_raise():
    html = render_compare_page("tok", [], review_all=True, total=0)
    assert "<!doctype html" in html
    assert _extract_items(html) == []


def test_compare_summary_empty_list_does_not_raise():
    html = render_compare_page("tok", [], review_all=False, total=0)
    assert "<!doctype html" in html
    assert _extract_items(html) == []


# --- modal / keyboard behaviour, copied idiom from video_review.py -----------


def test_select_page_modal_closes_on_escape_and_backdrop():
    html = render_select_page("tok", [_select_item()])
    assert 'e.key === "Escape"' in html
    assert 'getElementById("backdrop").onclick = closeModal' in html


def test_select_page_modal_releases_decoder_on_close():
    html = render_select_page("tok", [_select_item()])
    assert "player.removeAttribute(\"src\")" in html
    assert "player.load();" in html


def test_compare_grid_modal_closes_on_escape_and_backdrop():
    html = render_compare_page("tok", [_compare_pair()], review_all=True, total=1)
    assert 'e.key === "Escape"' in html
    assert 'getElementById("backdrop").onclick = closeModal' in html


def test_focus_visible_style_present_for_keyboard_users():
    html = render_select_page("tok", [_select_item()])
    assert "focus-visible" in html
