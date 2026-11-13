import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixtures" / "scene_golden.json"
SCRIPT = ROOT / "js" / "scene_golden.cjs"


def _close(a, e):
    if isinstance(e, dict):
        assert a.keys() == e.keys()
        for k in e:
            _close(a[k], e[k])
    elif isinstance(e, list):
        assert len(a) == len(e)
        for x, y in zip(a, e):
            _close(x, y)
    else:
        assert a == pytest.approx(e, abs=1e-6)


@pytest.mark.skipif(shutil.which("node") is None, reason="node no disponible")
def test_js_matches_golden():
    out = subprocess.run(
        ["node", str(SCRIPT)], capture_output=True, text=True, timeout=30,
        cwd=ROOT.parent, check=True,
    ).stdout
    results = json.loads(out)["results"]
    cases = json.loads(FIXTURE.read_text())["cases"]
    assert len(results) == len(cases) >= 30
    for r, c in zip(results, cases):
        _close(r, c["expect"])
