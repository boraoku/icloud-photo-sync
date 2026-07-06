"""Scanner + size-parsing tests for local-clean."""

import pytest

from icloud_photo_sync.local_clean import _parse_size, scan_images


def _write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_scan_filters_by_suffix_and_size(tmp_path):
    _write(tmp_path / "2023/07/a.JPG", 500)         # included (uppercase)
    _write(tmp_path / "2023/07/b.png", 500)         # included
    _write(tmp_path / "2023/07/c.jpeg", 500)        # included
    _write(tmp_path / "2023/07/big.jpg", 2_000_000) # excluded: too big
    _write(tmp_path / "2023/07/movie.mov", 500)     # excluded: not image
    _write(tmp_path / "2023/07/pic.heic", 500)      # excluded: heic
    _write(tmp_path / "2023/07/part.jpg.part", 500) # excluded: .part suffix
    _write(tmp_path / "2023/07/empty.jpg", 0)       # excluded: zero bytes

    found = scan_images(tmp_path, max_bytes=1_000_000)
    rels = [f.rel for f in found]
    assert rels == ["2023/07/a.JPG", "2023/07/b.png", "2023/07/c.jpeg"]


def test_scan_prunes_hidden_dirs(tmp_path):
    _write(tmp_path / "2023/visible.jpg", 100)
    _write(tmp_path / ".hidden/secret.jpg", 100)
    found = scan_images(tmp_path, max_bytes=1_000_000)
    assert [f.rel for f in found] == ["2023/visible.jpg"]


def test_scan_records_size_and_mtime(tmp_path):
    p = tmp_path / "2023/x.jpg"
    _write(p, 321)
    found = scan_images(tmp_path, max_bytes=1_000_000)
    assert len(found) == 1
    img = found[0]
    assert img.size == 321
    assert img.mtime_ns == p.stat().st_mtime_ns
    assert img.path == p


def test_scan_respects_custom_max_bytes(tmp_path):
    _write(tmp_path / "small.jpg", 400)
    _write(tmp_path / "medium.jpg", 800)
    found = scan_images(tmp_path, max_bytes=500)
    assert [f.rel for f in found] == ["small.jpg"]


def test_scan_deterministic_order(tmp_path):
    for name in ["z.jpg", "a.jpg", "m.jpg"]:
        _write(tmp_path / "2023" / name, 100)
    found = scan_images(tmp_path, max_bytes=1_000_000)
    assert [f.rel for f in found] == ["2023/a.jpg", "2023/m.jpg", "2023/z.jpg"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1MB", 1_000_000),
        ("500KB", 500_000),
        ("2mb", 2_000_000),
        ("1048576", 1_048_576),
        ("1MiB", 1_048_576),
        ("1.5MB", 1_500_000),
        (" 1 MB ", 1_000_000),
    ],
)
def test_parse_size_valid(text, expected):
    assert _parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1XB", "MB", "-5"])
def test_parse_size_invalid(text):
    from typer import BadParameter

    with pytest.raises(BadParameter):
        _parse_size(text)
