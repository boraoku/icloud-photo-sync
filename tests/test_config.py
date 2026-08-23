import pytest

from icloud_photo_sync.config import AppConfig, VideoOptimiseConfig


def test_unknown_override_raises(tmp_path):
    with pytest.raises(TypeError, match="unknown config override"):
        AppConfig.create("t@e.com", tmp_path / "out",
                         config_root=tmp_path / "cfg", untl_found=10)


def test_none_override_keeps_default(tmp_path):
    cfg = AppConfig.create("t@e.com", tmp_path / "out",
                           config_root=tmp_path / "cfg", until_found=None)
    assert cfg.until_found == 50


class TestVideoOptimiseConfigDbPrefix:
    """video-optimise-external must never share a job database with
    video-optimise on the same folder — different status vocabularies, and a
    row asset_id/swap semantics that mean different things in each."""

    def test_default_prefix_matches_video_optimise(self, tmp_path):
        cfg = VideoOptimiseConfig.create(tmp_path / "tree", config_root=tmp_path / "cfg")
        assert cfg.job_db.name.startswith("video-optimise-")
        assert "external" not in cfg.job_db.name

    def test_external_prefix_gives_a_distinct_filename(self, tmp_path):
        cloud = VideoOptimiseConfig.create(tmp_path / "tree", config_root=tmp_path / "cfg")
        external = VideoOptimiseConfig.create(
            tmp_path / "tree", config_root=tmp_path / "cfg",
            db_prefix="video-optimise-external",
        )
        assert cloud.job_db != external.job_db
        assert external.job_db.name.startswith("video-optimise-external-")

    def test_no_apple_id_either_way_still_keys_by_folder_alone(self, tmp_path):
        # Both commands resolve no Apple ID (--offline / always, respectively),
        # so only the prefix — not the key — is what keeps them apart.
        a = VideoOptimiseConfig.create(tmp_path / "tree", config_root=tmp_path / "cfg")
        b = VideoOptimiseConfig.create(tmp_path / "tree", config_root=tmp_path / "cfg")
        assert a.job_db == b.job_db
