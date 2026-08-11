"""Reconstructing a past clean session, and refusing to when it cannot be trusted.

The in-session path measures each file microseconds before it is trashed. Here
the file is already gone, so the evidence is circumstantial by construction —
these tests exist to pin exactly how much weaker it is allowed to be, and to
prove the tautology (comparing the manifest's size against itself) cannot be
expressed at all.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from icloud_photo_sync import retro_clean
from icloud_photo_sync.icloud_delete import (
    EVIDENCE_MEASURED,
    EVIDENCE_RETROSPECTIVE,
    SKIP_ELSEWHERE,
    SKIP_NOT_CLASSIFIED,
    SKIP_NOT_REVIEWED,
    SKIP_ON_DISK,
    SKIP_OUT_OF_ENVELOPE,
    SKIP_SIZE,
    SKIP_STILL_CLASSIFIED,
    SKIP_UNVERIFIED_ROW,
    RetroEvidence,
    Skip,
    build_plan,
)
from icloud_photo_sync.models import AssetRef
from icloud_photo_sync.state import StateStore

PASS_AT = "2026-07-27T08:20:10.339666+00:00"
CAPTURE = datetime(2024, 3, 11, 9, 14, tzinfo=timezone.utc)


# --- helpers ------------------------------------------------------------------


def _asset(id="a1", filename="IMG_1.JPG", size=100):
    return AssetRef(id=id, filename=filename, capture_dt=CAPTURE,
                    added_dt=CAPTURE, size=size)


def tracked(store, rel, *, id="a1", filename="IMG_1.JPG", size=100):
    """A completed row whose file is *not* on disk, verified by the last pass."""
    store.register(_asset(id=id, filename=filename, size=size), rel)
    store.mark_completed(id, size)
    store.flush()
    # Back-date to before the pass: the tool stamps "now", and every rung here
    # turns on the row predating the moment the tree was last whole.
    store._conn.execute(
        "UPDATE assets SET updated_at = ? WHERE id = ?",
        ("2026-07-25T00:00:00+00:00", id))
    store._conn.commit()
    return rel


def evidence(*, envelopes=None, vetoes=None, reviewed=None, verified=PASS_AT):
    return RetroEvidence(
        verified_present_at=verified,
        envelopes=envelopes if envelopes is not None else {},
        vetoes=vetoes or {},
        corroboration={},
        reviewed=reviewed,
    )


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.db")
    s.set_meta("last_full_pass_at", PASS_AT)
    yield s
    s.close()


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "photos"
    out.mkdir()
    return out


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


def write_log(logs, name, lines):
    (logs / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def trash_line(stamp="2026-08-10 11:04:33"):
    return f'{stamp} DEBUG   icloud_photo_sync.review: review: "POST /trash HTTP/1.1" 200 -'


def other_line(stamp="2026-07-05 23:15:24"):
    return f"{stamp} DEBUG   keyring.backend: Loading Gnome"


def make_cache(path, rels):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE classifications (
        path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
        model TEXT NOT NULL, category TEXT NOT NULL, confidence REAL,
        reason TEXT, classified_at TEXT)""")
    conn.executemany(
        "INSERT INTO classifications VALUES (?,1,1,'m','photo',1.0,'','')",
        [(r,) for r in rels])
    conn.commit()
    conn.close()
    return path


# --- the evidence class is structural, not advisory ---------------------------


def test_retrospective_evidence_refuses_a_caller_supplied_size(store, root):
    """The only size available would be the manifest's own — checking it against
    itself always passes, so the combination must not be expressible."""
    rel = tracked(store, "2024/03/IMG_1.JPG")
    with pytest.raises(ValueError, match="cannot take sizes"):
        build_plan([rel], state=store, output_root=root, sizes={rel: 100},
                   evidence=EVIDENCE_RETROSPECTIVE,
                   retro=evidence(envelopes={rel: "local-clean"}))


def test_retrospective_evidence_requires_the_evidence_object(store, root):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    with pytest.raises(ValueError, match="needs a RetroEvidence"):
        build_plan([rel], state=store, output_root=root,
                   evidence=EVIDENCE_RETROSPECTIVE)


def test_measured_evidence_still_requires_sizes(store, root):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    with pytest.raises(ValueError, match="needs the sizes"):
        build_plan([rel], state=store, output_root=root)


