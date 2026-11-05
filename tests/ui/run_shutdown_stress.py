"""Run the one-off synthetic shutdown regression with an external deadline."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[2]
    output = root / ".test-tmp"
    output.mkdir(exist_ok=True)
    environment = dict(os.environ)
    environment["OFFICEFLOW_DATA_DIR"] = str(output / "stress-profile")
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONUTF8"] = "1"
    command = [sys.executable, str(Path(__file__).with_name("shutdown_stress_child.py"))]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as error:
        data = error.stdout or b""
        (output / "shutdown-stress.jsonl").write_bytes(
            data.encode() if isinstance(data, str) else data
        )
        print("Synthetic shutdown child exceeded 180 seconds; its owned process was stopped.")
        return 1
    (output / "shutdown-stress.jsonl").write_text(result.stdout, encoding="utf-8")
    (output / "shutdown-stress.stderr.log").write_text(result.stderr, encoding="utf-8")
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
