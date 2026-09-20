from __future__ import annotations

from pathlib import Path

from officeflow.bootstrap.run_state import RunStateTracker


def test_run_state_detects_unclean_run_and_clears_on_clean_shutdown(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "officeflow.running"
    tracker = RunStateTracker(marker)

    assert tracker.begin() is False
    assert marker.is_file()
    assert RunStateTracker(marker).begin() is True

    tracker.mark_clean()

    assert not marker.exists()
