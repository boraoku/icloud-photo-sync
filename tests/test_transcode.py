"""Tests for ffprobe/ffmpeg mechanics: probing, argv building, running the encode.

No real ffmpeg/ffprobe calls: every test monkeypatches ``subprocess.run`` (for
``probe``/availability) or injects a fake ``runner`` (for ``convert``), so the
whole file passes on a machine with no media tools installed at all.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from icloud_photo_sync import transcode
from icloud_photo_sync.video_optimise import Encode

# --- fixtures / helpers --------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_encoder_cache():
    """``encoder_available`` memoises across calls; tests must not see each other."""
    transcode._encoder_cache.clear()
    yield
    transcode._encoder_cache.clear()


def _video_stream(
    width=1920, height=1080, rotation=None, r_frame_rate="30/1",
    avg_frame_rate="30/1", codec_name="hevc", pix_fmt="yuv420p",
    color_transfer=None, color_primaries=None, color_space=None,
):
    stream = {
        "width": width, "height": height,
        "r_frame_rate": r_frame_rate, "avg_frame_rate": avg_frame_rate,
        "codec_name": codec_name, "pix_fmt": pix_fmt,
        "color_transfer": color_transfer, "color_primaries": color_primaries,
        "color_space": color_space,
    }
    if rotation is not None:
        stream["side_data_list"] = [{"side_data_type": "Display Matrix", "rotation": rotation}]
    return stream


def _video_probe_json(duration="10.5", **stream_kwargs):
    return {"streams": [_video_stream(**stream_kwargs)], "format": {"duration": duration}}


def _fake_ffprobe_run(video_json, audio_json=None, calls=None):
    """A ``subprocess.run`` double that answers based on ``-select_streams``."""
    audio_json = audio_json if audio_json is not None else {"streams": []}

    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        idx = argv.index("-select_streams")
        sel = argv[idx + 1]
        payload = video_json if sel == "v:0" else audio_json
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    return run


def _which_present(*names: str):
    present = set(names)

    def which(name):
        return f"/usr/bin/{name}" if name in present else None

    return which


ENCODE = Encode(
    width=960, height=540, fps=30.0, bitrate=4_000_000,
    profile="main", pix_fmt="nv12",
    transfer="bt709", primaries="bt709", colorspace="bt709",
)

ENCODE_NO_COLOUR = Encode(
    width=960, height=540, fps=None, bitrate=4_000_000,
    profile="main10", pix_fmt="p010le",
    transfer=None, primaries=None, colorspace=None,
)


class FakePopen:
    """A ``subprocess.Popen`` double driven entirely by the constructor args."""

    def __init__(
        self, argv, *, returncode=0, lines=None, stderr_text="",
        write_bytes=None, raise_timeout_once=False,
    ):
        self.argv = argv
        self.stdout = iter(lines or [])
        self.stderr = _TextIO(stderr_text)
        self._returncode = returncode
        self.terminated = False
        self.killed = False
        self._raise_timeout_once = raise_timeout_once
        if write_bytes is not None:
            Path(argv[-1]).write_bytes(write_bytes)

    def wait(self, timeout=None):
        if self._raise_timeout_once:
            self._raise_timeout_once = False
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
        return self._returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class _TextIO:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def _make_runner(**popen_kwargs):
    def runner(argv, **kwargs):
        return FakePopen(argv, **popen_kwargs)

    return runner


# --- rotation --------------------------------------------------------------------


def test_probe_swaps_dimensions_for_quarter_turn_rotation(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run",
        _fake_ffprobe_run(_video_probe_json(width=3840, height=2160, rotation=-90)),
    )

    result = transcode.probe(src, "clip.mov")

    assert result is not None
    assert (result.width, result.height) == (2160, 3840)


def test_probe_does_not_swap_for_half_turn_rotation(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run",
        _fake_ffprobe_run(_video_probe_json(width=3840, height=2160, rotation=180)),
    )

    result = transcode.probe(src, "clip.mov")

    assert result is not None
    assert (result.width, result.height) == (3840, 2160)


def test_probe_does_not_swap_with_no_side_data(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run", _fake_ffprobe_run(_video_probe_json(width=1920, height=1080)),
    )

    result = transcode.probe(src, "clip.mov")

    assert result is not None
    assert (result.width, result.height) == (1920, 1080)


def test_probe_swaps_for_positive_quarter_turn(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run",
        _fake_ffprobe_run(_video_probe_json(width=1920, height=1080, rotation=90)),
    )

    result = transcode.probe(src, "clip.mov")

    assert (result.width, result.height) == (1080, 1920)


# --- fps -------------------------------------------------------------------------


def test_probe_fps_comes_from_nominal_rate_not_average(tmp_path, monkeypatch):
    src = tmp_path / "slomo.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run",
        _fake_ffprobe_run(_video_probe_json(
            r_frame_rate="240/1", avg_frame_rate="22286400/127693",
        )),
    )

    result = transcode.probe(src, "slomo.mov")

    assert result is not None
    assert result.fps == 240.0


def test_probe_fps_is_zero_for_zero_denominator(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run", _fake_ffprobe_run(_video_probe_json(r_frame_rate="30/0")),
    )

    result = transcode.probe(src, "clip.mov")

    assert result is not None
    assert result.fps == 0.0


# --- probe returns None, never raises --------------------------------------------


def test_probe_returns_none_for_unparseable_json(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )

    assert transcode.probe(src, "clip.mov") is None


def test_probe_returns_none_for_no_video_stream(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run", _fake_ffprobe_run({"streams": [], "format": {"duration": "5"}}),
    )

    assert transcode.probe(src, "clip.mov") is None


def test_probe_returns_none_for_missing_duration(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    payload = {"streams": [_video_stream()], "format": {}}
    monkeypatch.setattr(subprocess, "run", _fake_ffprobe_run(payload))

    assert transcode.probe(src, "clip.mov") is None


def test_probe_returns_none_when_ffprobe_exits_nonzero(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    assert transcode.probe(src, "clip.mov") is None


def test_probe_returns_none_when_ffprobe_absent(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", lambda name: None)

    assert transcode.probe(src, "clip.mov") is None


def test_probe_returns_none_when_subprocess_raises(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))

    def boom(argv, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", boom)

    assert transcode.probe(src, "clip.mov") is None


# --- has_audio ---------------------------------------------------------------------


def test_probe_has_audio_false_for_empty_audio_streams(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run",
        _fake_ffprobe_run(_video_probe_json(), audio_json={"streams": []}),
    )

    result = transcode.probe(src, "clip.mov")

    assert result is not None
    assert result.has_audio is False


def test_probe_has_audio_true_when_audio_stream_present(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 100)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(
        subprocess, "run",
        _fake_ffprobe_run(
            _video_probe_json(), audio_json={"streams": [{"codec_name": "aac"}]},
        ),
    )

    result = transcode.probe(src, "clip.mov")

    assert result is not None
    assert result.has_audio is True


def test_probe_size_comes_from_stat_not_bitrate(tmp_path, monkeypatch):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x" * 12345)
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    monkeypatch.setattr(subprocess, "run", _fake_ffprobe_run(_video_probe_json()))

    result = transcode.probe(src, "clip.mov")

    assert result is not None
    assert result.size == 12345


# --- availability ------------------------------------------------------------------


def test_ffmpeg_available_true_when_both_present(monkeypatch):
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffmpeg", "ffprobe"))
    assert transcode.ffmpeg_available() is True


def test_ffmpeg_available_false_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffprobe"))
    assert transcode.ffmpeg_available() is False


def test_ffmpeg_available_false_when_ffprobe_missing(monkeypatch):
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffmpeg"))
    assert transcode.ffmpeg_available() is False


def test_encoder_available_true_when_listed(monkeypatch):
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffmpeg"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout=" V..... hevc_videotoolbox   HEVC videotoolbox\n", stderr="",
        ),
    )

    assert transcode.encoder_available("hevc_videotoolbox") is True


def test_encoder_available_false_when_not_listed(monkeypatch):
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffmpeg"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="nothing useful\n", stderr=""),
    )

    assert transcode.encoder_available("hevc_videotoolbox") is False


def test_encoder_available_false_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(transcode.shutil, "which", lambda name: None)
    assert transcode.encoder_available("hevc_videotoolbox") is False


def test_encoder_available_is_cached(monkeypatch):
    monkeypatch.setattr(transcode.shutil, "which", _which_present("ffmpeg"))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="hevc_videotoolbox\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert transcode.encoder_available("hevc_videotoolbox") is True
    assert transcode.encoder_available("hevc_videotoolbox") is True
    assert len(calls) == 1


# --- build_argv --------------------------------------------------------------------


def test_build_argv_includes_fps_when_set():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE)
    assert "-r" in argv
    assert argv[argv.index("-r") + 1] == "30"


def test_build_argv_omits_fps_when_none():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE_NO_COLOUR)
    assert "-r" not in argv


def test_build_argv_has_hvc1_tag():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE)
    idx = argv.index("-tag:v")
    assert argv[idx + 1] == "hvc1"


def test_build_argv_vf_carries_format():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE_NO_COLOUR)
    idx = argv.index("-vf")
    assert "format=p010le" in argv[idx + 1]
    assert argv[idx + 1] == "scale=960:540,format=p010le"


def test_build_argv_omits_colour_flags_when_none():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE_NO_COLOUR)
    assert "-color_primaries" not in argv
    assert "-color_trc" not in argv
    assert "-colorspace" not in argv
    # color_range is unconditional
    assert "-color_range" in argv
    assert argv[argv.index("-color_range") + 1] == "tv"


def test_build_argv_includes_colour_flags_when_present():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE)
    assert argv[argv.index("-color_primaries") + 1] == "bt709"
    assert argv[argv.index("-color_trc") + 1] == "bt709"
    assert argv[argv.index("-colorspace") + 1] == "bt709"


def test_build_argv_maps_and_encodes_audio_when_present():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE, has_audio=True)
    assert "0:a?" in argv
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert argv[argv.index("-b:a") + 1] == "128000"


def test_build_argv_omits_audio_when_absent():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE, has_audio=False)
    assert "0:a?" not in argv
    assert "-c:a" not in argv
    assert "-b:a" not in argv


def test_build_argv_carries_metadata_flags():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE)
    assert argv[argv.index("-map_metadata") + 1] == "0"
    idx = argv.index("-movflags")
    assert argv[idx + 1] == "use_metadata_tags+faststart"


def test_build_argv_ends_with_progress_then_dest():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE)
    assert argv[-3:] == ["-progress", "pipe:1", "-nostats"] or argv[-1] == "/a/dest.mov"
    assert argv[-1] == "/a/dest.mov"
    assert "-progress" in argv
    assert argv[argv.index("-progress") + 1] == "pipe:1"
    assert "-nostats" in argv


def test_build_argv_starts_with_input():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE)
    assert argv[:6] == ["ffmpeg", "-y", "-v", "error", "-nostdin", "-i"]
    assert argv[6] == "/a/src.mov"


def test_build_argv_every_element_is_a_string():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE)
    assert all(isinstance(a, str) for a in argv)


def test_build_argv_bitrate_flags_match_encode():
    argv = transcode.build_argv(Path("/a/src.mov"), Path("/a/dest.mov"), ENCODE)
    for flag in ("-b:v", "-maxrate", "-bufsize"):
        assert argv[argv.index(flag) + 1] == "4000000"


# --- convert -------------------------------------------------------------------------


def test_convert_success_writes_dest_and_removes_part(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "out" / "dest.mov"

    runner = _make_runner(returncode=0, write_bytes=b"encoded output")
    result = transcode.convert(src, dest, ENCODE, duration=10.0, runner=runner)

    assert result.ok is True
    assert dest.exists()
    assert dest.read_bytes() == b"encoded output"
    assert result.size == len(b"encoded output")
    part = dest.with_suffix(dest.suffix + ".part")
    assert not part.exists()


def test_convert_nonzero_exit_leaves_no_dest_or_part(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "dest.mov"

    runner = _make_runner(returncode=1, stderr_text="ffmpeg exploded")
    result = transcode.convert(src, dest, ENCODE, duration=10.0, runner=runner)

    assert result.ok is False
    assert "ffmpeg exploded" in result.error
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_convert_removes_stale_part_before_starting(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "dest.mov"
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"stale garbage from a crashed run")

    runner = _make_runner(returncode=0, write_bytes=b"fresh output")
    result = transcode.convert(src, dest, ENCODE, duration=10.0, runner=runner)

    assert result.ok is True
    assert dest.read_bytes() == b"fresh output"
    assert not part.exists()


def test_convert_cancel_terminates_and_leaves_no_output(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "dest.mov"
    cancel = threading.Event()

    class CancellingLines:
        """Yields one progress line, then flips ``cancel`` before the next."""

        def __init__(self):
            self._n = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self._n == 0:
                self._n += 1
                return "out_time_us=1000000\n"
            if self._n == 1:
                self._n += 1
                cancel.set()
                return "out_time_us=2000000\n"
            raise StopIteration

    created = {}

    def runner(argv, **kw):
        proc = FakePopen(argv, returncode=0)
        proc.stdout = CancellingLines()
        created["proc"] = proc
        return proc

    result = transcode.convert(
        src, dest, ENCODE, duration=10.0, cancel=cancel, runner=runner,
    )

    assert result.ok is False
    assert result.cancelled is True
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()
    assert created["proc"].terminated is True


def test_convert_progress_callbacks_fire_with_increasing_fractions(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "dest.mov"
    fractions = []

    runner = _make_runner(
        returncode=0,
        lines=["out_time_us=1000000\n", "frame=5\n", "out_time_us=5000000\n",
               "out_time_us=9000000\n"],
        write_bytes=b"data",
    )
    result = transcode.convert(
        src, dest, ENCODE, duration=10.0, runner=runner, on_progress=fractions.append,
    )

    assert result.ok is True
    assert fractions == sorted(fractions)
    assert fractions[0] == pytest.approx(0.1)
    assert fractions[-1] == pytest.approx(0.9)


def test_convert_progress_callback_exception_does_not_kill_encode(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "dest.mov"

    def bad_callback(fraction):
        raise RuntimeError("bad callback")

    runner = _make_runner(returncode=0, lines=["out_time_us=1000000\n"], write_bytes=b"data")
    result = transcode.convert(
        src, dest, ENCODE, duration=10.0, runner=runner, on_progress=bad_callback,
    )

    assert result.ok is True
    assert dest.exists()


def test_convert_popen_oserror_is_caught(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "dest.mov"

    def runner(argv, **kw):
        raise OSError("ffmpeg not found")

    result = transcode.convert(src, dest, ENCODE, duration=10.0, runner=runner)

    assert result.ok is False
    assert "ffmpeg not found" in result.error
    assert not dest.exists()


def test_convert_timeout_terminates_and_leaves_no_output(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "dest.mov"

    runner = _make_runner(returncode=0, raise_timeout_once=True)
    result = transcode.convert(src, dest, ENCODE, duration=10.0, timeout=5.0, runner=runner)

    assert result.ok is False
    assert not result.cancelled
    assert "timed out" in result.error
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_convert_creates_dest_parent_directory(tmp_path):
    src = tmp_path / "src.mov"
    src.write_bytes(b"source")
    dest = tmp_path / "nested" / "deeper" / "dest.mov"

    runner = _make_runner(returncode=0, write_bytes=b"data")
    result = transcode.convert(src, dest, ENCODE, duration=10.0, runner=runner)

    assert result.ok is True
    assert dest.exists()


def test_convert_error_message_is_truncated():
    long_stderr = "x" * 2000
    src = Path("/tmp/does-not-matter-src.mov")
    dest_dir_result_error = None

    runner = _make_runner(returncode=1, stderr_text=long_stderr)
    # use a real tmp-like path via pytest's tmp_path is unnecessary here; just
    # check truncation logic directly through convert with a throwaway dir.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src.mov"
        src.write_bytes(b"x")
        dest = Path(d) / "dest.mov"
        result = transcode.convert(src, dest, ENCODE, duration=10.0, runner=runner)

    assert result.ok is False
    assert len(result.error) <= 500