def test_measured_evidence_rejects_a_retro_object(store, root):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    with pytest.raises(ValueError, match="measured plan"):
        build_plan([rel], state=store, output_root=root, sizes={rel: 100},
                   retro=evidence(envelopes={rel: "local-clean"}))


def test_an_unknown_evidence_class_is_refused(store, root):
    with pytest.raises(ValueError, match="unknown evidence"):
        build_plan([], state=store, output_root=root, evidence="probably-fine")


def test_a_retrospective_candidate_carries_the_measured_bytes_not_the_manifest_size(
        store, root):
    """local_size must be bytes_done — what this machine wrote — never a copy of
    expected_size, which is the value being checked against."""
    rel = tracked(store, "2024/03/IMG_1.JPG", size=100)
    plan = build_plan([rel], state=store, output_root=root,
                      evidence=EVIDENCE_RETROSPECTIVE,
                      retro=evidence(envelopes={rel: "local-clean"}))
    [candidate] = plan.candidates
    assert candidate.evidence == EVIDENCE_RETROSPECTIVE
    assert candidate.local_size == store.get("a1")["bytes_done"]


def test_the_measured_ladder_is_untouched(store, root):
    """Regression fence: the strong path must still reject a size mismatch."""
    rel = tracked(store, "2024/03/IMG_1.JPG", size=100)
    plan = build_plan([rel], state=store, output_root=root, sizes={rel: 101})
    assert [s.reason for s in plan.skipped] == [SKIP_SIZE]
    assert not plan.candidates


# --- the retrospective ladder --------------------------------------------------


def test_a_row_the_last_pass_never_vouched_for_is_refused(store, root):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    store._conn.execute("UPDATE assets SET updated_at = ? WHERE id = 'a1'",
                        ("2026-08-01T00:00:00+00:00",))     # after the pass
    store._conn.commit()
    plan = build_plan([rel], state=store, output_root=root,
                      evidence=EVIDENCE_RETROSPECTIVE,
                      retro=evidence(envelopes={rel: "local-clean"}))
    assert [s.reason for s in plan.skipped] == [SKIP_UNVERIFIED_ROW]


def test_a_file_outside_every_envelope_is_skipped(store, root):
    rel = tracked(store, "2024/03/IMG_1.HEIC", filename="IMG_1.HEIC")
    plan = build_plan([rel], state=store, output_root=root,
                      evidence=EVIDENCE_RETROSPECTIVE, retro=evidence(envelopes={}))
    assert [s.reason for s in plan.skipped] == [SKIP_OUT_OF_ENVELOPE]


def test_a_veto_wins_over_the_envelope(store, root):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    plan = build_plan(
        [rel], state=store, output_root=root, evidence=EVIDENCE_RETROSPECTIVE,
        retro=evidence(envelopes={rel: "local-clean"},
                       vetoes={rel: Skip(rel, SKIP_ELSEWHERE, "/Volumes/T7/x.JPG")}))
    [skip] = plan.skipped
    assert skip.reason == SKIP_ELSEWHERE
    assert skip.detail == "/Volumes/T7/x.JPG"


def test_unreviewed_files_are_skipped_once_a_review_has_run(store, root):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    kwargs = dict(state=store, output_root=root, evidence=EVIDENCE_RETROSPECTIVE)

    # reviewed=None means the review did not run and says nothing.
    assert build_plan([rel], retro=evidence(envelopes={rel: "local-clean"}),
                      **kwargs).candidates
    # An empty selection is a real answer: the user ticked nothing.
    plan = build_plan([rel], retro=evidence(envelopes={rel: "local-clean"},
                                            reviewed=frozenset()), **kwargs)
    assert [s.reason for s in plan.skipped] == [SKIP_NOT_REVIEWED]


def test_a_file_back_on_disk_is_refused_before_any_retro_reasoning(store, root):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    (root / "2024/03").mkdir(parents=True)
    (root / rel).write_bytes(b"restored")
    plan = build_plan([rel], state=store, output_root=root,
                      evidence=EVIDENCE_RETROSPECTIVE,
                      retro=evidence(envelopes={rel: "local-clean"}))
    assert [s.reason for s in plan.skipped] == [SKIP_ON_DISK]


# --- the envelope ---------------------------------------------------------------


