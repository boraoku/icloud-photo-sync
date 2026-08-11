"""The iCloud deletion adapter, exercised entirely against fake CloudKit responses.

Nothing here touches a real account. The point of most of these tests is that a
delete is only ever reported as done when the server actually said so — pyicloud's
own ``PhotoAsset.delete()`` returns True unconditionally, which is exactly the
failure this layer exists to avoid.
"""

import base64
import types
from datetime import datetime, timezone

import pytest
from pyicloud.common.cloudkit.models import (
    CKErrorItem,
    CKRecord,
    CKTombstoneRecord,
    CKZoneID,
)

from icloud_photo_sync.config import AppConfig
from icloud_photo_sync.icloud_client import ICloudClient

PRIMARY = {"zoneName": "PrimarySync", "zoneType": "REGULAR_CUSTOM_ZONE"}


# --- fakes --------------------------------------------------------------------


def asset_record(name="A1", *, tag="tag-1", master="M1", deleted=0, expunged=0,
                 date_ms=1704067200000, zone=None):
    return CKRecord(
        recordName=name, recordType="CPLAsset", recordChangeTag=tag,
        zoneID=CKZoneID(**(zone or PRIMARY)),
        fields={
            "isDeleted": {"type": "INT64", "value": deleted},
            "isExpunged": {"type": "INT64", "value": expunged},
            "assetDate": {"type": "TIMESTAMP", "value": date_ms},
            "masterRef": {"type": "REFERENCE",
                          "value": {"recordName": master, "action": "NONE"}},
        },
    )


def master_record(name="M1", *, filename="IMG_1.MOV", size=4242):
    return CKRecord(
        recordName=name, recordType="CPLMaster",
        fields={
            "filenameEnc": {"type": "STRING",
                            "value": base64.b64encode(filename.encode()).decode()},
            "resOriginalRes": {"type": "ASSETID", "value": {"size": size}},
        },
    )


class FakeCloudKit:
    """Records what was asked of it and replays canned responses."""

    def __init__(self, records=(), modify_records=None, modify_raises=None):
        self.by_name = {r.recordName: r for r in records}
        self.modify_records = modify_records
        self.modify_raises = modify_raises
        self.lookups: list[list[str]] = []
        self.modifies: list[dict] = []

    def lookup(self, *, record_names, zone_id, desired_keys=None):
        self.lookups.append(list(record_names))
        out = []
        for name in record_names:
            record = self.by_name.get(name)
            out.append(record if record is not None else CKErrorItem(
                serverErrorCode="NOT_FOUND", reason="not found", recordName=name))
        return types.SimpleNamespace(records=out)

    def modify(self, *, operations, zone_id, atomic=None):
        self.modifies.append({"operations": operations, "atomic": atomic,
                              "zone": zone_id})
        if self.modify_raises is not None:
            raise self.modify_raises
        if self.modify_records is not None:
            return types.SimpleNamespace(records=list(self.modify_records))
        # Default: the server applied every update.
        return types.SimpleNamespace(records=[
            asset_record(op.record.recordName, deleted=1) for op in operations
        ])


class FakePhotos:
    def __init__(self, client):
        self._private_client = client
        self._root_library = types.SimpleNamespace(zone_id=dict(PRIMARY))

    @property
    def all(self):
        # album.get(id) degrades to a full-library enumeration on a miss, so
        # deletion must never reach for it. Make that impossible to do quietly.
        raise AssertionError("deletion must never enumerate the library")


class FakeService:
    def __init__(self, client):
        self.photos = FakePhotos(client)


def make_client(tmp_path, cloudkit):
    cfg = AppConfig.create("t@e.com", tmp_path / "out", config_root=tmp_path / "cfg")
    return ICloudClient(FakeService(cloudkit), cfg)


# --- capability probe ---------------------------------------------------------


def test_supports_delete_true_with_a_typed_client(tmp_path):
    assert make_client(tmp_path, FakeCloudKit()).supports_delete() is True


def test_supports_delete_false_without_one(tmp_path):
    """Engine drift must be an honest refusal before the user trashes anything."""
    client = make_client(tmp_path, FakeCloudKit())
    client._service.photos._private_client = object()
    assert client.supports_delete() is False


# --- lookup -------------------------------------------------------------------


def test_lookup_joins_asset_and_master_records(tmp_path):
    ck = FakeCloudKit([asset_record(), master_record(filename="IMG_9.MOV", size=99)])
    found, missing = make_client(tmp_path, ck).lookup_assets(["A1"])

    assert missing == []
    remote = found["A1"]
    assert remote.filename == "IMG_9.MOV"      # filename lives on the master
    assert remote.size == 99                   # so does the original size
    assert remote.capture_dt == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert remote.change_tag == "tag-1"
    assert remote.is_deleted is False
    assert ck.lookups == [["A1"], ["M1"]]      # two batched calls, no enumeration


def test_lookup_reports_unknown_ids_as_missing(tmp_path):
    ck = FakeCloudKit([asset_record("A1"), master_record("M1")])
    found, missing = make_client(tmp_path, ck).lookup_assets(["A1", "GONE"])

    assert set(found) == {"A1"}
    assert missing == ["GONE"]


def test_lookup_treats_a_tombstone_as_missing(tmp_path):
    class Tombstoned(FakeCloudKit):
        def lookup(self, *, record_names, zone_id, desired_keys=None):
            self.lookups.append(list(record_names))
            return types.SimpleNamespace(records=[
                CKTombstoneRecord(recordName=record_names[0], deleted=True)])

    found, missing = make_client(tmp_path, Tombstoned()).lookup_assets(["A1"])
    assert found == {} and missing == ["A1"]


