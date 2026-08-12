"""Showing a retrospective plan before acting on it.

The local files are gone, so the review runs on iCloud's own thumbnails and on a
trash function that deliberately does nothing. These tests pin both halves: that
the page cannot be mistaken for a local-trash page, and that nothing in this path
can touch the filesystem.
"""

import base64

from pyicloud.common.cloudkit.models import CKRecord

from icloud_photo_sync import retro_review
from icloud_photo_sync.icloud_client import _thumb_url
from icloud_photo_sync.icloud_delete import Candidate
from icloud_photo_sync.review import render_page


def candidate(rel="2024/03/IMG_1.JPG", asset_id="a1", **kw):
    return Candidate(rel=rel, asset_id=asset_id, filename=rel.split("/")[-1],
                     capture_dt=None, expected_size=kw.pop("size", 100),
                     local_size=100, **kw)


class FakeClient:
    def __init__(self, *, thumbs=None, fail=()):
        self.thumbs = thumbs or {}
        self.fail = set(fail)
        self.fetched: list[str] = []

    def lookup_assets(self, asset_ids):
        found = {}
        for i in asset_ids:
            if i in self.thumbs:
                found[i] = type("R", (), {"thumb_url": self.thumbs[i]})()
        return found, [i for i in asset_ids if i not in found]

    def thumbnail_bytes(self, url, **kw):
        self.fetched.append(url)
        return None if url in self.fail else b"\xff\xd8jpegbytes"


# --- the thumbnail field --------------------------------------------------------


def test_the_thumbnail_url_is_read_off_the_master_record():
    master = CKRecord(
        recordName="M1", recordType="CPLMaster",
        fields={
            "filenameEnc": {"type": "STRING",
                            "value": base64.b64encode(b"IMG_1.JPG").decode()},
            "resJPEGThumbRes": {"type": "ASSETID",
                                "value": {"size": 1234,
                                          "downloadURL": "https://cdn/thumb.jpg"}},
        },
    )
    assert _thumb_url(master) == "https://cdn/thumb.jpg"


def test_a_master_without_a_thumbnail_yields_none():
    master = CKRecord(recordName="M1", recordType="CPLMaster", fields={})
    assert _thumb_url(master) is None
    assert _thumb_url(None) is None


# --- fetching -------------------------------------------------------------------


def test_thumbnails_are_written_by_index(tmp_path):
    client = FakeClient(thumbs={"a1": "https://cdn/1.jpg", "a2": "https://cdn/2.jpg"})
    cands = [candidate(rel="2024/03/A.JPG", asset_id="a1"),
             candidate(rel="2024/03/B.JPG", asset_id="a2")]

    written = retro_review.fetch_thumbnails(client, cands, tmp_path)

    assert set(written) == {"2024/03/A.JPG", "2024/03/B.JPG"}
    assert (tmp_path / "0.jpg").read_bytes().startswith(b"\xff\xd8")
    assert (tmp_path / "1.jpg").exists()


def test_a_thumbnail_that_will_not_load_is_not_fatal(tmp_path):
    """A blank tile is a better outcome than abandoning the review."""
    client = FakeClient(thumbs={"a1": "https://cdn/1.jpg", "a2": "https://cdn/2.jpg"},
                        fail={"https://cdn/1.jpg"})
    cands = [candidate(rel="2024/03/A.JPG", asset_id="a1"),
             candidate(rel="2024/03/B.JPG", asset_id="a2")]

    written = retro_review.fetch_thumbnails(client, cands, tmp_path)

    assert set(written) == {"2024/03/B.JPG"}
    assert not (tmp_path / "0.jpg").exists()


def test_an_asset_iCloud_no_longer_knows_is_skipped(tmp_path):
    client = FakeClient(thumbs={})
    assert retro_review.fetch_thumbnails(client, [candidate()], tmp_path) == {}


# --- the no-op trash function ----------------------------------------------------


def test_the_injected_trash_function_touches_nothing(tmp_path):
    """It reports success so the selection routes into outcome.icloud — but the
    paths it is handed do not exist, and it must never look at them."""
    ghost = tmp_path / "gone" / "IMG_1.JPG"
    results = retro_review._noop_trash([ghost])

    assert [r.ok for r in results] == [True]
    assert not ghost.exists() and not ghost.parent.exists()