@pytest.mark.parametrize("rel,size,expected", [
    ("2024/03/a.JPG", 500_000, "local-clean"),
    ("2024/03/a.jpeg", 1, "local-clean"),
    ("2024/03/a.PNG", 1_048_576, "local-clean"),
    ("2024/03/a.JPG", 1_048_577, None),          # over --max-size
    ("2024/03/a.HEIC", 500, None),               # never scanned by either
    ("2024/03/a.DNG", 500, None),
    ("2024/03/a.MOV", 10_000_000, "video-clean"),
    ("2024/03/a.mp4", 10_000_000, "video-clean"),
])
def test_envelope_membership(rel, size, expected):
    assert retro_clean.envelope_for(
        rel, size, max_bytes=1_048_576, min_bytes=0) == expected


def test_a_video_below_min_size_is_outside_the_envelope():
    assert retro_clean.envelope_for(
        "2024/03/a.MOV", 5, max_bytes=1_048_576, min_bytes=1_000) is None


# --- verified_present_at --------------------------------------------------------


def test_verified_present_at_needs_a_completed_pass(tmp_path):
    with StateStore(tmp_path / "s.db") as store:
        when, why = retro_clean.verified_present_at(store)
        assert when is None and "never recorded a completed full pass" in why


def test_verified_present_at_needs_a_whole_tree(store):
    store.register(_asset(id="a2"), "2024/03/b.JPG")     # left pending
    store.flush()
    when, why = retro_clean.verified_present_at(store)
    assert when is None and "pending or failed" in why


def test_verified_present_at_is_the_pass_timestamp(store):
    tracked(store, "2024/03/IMG_1.JPG")
    assert retro_clean.verified_present_at(store) == (PASS_AT, "")


# --- the log --------------------------------------------------------------------


def test_trash_rounds_are_parsed_across_rotated_files(logs):
    write_log(logs, "icloud-photo-sync.log.2", [other_line("2026-07-05 23:15:24")])
    write_log(logs, "icloud-photo-sync.log.1", [trash_line("2026-07-06 10:00:00")])
    write_log(logs, "icloud-photo-sync.log", [trash_line("2026-08-10 11:04:33")])

    since = datetime.fromisoformat(PASS_AT)
    log = retro_clean.trash_events(logs, since)
    assert [r.strftime("%Y-%m-%d %H:%M:%S") for r in log.rounds] \
        == ["2026-08-10 11:04:33"]          # the July round predates the window
    assert log.covers(since)                # but it still sets the log's reach


def test_log_timestamps_are_read_as_local_time_not_utc(logs):
    """asctime writes local time; the manifest is UTC. Comparing them naively
    would silently shift every round by the machine's offset."""
    write_log(logs, "icloud-photo-sync.log", [trash_line("2026-08-10 11:04:33")])
    [round_at] = retro_clean.trash_events(
        logs, datetime.fromisoformat(PASS_AT)).rounds
    assert round_at.tzinfo is not None
    assert round_at.utcoffset() == datetime(2026, 8, 10, 11, 4, 33).astimezone().utcoffset()


def test_a_log_that_starts_after_the_window_does_not_cover_it(logs):
    write_log(logs, "icloud-photo-sync.log", [trash_line("2026-08-10 11:04:33")])
    assert not retro_clean.trash_events(
        logs, datetime.fromisoformat(PASS_AT)).covers(datetime.fromisoformat(PASS_AT))


def test_a_missing_log_dir_yields_no_files_rather_than_raising(tmp_path):
    log = retro_clean.trash_events(tmp_path / "nope", datetime.now(timezone.utc))
    assert log.files == () and log.rounds == [] and log.oldest_entry is None


# --- the classification cache ----------------------------------------------------


def test_a_surviving_cache_row_disqualifies_a_file(tmp_path):
    cache = make_cache(tmp_path / "c.db", ["2024/01/a.JPG", "2024/05/z.JPG"])
    vetoes = retro_clean.cache_vetoes(cache, ["2024/01/a.JPG"])
    assert vetoes["2024/01/a.JPG"].reason == SKIP_STILL_CLASSIFIED


def test_a_file_the_classifier_never_reached_is_disqualified(tmp_path):
    cache = make_cache(tmp_path / "c.db", ["2024/01/a.JPG", "2024/05/z.JPG"])
    vetoes = retro_clean.cache_vetoes(cache, ["2026/09/later.JPG"])
    assert vetoes["2026/09/later.JPG"].reason == SKIP_NOT_CLASSIFIED


