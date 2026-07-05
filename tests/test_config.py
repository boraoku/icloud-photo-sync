import pytest

from icloud_photo_sync.config import AppConfig


def test_unknown_override_raises(tmp_path):
    with pytest.raises(TypeError, match="unknown config override"):
        AppConfig.create("t@e.com", tmp_path / "out",
                         config_root=tmp_path / "cfg", untl_found=10)


def test_none_override_keeps_default(tmp_path):
    cfg = AppConfig.create("t@e.com", tmp_path / "out",
                           config_root=tmp_path / "cfg", until_found=None)
    assert cfg.until_found == 50