def test_lookup_flags_a_shared_library_zone(tmp_path):
    shared = {"zoneName": "SharedSync-ABC", "zoneType": "REGULAR_CUSTOM_ZONE"}
    ck = FakeCloudKit([asset_record(zone=shared), master_record()])
    found, _ = make_client(tmp_path, ck).lookup_assets(["A1"])

    # Someone else's library is involved; the policy layer refuses these.
    assert found["A1"].in_shared_library is True


def test_lookup_batches_in_chunks(tmp_path):
    ids = [f"A{i}" for i in range(5)]
    ck = FakeCloudKit([asset_record(i, master=f"M{i}") for i in ids]
                      + [master_record(f"M{i}") for i in ids])
    make_client(tmp_path, ck).lookup_assets(ids, chunk=2)

    assert [len(call) for call in ck.lookups] == [2, 2, 2, 2, 1, 1]


# --- delete: the response is the only evidence --------------------------------


def test_delete_sends_the_fresh_change_tag_and_is_not_atomic(tmp_path):
    ck = FakeCloudKit([asset_record(), master_record()])
    client = make_client(tmp_path, ck)
    found, _ = client.lookup_assets(["A1"])

    assert client.delete_assets([found["A1"]])[0].ok is True

    call = ck.modifies[0]
    assert call["atomic"] is False            # one bad record must not sink the batch
    record = call["operations"][0].record
    assert record.recordChangeTag == "tag-1"  # the tag we just read, not a cached one
    assert record.recordName == "A1"
    assert record.recordType == "CPLAsset"
    assert record.fields.get_value("isDeleted") == 1


def test_delete_reports_a_per_record_error(tmp_path):
    ck = FakeCloudKit(
        [asset_record(), master_record()],
        modify_records=[CKErrorItem(serverErrorCode="CONFLICT",
                                    reason="record change tag is stale",
                                    recordName="A1")],
    )
    client = make_client(tmp_path, ck)
    found, _ = client.lookup_assets(["A1"])

    result = client.delete_assets([found["A1"]])[0]
    assert result.ok is False
    assert "CONFLICT" in result.error and "stale" in result.error


def test_delete_reports_a_record_the_server_did_not_actually_change(tmp_path):
    """The regression pyicloud's own delete() cannot catch: 200 OK, nothing done."""
    ck = FakeCloudKit(
        [asset_record(), master_record()],
        modify_records=[asset_record("A1", deleted=0)],   # still not deleted
    )
    client = make_client(tmp_path, ck)
    found, _ = client.lookup_assets(["A1"])

    result = client.delete_assets([found["A1"]])[0]
    assert result.ok is False
    assert "did not apply" in result.error


def test_delete_reports_an_asset_the_response_omits(tmp_path):
    ck = FakeCloudKit([asset_record(), master_record()], modify_records=[])
    client = make_client(tmp_path, ck)
    found, _ = client.lookup_assets(["A1"])

    result = client.delete_assets([found["A1"]])[0]
    assert result.ok is False
    assert "no per-record outcome" in result.error


def test_delete_treats_a_tombstoned_record_as_already_gone(tmp_path):
    ck = FakeCloudKit(
        [asset_record(), master_record()],
        modify_records=[CKTombstoneRecord(recordName="A1", deleted=True)],
    )
    client = make_client(tmp_path, ck)
    found, _ = client.lookup_assets(["A1"])

    result = client.delete_assets([found["A1"]])[0]
    assert result.ok is True and result.already_deleted is True


def test_delete_maps_api_failures_to_our_error_types(tmp_path):
    from pyicloud.exceptions import PyiCloudServiceUnavailable

    from icloud_photo_sync.errors import ServiceUnavailableError

    ck = FakeCloudKit([asset_record(), master_record()],
                      modify_raises=PyiCloudServiceUnavailable("503"))
    client = make_client(tmp_path, ck)
    found, _ = client.lookup_assets(["A1"])

    with pytest.raises(ServiceUnavailableError):
        client.delete_assets([found["A1"]])


def test_delete_of_nothing_touches_the_network(tmp_path):
    ck = FakeCloudKit()
    assert make_client(tmp_path, ck).delete_assets([]) == []
    assert ck.modifies == []


# --- verification: "the API accepted it" is not "it happened" -----------------


def test_verify_confirms_a_deleted_asset(tmp_path):
    ck = FakeCloudKit([asset_record(deleted=1), master_record()])
    assert make_client(tmp_path, ck).verify_deleted(["A1"]) == {"A1": True}


def test_verify_catches_an_asset_that_is_still_there(tmp_path):
    ck = FakeCloudKit([asset_record(deleted=0), master_record()])
    assert make_client(tmp_path, ck).verify_deleted(["A1"]) == {"A1": False}


def test_verify_counts_a_vanished_asset_as_deleted(tmp_path):
    assert make_client(tmp_path, FakeCloudKit()).verify_deleted(["A1"]) == {"A1": True}


# --- the forbidden shortcuts --------------------------------------------------


def test_package_never_calls_pyicloud_delete_or_album_get():
    """Both shortcuts are silently wrong; keep them out by construction.

    ``PhotoAsset.delete()`` cannot report failure, and ``album.get(id)`` walks
    the entire library when an id is missing.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "icloud_photo_sync"
    offenders = []
    for source in package.glob("*.py"):
        # Parse rather than grep: both names are discussed in the docstrings,
        # and it is the *calls* that must not exist.
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func
            if target.attr == "delete" and not node.args and not node.keywords:
                offenders.append(f"{source.name}:{node.lineno} .delete()")
            if target.attr == "get" and isinstance(target.value, ast.Attribute) \
                    and target.value.attr == "all":
                offenders.append(f"{source.name}:{node.lineno} .all.get()")
    assert offenders == []
