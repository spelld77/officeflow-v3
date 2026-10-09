import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
@pytest.mark.parametrize("palette", ["light", "dark"])
def test_phase_three_narrow_controls_and_render(scale, palette, tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "tests.ui.task_history_visual_child", str(tmp_path), scale, palette],
        capture_output=True,
        text=True,
        timeout=40,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert completed.returncode == 0, completed.stderr
    values = json.loads(completed.stdout)
    assert len(values) == 5
    assert all(value["dpr"] == float(scale) and value["width"] <= 520 for value in values)
