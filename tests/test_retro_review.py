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
