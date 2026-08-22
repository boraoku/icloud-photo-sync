"""Tests for the pure policy in :mod:`icloud_photo_sync.video_optimise`.

Three of the groups here exist because getting them wrong destroys footage
rather than merely wasting time: the dimension arithmetic (which decides whether
a portrait clip keeps its resolution), the frame-rate rule (which decides
whether a slow-motion clip survives), and the swap fence (which decides whether
an original can be deleted before its replacement is known to exist).

The calibration group is a regression fence of a different kind: it checks the
size model against four encodes that were actually run on real clips, so a
change to the bitrate constants shows up as a failing test rather than as a
projection that quietly stops matching reality.
"""

from __future__ import annotations

import pytest

from icloud_photo_sync import video_optimise as vo


def probe(**kw) -> vo.VideoProbe:
    """A 1080p SDR landscape clip, with fields overridable per test."""
    base = dict(
        rel="2024/05/IMG_1.MOV", size=200 * 1024 * 1024,
        width=1920, height=1080, fps=30.0, duration=60.0,
        codec="hevc", pix_fmt="yuv420p", transfer="bt709",
        primaries="bt709", colorspace="bt709",
    )
    base.update(kw)
    return vo.VideoProbe(**base)


def hdr_probe(**kw) -> vo.VideoProbe:
    """A 4K HLG clip — the shape 228 of this library's 637 large videos have."""
    return probe(**{
        "width": 3840, "height": 2160, "fps": 60.0,
        "pix_fmt": "yuv420p10le", "transfer": "arib-std-b67",
        "primaries": "bt2020", "colorspace": "bt2020nc",
        **kw,
    })


# --- Dimensions: the shorter side, never a box -------------------------------

class TestTargetDimensions:
    def test_landscape_4k_becomes_1080p(self):
        assert vo.target_dimensions(3840, 2160) == (1920, 1080)

    def test_portrait_4k_keeps_its_long_side(self):
        # The bug this rule exists to prevent: fitting 2160x3840 into a
        # 1920x1080 box gives 608x1080 and throws away two thirds of the pixels.
        assert vo.target_dimensions(2160, 3840) == (1080, 1920)

    def test_portrait_1080p_is_untouched(self):
        assert vo.target_dimensions(1080, 1920) == (1080, 1920)

    def test_never_upscales(self):
        assert vo.target_dimensions(640, 480) == (640, 480)
        assert vo.target_dimensions(480, 640) == (480, 640)

    def test_square_4k(self):
        assert vo.target_dimensions(2160, 2160) == (1080, 1080)

    @pytest.mark.parametrize("w,h", [(3840, 2160), (2160, 3840), (1440, 2560),
                                     (2560, 1440), (1234, 5678), (2161, 3841)])
    def test_results_are_always_even(self, w, h):
        ow, oh = vo.target_dimensions(w, h)
        assert ow % 2 == 0 and oh % 2 == 0

    def test_aspect_ratio_is_preserved_within_a_pixel(self):
        ow, oh = vo.target_dimensions(2160, 3840)
        assert abs(ow / oh - 2160 / 3840) < 0.01

    def test_custom_short_side(self):
        assert vo.target_dimensions(3840, 2160, short_side=720) == (1280, 720)

    def test_rejects_nonsense(self):
        with pytest.raises(ValueError):
            vo.target_dimensions(0, 1080)


# --- Frame rate: never touched above 60 fps ----------------------------------