def test_a_hole_inside_the_walked_range_is_not_vetoed(tmp_path):
    """The true positives are exactly the holes: local_clean purges the row of
    everything it trashes."""
    cache = make_cache(tmp_path / "c.db", ["2024/01/a.JPG", "2024/05/z.JPG"])
    assert retro_clean.cache_vetoes(cache, ["2024/03/middle.JPG"]) == {}


def test_an_absent_cache_contributes_nothing_and_is_never_created(tmp_path):
    missing = tmp_path / "no-such-cache.db"
    assert retro_clean.cache_vetoes(missing, ["2024/03/a.JPG"]) == {}
    assert not missing.exists()      # an auto-made empty cache would veto everything


# --- corroboration roots ----------------------------------------------------------


def test_a_second_copy_at_the_same_size_disqualifies_a_file(store, tmp_path):
    tracked(store, "2024/03/IMG_1.JPG", size=100)
    other = tmp_path / "backup"
    (other / "2024/03").mkdir(parents=True)
    (other / "2024/03/IMG_1.JPG").write_bytes(b"x" * 100)

    vetoes = retro_clean.copy_vetoes(store.iter_completed(), [other])
    assert vetoes["2024/03/IMG_1.JPG"].reason == SKIP_ELSEWHERE


def test_a_second_copy_of_a_different_size_is_not_the_same_file(store, tmp_path):
    tracked(store, "2024/03/IMG_1.JPG", size=100)
    other = tmp_path / "backup"
    (other / "2024/03").mkdir(parents=True)
    (other / "2024/03/IMG_1.JPG").write_bytes(b"x" * 99)

    assert retro_clean.copy_vetoes(store.iter_completed(), [other]) == {}


# --- scan(): the whole-run tripwires ----------------------------------------------


def _scan(store, root, logs, tmp_path, **kw):
    return retro_clean.scan(
        store, output_root=root, logs_dir=logs,
        cache_db=kw.pop("cache_db", tmp_path / "absent-cache.db"),
        max_bytes=kw.pop("max_bytes", 1_048_576), min_bytes=kw.pop("min_bytes", 0),
        **kw)


def test_an_unreadable_output_root_refuses_before_anything_else(store, logs, tmp_path):
    result = _scan(store, tmp_path / "not-mounted", logs, tmp_path)
    assert result.structural and "not a readable directory" in result.structural[0]
    assert result.evidence is None


def test_no_completed_pass_refuses(tmp_path, root, logs):
    with StateStore(tmp_path / "fresh.db") as store:
        result = _scan(store, root, logs, tmp_path)
    assert "never recorded a completed full pass" in result.structural[0]


def test_missing_files_with_no_logged_trash_round_refuse(store, root, logs, tmp_path):
    tracked(store, "2024/03/IMG_1.JPG")
    write_log(logs, "icloud-photo-sync.log", [other_line("2026-07-05 23:15:24")])
    result = _scan(store, root, logs, tmp_path)
    assert "no trash round is logged" in result.structural[0]


def test_a_log_gap_over_the_window_refuses(store, root, logs, tmp_path):
    tracked(store, "2024/03/IMG_1.JPG")
    write_log(logs, "icloud-photo-sync.log", [trash_line("2026-08-10 11:04:33")])
    result = _scan(store, root, logs, tmp_path)
    assert "unobserved gap" in result.structural[0]


def test_no_logs_at_all_refuse(store, root, logs, tmp_path):
    tracked(store, "2024/03/IMG_1.JPG")
    result = _scan(store, root, logs, tmp_path)
    assert "no log files were found" in result.structural[0]


def test_one_file_outside_the_envelope_stops_the_whole_run(store, root, logs, tmp_path):
    """The tripwire is deliberately global: a missing HEIC means something other
    than a clean session removed files, and the premise is what is wrong."""
    tracked(store, "2024/03/IMG_1.JPG", id="a1")
    tracked(store, "2024/03/IMG_2.HEIC", id="a2", filename="IMG_2.HEIC")
    write_log(logs, "icloud-photo-sync.log",
              [other_line("2026-07-05 23:15:24"), trash_line()])

    result = _scan(store, root, logs, tmp_path)
    assert result.out_of_envelope == ["2024/03/IMG_2.HEIC"]
    assert any("outside every clean" in line for line in result.structural)


