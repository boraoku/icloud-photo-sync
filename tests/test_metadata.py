"""Tests for icloud_photo_sync.metadata: presence checks, stamping, mtime.

No real ffmpeg/ffprobe/exiftool calls: every test monkeypatches
``subprocess.run`` and ``shutil.which``, so the whole file passes on a
machine with none of those tools installed.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from icloud_photo_sync import metadata as md


@pytest.fixture(autouse=True)
def _clear_tool_cache():
    """Availability is memoised across calls; tests must not see each other."""
    md._tool_cache.clear()
    yield
    md._tool_cache.clear()


def _which_present(*names: str):
    present = set(names)

    def which(name):
        return f"/usr/bin/{name}" if name in present else None

    return which


CAPTURE_DT = datetime(2019, 10, 25, 15, 7, 50, tzinfo=timezone.utc)


# --- availability ----------------------------------------------------------------


def test_exiftool_available_reflects_which_and_is_cached(monkeypatch):
    calls = []

    def which(name):
        calls.append(name)
        return "/usr/bin/exiftool" if name == "exiftool" else None

    monkeypatch.setattr(md.shutil, "which", which)

    assert md.exiftool_available() is True
    assert md.exiftool_available() is True
    assert calls == ["exiftool"]                # second call served from cache


def test_ffmpeg_pair_available_requires_both_tools(monkeypatch):
    monkeypatch.setattr(md.shutil, "which", _which_present("ffmpeg"))
    assert md.ffmpeg_pair_available() is False

    md._tool_cache.clear()
    monkeypatch.setattr(md.shutil, "which", _which_present("ffmpeg", "ffprobe"))
    assert md.ffmpeg_pair_available() is True


# --- _video_has_date ---------------------------------------------------------------


def _ffprobe_tags_run(tags):
    def run(argv, **kwargs):
        payload = {"format": {"tags": tags}} if tags is not None else {"format": {}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    return run


def test_video_has_date_true_when_creation_time_present(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(subprocess, "run", _ffprobe_tags_run({"creation_time": "2019-10-25T15:07:50Z"}))
    assert md._video_has_date(src) is True


def test_video_has_date_true_when_only_quicktime_tag_present(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        _ffprobe_tags_run({"com.apple.quicktime.creationdate": "2019-10-25T18:07:50+0300"}),
    )
    assert md._video_has_date(src) is True


def test_video_has_date_false_when_neither_tag_present(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(subprocess, "run", _ffprobe_tags_run({}))
    assert md._video_has_date(src) is False


def test_video_has_date_none_on_nonzero_exit(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert md._video_has_date(src) is None


def test_video_has_date_none_on_subprocess_error(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")

    def run(argv, **kw):
        raise OSError("ffprobe not found")

    monkeypatch.setattr(subprocess, "run", run)
    assert md._video_has_date(src) is None


def test_video_has_date_none_on_malformed_json(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )
    assert md._video_has_date(src) is None


# --- _image_has_date ---------------------------------------------------------------


def test_image_has_date_true_when_a_tag_line_is_non_blank(tmp_path, monkeypatch):
    src = tmp_path / "photo.heic"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="2019:10:25 15:07:50\n\n", stderr=""),
    )
    assert md._image_has_date(src) is True


def test_image_has_date_false_when_both_lines_blank(tmp_path, monkeypatch):
    src = tmp_path / "photo.heic"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="\n\n", stderr=""),
    )
    assert md._image_has_date(src) is False


def test_image_has_date_none_on_nonzero_exit(tmp_path, monkeypatch):
    src = tmp_path / "photo.heic"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert md._image_has_date(src) is None


# --- _stamp_video ------------------------------------------------------------------


def test_stamp_video_names_the_container_explicitly(tmp_path, monkeypatch):
    """Regression: a bare ``.meta.part`` name gives ffmpeg nothing to infer a
    container from (verified against real ffmpeg: it refuses outright)."""
    src = tmp_path / "clip.mov"
    src.write_bytes(b"original bytes")
    captured = {}

    def run(argv, **kw):
        captured["argv"] = argv
        part = tmp_path / "clip.mov.meta.part"
        part.write_bytes(b"remuxed bytes")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert md._stamp_video(src, CAPTURE_DT, None) is True
    assert src.read_bytes() == b"remuxed bytes"
    assert not (tmp_path / "clip.mov.meta.part").exists()
    argv = captured["argv"]
    assert argv[-3:-1] == ["-f", "mov"]
    assert any(a.startswith("creation_time=2019-10-25T15:07:50Z") for a in argv)
    assert any(a.startswith("com.apple.quicktime.creationdate=2019-10-25T15:07:50+0000") for a in argv)


def test_stamp_video_uses_given_tz_offset(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    captured = {}

    def run(argv, **kw):
        captured["argv"] = argv
        (tmp_path / "clip.mov.meta.part").write_bytes(b"y")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    md._stamp_video(src, CAPTURE_DT, 11 * 3600)

    assert any(
        a.startswith("com.apple.quicktime.creationdate=2019-10-26T02:07:50+1100")
        for a in captured["argv"]
    )


def test_stamp_video_leaves_no_part_file_on_nonzero_exit(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"original")

    def run(argv, **kw):
        (tmp_path / "clip.mov.meta.part").write_bytes(b"partial")
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", run)

    assert md._stamp_video(src, CAPTURE_DT, None) is False
    assert src.read_bytes() == b"original"
    assert not (tmp_path / "clip.mov.meta.part").exists()


def test_stamp_video_leaves_no_part_file_on_subprocess_error(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"original")

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(subprocess, "run", run)

    assert md._stamp_video(src, CAPTURE_DT, None) is False
    assert src.read_bytes() == b"original"


# --- _stamp_image ------------------------------------------------------------------


def test_stamp_image_writes_via_dash_o_and_swaps_in(tmp_path, monkeypatch):
    src = tmp_path / "photo.heic"
    src.write_bytes(b"original")
    captured = {}

    def run(argv, **kw):
        captured["argv"] = argv
        (tmp_path / "photo.heic.meta.part").write_bytes(b"stamped")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert md._stamp_image(src, CAPTURE_DT, None) is True
    assert src.read_bytes() == b"stamped"
    assert not (tmp_path / "photo.heic.meta.part").exists()
    argv = captured["argv"]
    assert "-o" in argv
    assert any(a == "-DateTimeOriginal=2019:10:25 15:07:50" for a in argv)


def test_stamp_image_leaves_no_part_file_on_failure(tmp_path, monkeypatch):
    src = tmp_path / "photo.heic"
    src.write_bytes(b"original")

    def run(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", run)

    assert md._stamp_image(src, CAPTURE_DT, None) is False
    assert src.read_bytes() == b"original"
    assert not (tmp_path / "photo.heic.meta.part").exists()


# --- ensure_capture_date -----------------------------------------------------------


def _mtime(path):
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def test_unsupported_type_sets_mtime_only(tmp_path, monkeypatch):
    src = tmp_path / "notes.txt"
    src.write_bytes(b"x")
    monkeypatch.setattr(md.shutil, "which", _which_present())

    outcome = md.ensure_capture_date(src, CAPTURE_DT)

    assert outcome is md.MetadataOutcome.UNSUPPORTED_TYPE
    assert _mtime(src) == CAPTURE_DT


def test_tool_unavailable_still_sets_mtime(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(md.shutil, "which", _which_present())          # neither tool

    outcome = md.ensure_capture_date(src, CAPTURE_DT)

    assert outcome is md.MetadataOutcome.TOOL_UNAVAILABLE
    assert _mtime(src) == CAPTURE_DT


def test_already_present_video_is_never_stamped(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(md.shutil, "which", _which_present("ffmpeg", "ffprobe"))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"format": {"tags": {"creation_time": "already there"}}}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)

    outcome = md.ensure_capture_date(src, CAPTURE_DT)

    assert outcome is md.MetadataOutcome.ALREADY_PRESENT
    assert len(calls) == 1                       # only the read, never a write
    assert _mtime(src) == CAPTURE_DT              # mtime still set unconditionally


def test_missing_video_date_is_stamped_and_final_mtime_is_the_capture_date(tmp_path, monkeypatch):
    """Regression for the bug caught in live testing: a successful stamp
    replaces the file via os.replace() with a freshly-written temp file whose
    own mtime is "now". If the mtime were set *before* that swap, the swap
    would silently clobber it — this asserts the final file carries the true
    capture date, not the moment the stamp subprocess ran.
    """
    src = tmp_path / "clip.mov"
    src.write_bytes(b"original")
    monkeypatch.setattr(md.shutil, "which", _which_present("ffmpeg", "ffprobe"))

    def run(argv, **kw):
        if argv[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"format": {"tags": {}}}), stderr="")
        # ffmpeg: simulate a freshly-written temp file with a "wrong" mtime,
        # the way a real ffmpeg process naturally would.
        part = tmp_path / "clip.mov.meta.part"
        part.write_bytes(b"remuxed")
        wrong = datetime(2026, 8, 23, tzinfo=timezone.utc).timestamp()
        os.utime(part, (wrong, wrong))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    outcome = md.ensure_capture_date(src, CAPTURE_DT, tz_offset_seconds=11 * 3600)

    assert outcome is md.MetadataOutcome.STAMPED
    assert src.read_bytes() == b"remuxed"
    assert _mtime(src) == CAPTURE_DT


def test_ensure_capture_date_sets_mtime_even_when_stamp_fails(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(md.shutil, "which", _which_present("ffmpeg", "ffprobe"))

    def run(argv, **kw):
        if argv[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"format": {"tags": {}}}), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", run)

    outcome = md.ensure_capture_date(src, CAPTURE_DT)

    assert outcome is md.MetadataOutcome.FAILED
    assert _mtime(src) == CAPTURE_DT


def test_ensure_capture_date_never_raises_when_utime_fails(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(md.shutil, "which", _which_present())

    def bad_utime(path, times):
        raise OSError("no permission")

    monkeypatch.setattr(md.os, "utime", bad_utime)

    outcome = md.ensure_capture_date(src, CAPTURE_DT)
    assert outcome is md.MetadataOutcome.TOOL_UNAVAILABLE


def test_missing_image_date_is_stamped(tmp_path, monkeypatch):
    src = tmp_path / "photo.heic"
    src.write_bytes(b"original")
    monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))

    def run(argv, **kw):
        if "-s3" in argv:
            return SimpleNamespace(returncode=0, stdout="\n\n", stderr="")
        part = tmp_path / "photo.heic.meta.part"
        part.write_bytes(b"stamped")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    outcome = md.ensure_capture_date(src, CAPTURE_DT)

    assert outcome is md.MetadataOutcome.STAMPED
    assert src.read_bytes() == b"stamped"
    assert _mtime(src) == CAPTURE_DT


def test_already_present_image_is_never_stamped(tmp_path, monkeypatch):
    src = tmp_path / "photo.heic"
    src.write_bytes(b"x")
    monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="2019:10:25 15:07:50\n\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    outcome = md.ensure_capture_date(src, CAPTURE_DT)

    assert outcome is md.MetadataOutcome.ALREADY_PRESENT
    assert len(calls) == 1


# --- read_embedded_capture_date -----------------------------------------------


def test_read_video_embedded_date_prefers_creation_time(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        _ffprobe_tags_run({
            "creation_time": "2019-10-25T15:07:50Z",
            "com.apple.quicktime.creationdate": "2019-10-25T18:07:50+0300",
        }),
    )
    assert md.read_embedded_capture_date(src) == datetime(2019, 10, 25, 15, 7, 50, tzinfo=timezone.utc)


def test_read_video_embedded_date_falls_back_to_quicktime_tag(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        _ffprobe_tags_run({"com.apple.quicktime.creationdate": "2019-10-25T18:07:50+0300"}),
    )
    dt = md.read_embedded_capture_date(src)
    assert dt == datetime(2019, 10, 25, 18, 7, 50, tzinfo=dt.tzinfo)
    assert dt.utcoffset().total_seconds() == 3 * 3600


def test_read_video_embedded_date_none_when_neither_tag_present(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    monkeypatch.setattr(subprocess, "run", _ffprobe_tags_run({}))
    assert md.read_embedded_capture_date(src) is None


def test_read_image_embedded_date(tmp_path, monkeypatch):
    src = tmp_path / "photo.heic"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="2019:10:25 15:07:50\n\n", stderr=""),
    )
    assert md.read_embedded_capture_date(src) == datetime(2019, 10, 25, 15, 7, 50, tzinfo=timezone.utc)


def test_read_embedded_date_none_for_unsupported_suffix(tmp_path):
    assert md.read_embedded_capture_date(tmp_path / "notes.txt") is None


# --- infer_capture_date_from_name ----------------------------------------------


class TestInferFromName:
    def test_whatsapp_image_name_gets_noon(self):
        dt = md.infer_capture_date_from_name("IMG-20191025-WA0007.jpg")
        assert dt == datetime(2019, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_whatsapp_video_name_gets_noon(self):
        dt = md.infer_capture_date_from_name("VID-20191025-WA0003.mp4")
        assert dt == datetime(2019, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_camera_datetime_name_keeps_the_exact_time(self):
        dt = md.infer_capture_date_from_name("IMG_20191025_150712.jpg")
        assert dt == datetime(2019, 10, 25, 15, 7, 12, tzinfo=timezone.utc)

    def test_bare_datetime_name_with_no_img_prefix(self):
        dt = md.infer_capture_date_from_name("20191025_150712.mp4")
        assert dt == datetime(2019, 10, 25, 15, 7, 12, tzinfo=timezone.utc)

    def test_datetime_pattern_preferred_over_date_only_when_both_could_apply(self):
        # Not a realistic filename, but pins the stated precedence: the more
        # informative match wins when the regexes could both fire.
        dt = md.infer_capture_date_from_name("20191025_150712-WA0001.jpg")
        assert dt == datetime(2019, 10, 25, 15, 7, 12, tzinfo=timezone.utc)

    def test_no_recognisable_pattern_is_unknown(self):
        assert md.infer_capture_date_from_name("holiday_photo_final_v2.jpg") is None

    def test_invalid_calendar_date_in_camera_pattern_is_rejected(self):
        # Month 13 — looks like the shape, isn't a real date.
        assert md.infer_capture_date_from_name("IMG_20191325_150712.jpg") is None

    def test_invalid_calendar_date_in_whatsapp_pattern_is_rejected(self):
        assert md.infer_capture_date_from_name("IMG-20190231-WA0007.jpg") is None  # Feb 31

    def test_coincidental_eight_digits_without_the_right_shape_is_not_matched(self):
        # A serial number or resolution tag must not masquerade as a date.
        assert md.infer_capture_date_from_name("DSC12345678.jpg") is None


# --- infer_capture_date_from_path -----------------------------------------------


class TestInferFromPath:
    def test_year_month_folder_gives_the_15th_at_noon_utc(self):
        dt = md.infer_capture_date_from_path("2019/10/clip.mp4")
        assert dt == datetime(2019, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_nested_deeper_still_uses_the_first_two_parts(self):
        dt = md.infer_capture_date_from_path("2019/10/subalbum/clip.mp4")
        assert dt == datetime(2019, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_invalid_month_is_rejected(self):
        assert md.infer_capture_date_from_path("2019/13/clip.mp4") is None

    def test_unknown_date_folder_is_not_a_year(self):
        assert md.infer_capture_date_from_path("unknown-date/clip.mp4") is None

    def test_flat_file_with_no_folder_at_all(self):
        assert md.infer_capture_date_from_path("clip.mp4") is None

    def test_a_plausible_looking_but_out_of_range_year_is_rejected(self):
        assert md.infer_capture_date_from_path("1899/10/clip.mp4") is None


# --- infer_capture_date (the full ladder) ---------------------------------------


class TestInferCaptureDate:
    def test_embedded_date_wins_over_filename_and_folder(self, tmp_path, monkeypatch):
        src = tmp_path / "IMG-20200101-WA0001.mov"
        src.write_bytes(b"x")
        monkeypatch.setattr(
            subprocess, "run",
            _ffprobe_tags_run({"creation_time": "2019-10-25T15:07:50Z"}),
        )
        dt, source = md.infer_capture_date(src, "2021/06/IMG-20200101-WA0001.mov")
        assert dt == datetime(2019, 10, 25, 15, 7, 50, tzinfo=timezone.utc)
        assert source == md.SOURCE_EMBEDDED

    def test_filename_wins_over_folder_when_no_embedded_date(self, tmp_path, monkeypatch):
        src = tmp_path / "IMG-20191025-WA0007.jpg"
        src.write_bytes(b"x")
        monkeypatch.setattr(
            subprocess, "run",
            lambda argv, **kw: SimpleNamespace(returncode=0, stdout="\n\n", stderr=""),
        )
        dt, source = md.infer_capture_date(src, "2021/06/IMG-20191025-WA0007.jpg")
        assert dt == datetime(2019, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
        assert source == md.SOURCE_FILENAME

    def test_folder_is_the_last_resort(self, tmp_path, monkeypatch):
        src = tmp_path / "holiday.jpg"
        src.write_bytes(b"x")
        monkeypatch.setattr(
            subprocess, "run",
            lambda argv, **kw: SimpleNamespace(returncode=0, stdout="\n\n", stderr=""),
        )
        dt, source = md.infer_capture_date(src, "2021/06/holiday.jpg")
        assert dt == datetime(2021, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert source == md.SOURCE_FOLDER

    def test_nothing_found_anywhere_is_unknown(self, tmp_path, monkeypatch):
        src = tmp_path / "holiday.jpg"
        src.write_bytes(b"x")
        monkeypatch.setattr(
            subprocess, "run",
            lambda argv, **kw: SimpleNamespace(returncode=0, stdout="\n\n", stderr=""),
        )
        dt, source = md.infer_capture_date(src, "holiday.jpg")
        assert dt is None
        assert source == md.SOURCE_UNKNOWN

    def test_unsupported_suffix_skips_straight_to_filename(self, tmp_path):
        src = tmp_path / "IMG-20191025-WA0007.txt"
        src.write_bytes(b"x")
        dt, source = md.infer_capture_date(src, "2021/06/IMG-20191025-WA0007.txt")
        assert dt == datetime(2019, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
        assert source == md.SOURCE_FILENAME


# --- read_embedded_capture_dates (the bulk read) --------------------------------


def _exiftool_batch_run(by_name, *, returncode=0, calls=None, stdout=None):
    """A subprocess double standing in for ``exiftool -@ argfile``. Reads the
    argfile the way the real tool would, so the test also proves the file was
    written correctly rather than only that the JSON was parsed."""
    def run(argv, **kw):
        if calls is not None:
            calls.append(argv)
        assert argv[0] == "exiftool" and argv[1] == "-@"
        lines = Path(argv[2]).read_text(encoding="utf-8").splitlines()
        paths = [ln for ln in lines if not ln.startswith("-")]
        if stdout is not None:
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
        entries = []
        for p in paths:
            entry = {"SourceFile": p}
            value = by_name.get(Path(p).name)
            if value:
                entry["DateTimeOriginal"] = value
            entries.append(entry)
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(entries), stderr="")
    return run


class TestReadEmbeddedCaptureDates:
    def _photos(self, tmp_path, *names):
        out = []
        for n in names:
            p = tmp_path / n
            p.write_bytes(b"x")
            out.append(p)
        return out

    def test_reads_a_whole_batch_in_one_subprocess(self, tmp_path, monkeypatch):
        paths = self._photos(tmp_path, "a.heic", "b.heic", "c.heic")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        calls = []
        monkeypatch.setattr(subprocess, "run", _exiftool_batch_run(
            {"a.heic": "2019:10:25 15:07:50", "c.heic": "2020:01:02 03:04:05"},
            calls=calls))

        result = md.read_embedded_capture_dates(paths)

        assert len(calls) == 1                       # one process for all three
        assert result[paths[0]] == datetime(2019, 10, 25, 15, 7, 50, tzinfo=timezone.utc)
        assert result[paths[1]] is None              # present in the batch, no date
        assert result[paths[2]] == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def test_every_requested_path_appears_in_the_result(self, tmp_path, monkeypatch):
        paths = self._photos(tmp_path, "a.heic", "b.heic")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        monkeypatch.setattr(subprocess, "run", _exiftool_batch_run({}))

        result = md.read_embedded_capture_dates(paths)

        assert set(result) == set(paths)

    def test_chunks_at_the_batch_size(self, tmp_path, monkeypatch):
        paths = self._photos(tmp_path, *[f"p{i}.heic" for i in range(5)])
        monkeypatch.setattr(md, "EXIFTOOL_BATCH", 2)
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        calls = []
        monkeypatch.setattr(subprocess, "run", _exiftool_batch_run({}, calls=calls))

        md.read_embedded_capture_dates(paths)

        assert len(calls) == 3                       # 2 + 2 + 1

    def test_non_image_suffixes_are_never_sent_to_exiftool(self, tmp_path, monkeypatch):
        paths = self._photos(tmp_path, "a.heic", "clip.mov", "notes.txt")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        sent = []

        def run(argv, **kw):
            sent.extend(
                ln for ln in Path(argv[2]).read_text(encoding="utf-8").splitlines()
                if not ln.startswith("-")
            )
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = md.read_embedded_capture_dates(paths)

        assert [Path(s).name for s in sent] == ["a.heic"]
        assert result[paths[1]] is None and result[paths[2]] is None

    def test_a_nonzero_exit_still_uses_the_json_it_produced(self, tmp_path, monkeypatch):
        # exiftool exits 1 when ANY file in a batch is unreadable, while still
        # emitting good records for the rest. Discarding those would make one
        # bad photo blank out its whole batch.
        paths = self._photos(tmp_path, "good.heic", "bad.heic")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        monkeypatch.setattr(subprocess, "run", _exiftool_batch_run(
            {"good.heic": "2019:10:25 15:07:50"}, returncode=1))

        result = md.read_embedded_capture_dates(paths)

        assert result[paths[0]] == datetime(2019, 10, 25, 15, 7, 50, tzinfo=timezone.utc)
        assert result[paths[1]] is None

    def test_unparseable_json_reports_the_batch_as_dateless(self, tmp_path, monkeypatch):
        paths = self._photos(tmp_path, "a.heic")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        monkeypatch.setattr(subprocess, "run",
                            _exiftool_batch_run({}, stdout="not json at all"))

        assert md.read_embedded_capture_dates(paths) == {paths[0]: None}

    def test_a_crashing_subprocess_reports_the_batch_as_dateless(self, tmp_path, monkeypatch):
        paths = self._photos(tmp_path, "a.heic")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))

        def run(argv, **kw):
            raise OSError("exiftool vanished")

        monkeypatch.setattr(subprocess, "run", run)
        assert md.read_embedded_capture_dates(paths) == {paths[0]: None}

    def test_leaves_no_argfile_behind(self, tmp_path, monkeypatch):
        paths = self._photos(tmp_path, "a.heic")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        seen = []
        monkeypatch.setattr(subprocess, "run", _exiftool_batch_run({}, calls=seen))

        md.read_embedded_capture_dates(paths)

        assert not Path(seen[0][2]).exists()

    def test_no_exiftool_means_every_path_is_dateless(self, tmp_path, monkeypatch):
        paths = self._photos(tmp_path, "a.heic")
        monkeypatch.setattr(md.shutil, "which", _which_present())
        monkeypatch.setattr(subprocess, "run", _never_called)

        assert md.read_embedded_capture_dates(paths) == {paths[0]: None}

    def test_empty_input(self):
        assert md.read_embedded_capture_dates([]) == {}


def _never_called(*a, **kw):
    raise AssertionError("subprocess.run must not be called here")


# --- known_absent ----------------------------------------------------------------


class TestKnownAbsent:
    """The bulk read has already established absence, so the per-file
    presence check would re-read the same file for the same answer."""

    def test_known_absent_skips_the_presence_read(self, tmp_path, monkeypatch):
        src = tmp_path / "photo.heic"
        src.write_bytes(b"original")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        argvs = []

        def run(argv, **kw):
            argvs.append(argv)
            (tmp_path / "photo.heic.meta.part").write_bytes(b"stamped")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)

        outcome = md.ensure_capture_date(src, CAPTURE_DT, known_absent=True)

        assert outcome is md.MetadataOutcome.STAMPED
        assert len(argvs) == 1                       # the write only
        assert "-s3" not in argvs[0]                 # no read happened

    def test_default_still_reads_before_writing(self, tmp_path, monkeypatch):
        src = tmp_path / "photo.heic"
        src.write_bytes(b"original")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        argvs = []

        def run(argv, **kw):
            argvs.append(argv)
            if "-s3" in argv:
                return SimpleNamespace(returncode=0, stdout="\n\n", stderr="")
            (tmp_path / "photo.heic.meta.part").write_bytes(b"stamped")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)

        md.ensure_capture_date(src, CAPTURE_DT)

        assert "-s3" in argvs[0]                     # the read-before-write fence
        assert len(argvs) == 2

    def test_known_absent_never_bypasses_an_existing_date_for_other_callers(self, tmp_path, monkeypatch):
        # The fence that matters: default callers are untouched by this
        # parameter existing, and still refuse to overwrite a real date.
        src = tmp_path / "photo.heic"
        src.write_bytes(b"original")
        monkeypatch.setattr(md.shutil, "which", _which_present("exiftool"))
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout="2019:10:25 15:07:50\n\n", stderr=""))

        assert md.ensure_capture_date(src, CAPTURE_DT) is md.MetadataOutcome.ALREADY_PRESENT
        assert src.read_bytes() == b"original"