class TestSlowMotion:
    def test_240fps_is_slow_motion(self):
        assert probe(fps=240.0).is_slow_motion

    def test_60fps_is_not(self):
        assert not probe(fps=60.0).is_slow_motion

    def test_a_rounding_artefact_above_60_is_not(self):
        assert not probe(fps=60.4).is_slow_motion

    def test_slow_motion_gets_no_frame_rate(self):
        # fps=None is how transcode knows to omit -r entirely.
        assert vo.choose_encode(probe(fps=240.0, width=2160, height=3840)).fps is None

    def test_normal_footage_is_capped_at_30(self):
        assert vo.choose_encode(hdr_probe()).fps == 30.0

    def test_a_clip_already_under_the_cap_is_left_alone(self):
        # No -r at all, rather than -r 24: there is nothing to change, and
        # resampling a clip to its own rate only risks duplicating frames.
        assert vo.choose_encode(probe(fps=24.0)).fps is None

    def test_a_29_97_clip_is_not_resampled_to_a_rounded_30(self):
        assert vo.choose_encode(probe(fps=30000 / 1001)).fps is None

    def test_slow_motion_at_target_resolution_is_skipped_entirely(self):
        # Nothing to downscale, so there is nothing worth putting nine thousand
        # frames of unusual footage through an encoder for.
        result = vo.classify(probe(fps=240.0, width=1080, height=1920,
                                   size=300 * 1024 * 1024))
        assert isinstance(result, vo.Skip)
        assert result.reason == vo.SKIP_SLOMO_AT_TARGET

    def test_slow_motion_above_target_is_downscaled(self):
        result = vo.classify(probe(fps=120.0, width=2160, height=3840,
                                   size=200 * 1024 * 1024, duration=40.0))
        assert isinstance(result, vo.Candidate)
        assert (result.encode.width, result.encode.height) == (1080, 1920)
        assert result.encode.fps is None

    def test_a_720p_slow_motion_clip_is_skipped(self):
        result = vo.classify(probe(fps=240.0, width=720, height=1280,
                                   size=140 * 1024 * 1024))
        assert isinstance(result, vo.Skip)
        assert result.reason == vo.SKIP_SLOMO_AT_TARGET


# --- Colour ------------------------------------------------------------------

class TestColour:
    def test_hlg_is_hdr(self):
        assert hdr_probe().is_hdr

    def test_pq_is_hdr(self):
        assert probe(transfer="smpte2084").is_hdr

    def test_bt709_is_not(self):
        assert not probe().is_hdr

    def test_hdr_source_encodes_10bit(self):
        enc = vo.choose_encode(hdr_probe())
        assert enc.profile == vo.PROFILE_10BIT and enc.pix_fmt == vo.PIX_FMT_10BIT

    def test_sdr_source_stays_8bit(self):
        # Promoting an 8-bit source to 10-bit grew a real 348 MiB clip to 419 MiB.
        enc = vo.choose_encode(probe())
        assert enc.profile == vo.PROFILE_8BIT and enc.pix_fmt == vo.PIX_FMT_8BIT

    def test_a_10bit_sdr_source_still_encodes_10bit(self):
        enc = vo.choose_encode(probe(pix_fmt="yuv420p10le"))
        assert enc.pix_fmt == vo.PIX_FMT_10BIT

    def test_the_colour_triplet_is_copied_not_chosen(self):
        enc = vo.choose_encode(hdr_probe())
        assert (enc.transfer, enc.primaries, enc.colorspace) == (
            "arib-std-b67", "bt2020", "bt2020nc")

    def test_identical_colour_matches(self):
        assert vo.colour_matches(hdr_probe(), hdr_probe())

    def test_hdr_in_sdr_out_does_not_match(self):
        out = hdr_probe(transfer="bt709", primaries="bt709",
                        colorspace="bt709", pix_fmt="yuv420p")
        assert not vo.colour_matches(hdr_probe(), out)

    def test_losing_bit_depth_does_not_match(self):
        assert not vo.colour_matches(hdr_probe(), hdr_probe(pix_fmt="yuv420p"))

    def test_gaining_bit_depth_does_not_match(self):
        assert not vo.colour_matches(probe(), probe(pix_fmt="yuv420p10le"))

    def test_unknown_normalises_to_untagged(self):
        assert vo.colour_matches(probe(transfer=None, primaries=None, colorspace=None),
                                 probe(transfer="unknown", primaries=None, colorspace=None))

    def test_untagged_may_come_back_bt709(self):
        # ffmpeg naming what was already implicit is not a loss.
        src = probe(transfer=None, primaries=None, colorspace=None)
        out = probe(transfer="bt709", primaries="bt709", colorspace="bt709")
        assert vo.colour_matches(src, out)

    def test_untagged_may_not_come_back_hdr(self):
        src = probe(transfer=None, primaries=None, colorspace=None)
        assert not vo.colour_matches(src, hdr_probe())

    def test_case_is_ignored(self):
        assert vo.colour_matches(hdr_probe(), hdr_probe(transfer="ARIB-STD-B67"))