def test_retro_review_does_not_import_the_trash_machinery():
    import ast
    import pathlib

    source = pathlib.Path(retro_review.__file__).read_text(encoding="utf-8")
    calls = {node.func.attr for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "move_to_trash" not in calls


# --- the page wording -------------------------------------------------------------


def test_the_retro_page_never_offers_to_move_anything_to_the_trash():
    page = render_page("tok", icloud_armed=True, retro=True)
    assert "const RETRO = true;" in page
    assert "Delete \" + selected.size + \" from iCloud" in page
    assert "already gone from your disk" in page


def test_the_ordinary_page_is_unchanged():
    page = render_page("tok", icloud_armed=True)
    assert "const RETRO = false;" in page
    assert "Move \" + selected.size + \" to Trash" in page


def test_a_retro_page_hides_the_per_round_icloud_opt_out():
    """Opting out of iCloud there would mean doing nothing at all."""
    page = render_page("tok", icloud_armed=True, retro=True)
    assert "if (RETRO) return ICLOUD_ARMED;" in page


# --- the thumbnail step must not be able to kill a run --------------------------


class CountingLookupClient(FakeClient):
    """Records when lookups happen relative to downloads."""

    def __init__(self, *, thumbs=None, fail=(), lookup_raises_after=None):
        super().__init__(thumbs=thumbs, fail=fail)
        self.events: list[str] = []
        self.lookups = 0
        self.lookup_raises_after = lookup_raises_after

    def lookup_assets(self, asset_ids):
        self.lookups += 1
        self.events.append("lookup")
        if (self.lookup_raises_after is not None
                and self.lookups > self.lookup_raises_after):
            raise RuntimeError("Unable to request PCS access!")
        return super().lookup_assets(asset_ids)

    def thumbnail_bytes(self, url, **kw):
        self.events.append("download")
        return super().thumbnail_bytes(url, **kw)


def many(n):
    return [candidate(rel=f"2024/03/IMG_{i}.JPG", asset_id=f"a{i}") for i in range(n)]


def test_every_lookup_happens_before_any_download(tmp_path):
    """Interleaving them is what stretched a few authenticated calls across
    fifteen minutes of transfers and tripped Apple's PCS consent."""
    cands = many(250)                       # 3 chunks of 100
    client = CountingLookupClient(
        thumbs={c.asset_id: f"https://cdn/{c.asset_id}.jpg" for c in cands})

    retro_review.fetch_thumbnails(client, cands, tmp_path, workers=4)

    assert client.lookups == 3
    assert client.events[:3] == ["lookup", "lookup", "lookup"]
    assert "lookup" not in client.events[3:]


def test_a_lookup_failure_costs_its_chunk_not_the_run(tmp_path):
    """This is the exact crash: chunk 12's lookup threw and took the whole
    scan with it, before the manifest had even been written."""
    cands = many(250)
    client = CountingLookupClient(
        thumbs={c.asset_id: f"https://cdn/{c.asset_id}.jpg" for c in cands},
        lookup_raises_after=1)

    written = retro_review.fetch_thumbnails(client, cands, tmp_path, workers=4)

    assert len(written) == 100          # the first chunk survived
    assert client.lookups == 3          # and it kept trying the rest


def test_a_total_lookup_failure_still_returns_a_reviewable_set(tmp_path):
    cands = many(10)
    client = CountingLookupClient(thumbs={}, lookup_raises_after=0)

    assert retro_review.fetch_thumbnails(client, cands, tmp_path, workers=2) == {}


def test_downloads_run_concurrently(tmp_path):
    import threading

    cands = many(16)
    peak = {"n": 0, "cur": 0}
    lock = threading.Lock()

    class SlowClient(FakeClient):
        def thumbnail_bytes(self, url, **kw):
            with lock:
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
            threading.Event().wait(0.02)
            with lock:
                peak["cur"] -= 1
            return b"\xff\xd8x"

    client = SlowClient(thumbs={c.asset_id: f"https://cdn/{c.asset_id}.jpg"
                                for c in cands})
    retro_review.fetch_thumbnails(client, cands, tmp_path, workers=4)

    assert peak["n"] > 1, "downloads were serialised"


# --- ending the session ---------------------------------------------------------


def test_finishing_is_not_disabled_by_the_stream_completing():
    """The page used to set `finished = true` as soon as the item stream ended,
    which made finishSession() bail out before ever POSTing /finish. The server
    then waited forever and the only way out was Ctrl-C — which discarded the
    selection. Everything published up front (a retrospective review) hit this
    on the very first poll."""
    page = render_page("tok", icloud_armed=True, retro=True)

    # finishSession() still guards on `finished`...
    assert "if (finished) return;" in page
    # ...so the poll loop must not set it when the stream ends.
    poll = page[page.index("async function poll()"):page.index("function wantsICloud")]
    assert "finished = true" not in poll, \
        "poll() marks the session finished; Finish will silently do nothing"
    assert "showNothingFlagged();" in poll      # it still stops polling


def test_a_completed_selection_still_auto_finishes():
    page = render_page("tok", icloud_armed=True, retro=True)
    assert "if (done && cards.size === 0) { finishSession(); return; }" in page


class Interrupting:
    """A review server whose wait is cut short by Ctrl-C."""

    def __init__(self, selected):
        from icloud_photo_sync.review import TrashOutcome
        self.outcome = TrashOutcome(moved=list(selected), icloud=list(selected))
        self.url = "http://127.0.0.1:0/"
        self.closed = False

    def publish(self, item): pass
    def set_progress(self, *a): pass
    def mark_done(self): pass
    def start(self): pass
    def close(self): self.closed = True
    def wait_finished(self): raise KeyboardInterrupt


def test_ctrl_c_keeps_a_selection_the_user_already_made(tmp_path, monkeypatch):
    """Closing the tab and hitting Ctrl-C used to throw away a real, recorded
    selection and report "nothing to delete"."""
    picked = ["2024/03/IMG_1.JPG", "2024/03/IMG_2.JPG"]
    monkeypatch.setattr(retro_review, "ReviewServer",
                        lambda **kw: Interrupting(picked))
    lines = []

    got = retro_review.review_candidates(
        FakeClient(thumbs={}), many(3), open_browser=False,
        echo=lambda m="", **k: lines.append(str(m)))

    assert got == frozenset(picked)
    assert any("keeping the 2 file(s)" in ln for ln in lines)


def test_ctrl_c_with_nothing_selected_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(retro_review, "ReviewServer", lambda **kw: Interrupting([]))
    lines = []

    got = retro_review.review_candidates(
        FakeClient(thumbs={}), many(3), open_browser=False,
        echo=lambda m="", **k: lines.append(str(m)))

    assert got == frozenset()
    assert any("nothing had been selected" in ln for ln in lines)