def test_a_clean_reconstruction_passes_every_tripwire(store, root, logs, tmp_path):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    write_log(logs, "icloud-photo-sync.log",
              [other_line("2026-07-05 23:15:24"), trash_line()])

    result = _scan(store, root, logs, tmp_path)
    assert result.structural == []
    assert result.rels == [rel]
    assert result.evidence.envelopes == {rel: "local-clean"}
    assert result.evidence.verified_present_at == PASS_AT
    assert "on disk at" in result.evidence.corroboration[rel][0]


def test_nothing_missing_is_not_a_refusal(store, root, logs, tmp_path):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    (root / "2024/03").mkdir(parents=True)
    (root / rel).write_bytes(b"x" * 100)
    result = _scan(store, root, logs, tmp_path)
    assert result.structural == [] and result.missing == []


def test_scan_feeds_build_plan_end_to_end(store, root, logs, tmp_path):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    write_log(logs, "icloud-photo-sync.log",
              [other_line("2026-07-05 23:15:24"), trash_line()])

    result = _scan(store, root, logs, tmp_path)
    plan = build_plan(result.rels, state=store, output_root=root,
                      evidence=EVIDENCE_RETROSPECTIVE, retro=result.evidence)
    [candidate] = plan.candidates
    assert candidate.rel == rel
    assert candidate.evidence == EVIDENCE_RETROSPECTIVE
    assert candidate.corroboration      # the notes travel into the manifest


# --- guards ---------------------------------------------------------------------


def test_retro_refusal_reports_the_structural_reason_first(store, root):
    rel = tracked(store, "2024/03/IMG_1.JPG")
    plan = build_plan([rel], state=store, output_root=root,
                      evidence=EVIDENCE_RETROSPECTIVE,
                      retro=evidence(envelopes={rel: "local-clean"}))
    assert plan.retro_refusal(completed_rows=1000, structural=["drive not mounted"]) \
        == "drive not mounted"


def test_retro_refusal_still_applies_the_proportion_guard(store, root):
    """Slicing into confirmed batches must not become a way around the 25% rule."""
    rels = []
    for n in range(60):
        rels.append(tracked(store, f"2024/03/IMG_{n}.JPG", id=f"a{n}"))
    plan = build_plan(rels, state=store, output_root=root,
                      evidence=EVIDENCE_RETROSPECTIVE,
                      retro=evidence(envelopes={r: "local-clean" for r in rels}))
    assert len(plan.candidates) == 60
    assert plan.retro_refusal(completed_rows=240, structural=[]) is None   # exactly 25%
    assert "more than 25%" in plan.retro_refusal(completed_rows=239, structural=[])


def test_retro_refusal_does_not_apply_the_per_run_cap(store, root):
    """The cap is spent on extra confirmations, not on a refusal — see
    _apply_in_slices."""
    rels = [tracked(store, f"2024/03/IMG_{n}.JPG", id=f"a{n}") for n in range(30)]
    plan = build_plan(rels, state=store, output_root=root,
                      evidence=EVIDENCE_RETROSPECTIVE,
                      retro=evidence(envelopes={r: "local-clean" for r in rels}))
    assert plan.retro_refusal(completed_rows=10_000, structural=[]) is None
    assert plan.guard_refusal(completed_rows=10_000, max_delete=10) is not None


# --- the module boundary ----------------------------------------------------------


def test_retro_clean_cannot_reach_the_network():
    import ast
    import pathlib

    source = pathlib.Path(retro_clean.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not {"auth", "icloud_client", "requests", "pyicloud"} & {
        m.split(".")[-1] for m in imported}


def test_the_video_envelope_admits_everything_at_the_default_min_size():
    """Not a bug: video-clean's default --min-size is 0, so it really does list
    every video. Pinned so nobody mistakes the envelope for a filter there."""
    assert retro_clean.envelope_for("a/b.MOV", 1, max_bytes=1_048_576,
                                    min_bytes=0) == "video-clean"
    assert retro_clean.envelope_for("a/b.MOV", 10**12, max_bytes=1_048_576,
                                    min_bytes=0) == "video-clean"


def test_a_missing_video_never_trips_the_out_of_envelope_wire_at_min_size_zero(
        store, root, logs, tmp_path):
    tracked(store, "2024/03/CLIP.MOV", filename="CLIP.MOV", size=500_000_000)
    write_log(logs, "icloud-photo-sync.log",
              [other_line("2026-07-05 23:15:24"), trash_line()])
    result = _scan(store, root, logs, tmp_path)
    assert result.out_of_envelope == [] and result.structural == []