# --- The post-encode gate ----------------------------------------------------

class TestAcceptOutput:
    def test_a_smaller_output_with_the_same_colour_is_kept(self):
        src = hdr_probe(size=300 * 1024 * 1024)
        assert vo.accept_output(src, hdr_probe(size=30 * 1024 * 1024)) is None

    def test_a_colour_mismatch_is_refused(self):
        src = hdr_probe(size=300 * 1024 * 1024)
        out = hdr_probe(size=30 * 1024 * 1024, transfer="bt709",
                        primaries="bt709", colorspace="bt709", pix_fmt="yuv420p")
        skip = vo.accept_output(src, out)
        assert skip is not None and skip.reason == vo.SKIP_COLOUR_MISMATCH
        assert "HLG HDR" in skip.detail

    def test_colour_is_checked_before_size(self):
        # A washed-out file that is also huge should say so about the colour:
        # that is the fault worth reporting.
        src = hdr_probe(size=300 * 1024 * 1024)
        out = hdr_probe(size=400 * 1024 * 1024, transfer="bt709",
                        primaries="bt709", colorspace="bt709", pix_fmt="yuv420p")
        assert vo.accept_output(src, out).reason == vo.SKIP_COLOUR_MISMATCH

    def test_an_output_that_barely_shrank_is_refused(self):
        src = probe(size=100 * 1024 * 1024)
        skip = vo.accept_output(src, probe(size=80 * 1024 * 1024))
        assert skip is not None and skip.reason == vo.SKIP_NOT_SMALLER

    def test_an_output_that_grew_is_refused(self):
        src = probe(size=100 * 1024 * 1024)
        assert vo.accept_output(src, probe(size=120 * 1024 * 1024)) is not None

    def test_exactly_at_the_ratio_is_refused(self):
        src = probe(size=100 * 1024 * 1024)
        assert vo.accept_output(src, probe(size=75 * 1024 * 1024)) is not None


# --- The swap fence ----------------------------------------------------------

class TestSwapFence:
    def test_a_verified_swap_is_constructible(self):
        swap = vo.Swap(rel="a.MOV", old_asset_id="OLD", new_asset_id="NEW",
                       old_size=100, new_size=10)
        assert swap.freed == 90

    def test_no_new_asset_id_raises(self):
        # The ordering rule made structural: "delete this original" is not an
        # expressible instruction until the replacement is known to exist.
        with pytest.raises(ValueError, match="verified new asset id"):
            vo.Swap(rel="a.MOV", old_asset_id="OLD", new_asset_id="",
                    old_size=100, new_size=10)

    def test_no_old_asset_id_raises(self):
        with pytest.raises(ValueError):
            vo.Swap(rel="a.MOV", old_asset_id="", new_asset_id="NEW",
                    old_size=100, new_size=10)

    def test_deleting_what_was_just_uploaded_raises(self):
        with pytest.raises(ValueError, match="cannot be the original"):
            vo.Swap(rel="a.MOV", old_asset_id="SAME", new_asset_id="SAME",
                    old_size=100, new_size=10)

    def test_freed_never_goes_negative(self):
        swap = vo.Swap(rel="a.MOV", old_asset_id="OLD", new_asset_id="NEW",
                       old_size=10, new_size=100)
        assert swap.freed == 0


# --- Classification ----------------------------------------------------------

