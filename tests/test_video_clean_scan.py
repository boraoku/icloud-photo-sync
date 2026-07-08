"""Scanner tests for video-clean."""

from icloud_photo_sync.video_clean import scan_videos


def _write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_scan_filters_by_suffix(tmp_path):
    _write(tmp_path / "2023/07/a.MOV", 500)          # included (uppercase)
    _write(tmp_path / "2023/07/b.mp4", 500)          # included
    _write(tmp_path / "2023/07/c.mkv", 500)          # included
    _write(tmp_path / "2023/07/pic.jpg", 500)        # excluded: not video
    _write(tmp_path / "2023/07/pic.heic", 500)       # excluded: not video
    _write(tmp_path / "2023/07/clip.mp4.part", 500)  # excluded: .part suffix
    _write(tmp_path / "2023/07/empty.mov", 0)        # excluded: zero bytes

    found = scan_videos(tmp_path)
    rels = sorted(f.rel for f in found)
    assert rels == ["2023/07/a.MOV", "2023/07/b.mp4", "2023/07/c.mkv"]


def test_scan_sorted_largest_first(tmp_path):
    _write(tmp_path / "small.mp4", 100)
    _write(tmp_path / "big.mp4", 900)
    _write(tmp_path / "medium.mp4", 500)
    found = scan_videos(tmp_path)
    assert [f.rel for f in found] == ["big.mp4", "medium.mp4", "small.mp4"]


def test_scan_size_tiebreak_is_rel(tmp_path):
    for name in ["z.mov", "a.mov", "m.mov"]:
        _write(tmp_path / "2023" / name, 100)  # identical size
    found = scan_videos(tmp_path)
    assert [f.rel for f in found] == ["2023/a.mov", "2023/m.mov", "2023/z.mov"]


def test_scan_prunes_hidden_and_dot_files(tmp_path):
    _write(tmp_path / "2023/visible.mov", 100)
    _write(tmp_path / ".hidden/secret.mov", 100)      # hidden dir
    _write(tmp_path / "2023/._visible.mov", 100)       # AppleDouble sidecar
    _write(tmp_path / "2023/.thumb.mov", 100)          # dot-file
    found = scan_videos(tmp_path)
    assert [f.rel for f in found] == ["2023/visible.mov"]


def test_scan_respects_min_bytes(tmp_path):
    _write(tmp_path / "small.mp4", 400)
    _write(tmp_path / "big.mp4", 800)
    found = scan_videos(tmp_path, min_bytes=500)
    assert [f.rel for f in found] == ["big.mp4"]


def test_scan_records_size_and_mtime(tmp_path):
    p = tmp_path / "2023/x.mov"
    _write(p, 321)
    found = scan_videos(tmp_path)
    assert len(found) == 1
    v = found[0]
    assert v.size == 321
    assert v.mtime_ns == p.stat().st_mtime_ns
    assert v.path == p


def test_scan_skips_symlinks(tmp_path):
    real = tmp_path / "real.mov"
    _write(real, 100)
    link = tmp_path / "link.mov"
    link.symlink_to(real)
    found = scan_videos(tmp_path)
    assert [f.rel for f in found] == ["real.mov"]
