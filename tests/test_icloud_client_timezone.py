"""asset_timezone_offset: best-effort local-offset extraction for date stamping.

See icloud_photo_sync.metadata — a video's embedded creationdate is written
in local time-with-offset when the source engine can tell us one; this is
where that offset comes from, and it must never raise regardless of what
shape (or absence) of ``_asset_record`` an asset carries.
"""

import types

from pyicloud.common.cloudkit.models import CKRecord, CKZoneID

from icloud_photo_sync.icloud_client import asset_timezone_offset

PRIMARY = {"zoneName": "PrimarySync", "zoneType": "REGULAR_CUSTOM_ZONE"}


def _record(tz_value=None):
    fields = {"assetDate": {"type": "TIMESTAMP", "value": 1704067200000}}
    if tz_value is not None:
        fields["timeZoneOffset"] = {"type": "INT64", "value": tz_value}
    return CKRecord(
        recordName="A1", recordType="CPLAsset", recordChangeTag="tag-1",
        zoneID=CKZoneID(**PRIMARY), fields=fields,
    )


def test_reads_a_positive_offset():
    raw = types.SimpleNamespace(_asset_record=_record(39600))
    assert asset_timezone_offset(raw) == 39600


def test_reads_a_negative_offset():
    raw = types.SimpleNamespace(_asset_record=_record(-18000))
    assert asset_timezone_offset(raw) == -18000


def test_none_when_field_absent():
    raw = types.SimpleNamespace(_asset_record=_record())
    assert asset_timezone_offset(raw) is None


def test_none_when_no_asset_record_attribute():
    assert asset_timezone_offset(types.SimpleNamespace()) is None


def test_none_when_asset_record_is_none():
    assert asset_timezone_offset(types.SimpleNamespace(_asset_record=None)) is None


def test_never_raises_on_a_completely_unexpected_raw():
    assert asset_timezone_offset(object()) is None
    assert asset_timezone_offset(None) is None


def test_never_raises_when_record_field_value_itself_blows_up():
    class Explodes:
        def __getattr__(self, name):
            raise RuntimeError("pyicloud shape changed")

    raw = types.SimpleNamespace(_asset_record=Explodes())
    assert asset_timezone_offset(raw) is None