class TestClassify:
    def test_a_big_wasteful_4k_clip_is_taken(self):
        result = vo.classify(hdr_probe(size=350 * 1024 * 1024, duration=36.0))
        assert isinstance(result, vo.Candidate)
        assert result.predicted_saving > 300 * 1024 * 1024

    def test_a_small_file_is_below_the_floor(self):
        result = vo.classify(probe(size=5 * 1024 * 1024))
        assert isinstance(result, vo.Skip) and result.reason == vo.SKIP_TOO_SMALL

    def test_an_already_efficient_clip_is_left_alone(self):
        # 1080p30 at 3 Mbps has nothing left to give.
        result = vo.classify(probe(size=22 * 1024 * 1024, duration=60.0))
        assert isinstance(result, vo.Skip)
        assert result.reason == vo.SKIP_NOTHING_TO_GAIN

    def test_an_unprobeable_file_is_a_skip_not_a_crash(self):
        result = vo.classify(None, rel="2024/05/broken.MOV")
        assert isinstance(result, vo.Skip)
        assert result.reason == vo.SKIP_UNPROBEABLE and result.rel.endswith("broken.MOV")

    def test_a_zero_duration_file_is_a_skip(self):
        result = vo.classify(probe(duration=0.0))
        assert isinstance(result, vo.Skip) and result.reason == vo.SKIP_UNPROBEABLE

    def test_the_live_photo_guard(self):
        result = vo.classify(probe(duration=3.0, size=30 * 1024 * 1024),
                             has_image_sibling=True)
        assert isinstance(result, vo.Skip) and result.reason == vo.SKIP_LIVE_PHOTO

    def test_a_long_video_beside_a_still_is_not_a_live_photo(self):
        result = vo.classify(hdr_probe(duration=40.0, size=300 * 1024 * 1024),
                             has_image_sibling=True)
        assert isinstance(result, vo.Candidate)

    def test_skip_hdr_excludes_hdr_only(self):
        assert isinstance(vo.classify(hdr_probe(size=300 * 1024 * 1024,
                                                duration=36.0), skip_hdr=True), vo.Skip)
        assert isinstance(vo.classify(probe(size=300 * 1024 * 1024, duration=60.0),
                                      skip_hdr=True), vo.Candidate)

    def test_hdr_only_excludes_sdr(self):
        assert isinstance(vo.classify(probe(size=300 * 1024 * 1024, duration=60.0),
                                      hdr_only=True), vo.Skip)

    def test_the_floor_is_configurable(self):
        result = vo.classify(hdr_probe(size=5 * 1024 * 1024, duration=1.0),
                             min_bytes=1024)
        assert isinstance(result, vo.Candidate)


# --- The plan ----------------------------------------------------------------

class TestBuildPlan:
    def test_candidates_come_back_biggest_win_first(self):
        small = hdr_probe(rel="b.MOV", size=60 * 1024 * 1024, duration=10.0)
        large = hdr_probe(rel="a.MOV", size=500 * 1024 * 1024, duration=40.0)
        plan = vo.build_plan([small, large])
        assert [c.rel for c in plan.candidates] == ["a.MOV", "b.MOV"]

    def test_skips_are_kept_and_reported(self):
        plan = vo.build_plan([probe(rel="tiny.MOV", size=1024)])
        assert plan.candidates == () and len(plan.skipped) == 1

    def test_totals_add_up(self):
        plan = vo.build_plan([hdr_probe(size=300 * 1024 * 1024, duration=36.0)])
        assert plan.predicted_saving == plan.source_bytes - plan.predicted_bytes
        assert plan.duration == 36.0

    def test_an_unprobeable_entry_uses_the_supplied_name(self):
        plan = vo.build_plan([None], rels=["2024/05/x.MOV"])
        assert plan.skipped[0].rel == "2024/05/x.MOV"

    def test_image_stems_drive_the_live_photo_guard(self):
        p = probe(rel="2024/05/IMG_9.MOV", duration=2.0, size=30 * 1024 * 1024)
        plan = vo.build_plan([p], image_stems=frozenset({"2024/05/img_9"}))
        assert plan.skipped[0].reason == vo.SKIP_LIVE_PHOTO

    def test_stem_key_folds_case_and_drops_the_suffix(self):
        assert vo.stem_key("2024/05/IMG_1.MOV") == "2024/05/img_1"
        assert vo.stem_key("IMG_1.MOV") == "img_1"
        assert vo.stem_key("2024/05/no-suffix") == "2024/05/no-suffix"


class TestRefusals:
    def _plan(self, n: int) -> vo.OptimisePlan:
        return vo.build_plan([hdr_probe(rel=f"{i}.MOV", size=300 * 1024 * 1024,
                                        duration=36.0) for i in range(n)])

    def test_no_refusal_when_everything_is_fine(self):
        assert self._plan(3).refusal(free_bytes=50 * 1024 ** 3, max_convert=10) is None

    def test_a_full_disk_refuses_and_says_how_much_is_needed(self):
        message = self._plan(3).refusal(free_bytes=1024 ** 3)
        assert message is not None and "free" in message.lower()

    def test_too_many_videos_refuses_and_names_the_flag(self):
        message = self._plan(5).refusal(max_convert=2)
        assert message is not None and "--limit" in message

    def test_the_cap_is_inclusive(self):
        assert self._plan(2).refusal(max_convert=2) is None


# --- Calibration: the model against encodes that were actually run -----------

class TestCalibration:
    """Four real encodes, measured. The model must land within 10 % of each.

    If a future ffmpeg changes what ``-b:v`` delivers, these fail — which is the
    point. A projection that silently stops matching reality is worse than one
    that breaks loudly.
    """

    CASES = [
        # (label, w, h, fps, hdr, ten_bit, duration, source bytes, measured output bytes)
        ("4K HLG landscape", 3840, 2160, 60.0, True, True,
         101.908333, 612_395_265, 74_358_872),
        ("4K HLG portrait", 2160, 3840, 60.0, True, True,
         35.631700, 372_619_090, 26_078_711),
        ("1080p SDR HEVC", 1920, 1080, 30.0, False, False,
         210.525000, 365_044_794, 114_121_699),
        ("1080p SDR H.264 portrait", 1080, 1920, 30.0, False, False,
         222.061678, 428_806_476, 120_209_244),
    ]

    @pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
    def test_predicted_size_matches_what_was_measured(self, case):
        _, w, h, fps, hdr, ten_bit, duration, src_bytes, measured = case
        p = probe(
            width=w, height=h, fps=fps, duration=duration, size=src_bytes,
            pix_fmt="yuv420p10le" if ten_bit else "yuv420p",
            transfer="arib-std-b67" if hdr else "bt709",
            primaries="bt2020" if hdr else "bt709",
            colorspace="bt2020nc" if hdr else "bt709",
        )
        predicted = vo.predicted_size(p, vo.choose_encode(p))
        assert abs(predicted - measured) / measured < 0.10, (
            f"predicted {predicted:,} vs measured {measured:,}")

    @pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
    def test_the_bitrate_target_is_what_was_actually_requested(self, case):
        _, w, h, fps, hdr, ten_bit, duration, src_bytes, _ = case
        p = probe(width=w, height=h, fps=fps, duration=duration, size=src_bytes,
                  pix_fmt="yuv420p10le" if ten_bit else "yuv420p",
                  transfer="arib-std-b67" if hdr else "bt709")
        # Every one of the four outputs 1920x1080 worth of pixels at 30 fps, so
        # the model must ask for exactly the base rate that was passed to ffmpeg.
        assert vo.choose_encode(p).bitrate == (vo.BITRATE_HDR if hdr else vo.BITRATE_SDR)

    def test_the_measured_efficiency_constant(self):
        # Documented as measured; a change here must be deliberate.
        assert vo.VIDEOTOOLBOX_EFFICIENCY == pytest.approx(0.73)

    def test_bitrate_scales_with_pixel_count(self):
        full = vo.target_bitrate(1920 * 1080, 30.0, hdr=True)
        half = vo.target_bitrate(1920 * 1080 // 2, 30.0, hdr=True)
        assert half == pytest.approx(full / 2, rel=0.01)

    def test_bitrate_scales_sublinearly_with_frame_rate(self):
        at30 = vo.target_bitrate(1920 * 1080, 30.0, hdr=True)
        at120 = vo.target_bitrate(1920 * 1080, 120.0, hdr=True)
        assert at30 < at120 < at30 * 4

    def test_hdr_gets_more_bits_than_sdr(self):
        assert (vo.target_bitrate(1920 * 1080, 30.0, hdr=True)
                > vo.target_bitrate(1920 * 1080, 30.0, hdr=False))
