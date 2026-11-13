import json
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock
from gemini_image_api.updates import check_update, run_update

def test_check_update_cached(tmp_path):
    cache_file = tmp_path / "update_check.json"
    cache_file.write_text(json.dumps({
        "last_checked": 9999999999.0,
        "latest_sha": "abc1234567",
        "has_update": False,
        "message": "Up to date",
    }))

    res = check_update(tmp_path, force=False)
    assert res["latest_sha"] == "abc1234567"
    assert res["has_update"] is False

def test_check_update_queries_github(tmp_path):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "sha": "new_sha_999999",
        "commit": {"message": "feat: new feature\n\ndetails"}
    }

    with patch("httpx.get", return_value=mock_resp), \
         patch("gemini_image_api.updates.get_local_sha", return_value="old_sha_111111"):
        res = check_update(tmp_path, force=True)
        assert res["has_update"] is True
        assert res["latest_sha"] == "new_sha_999999"
        assert "feat: new feature" in res["message"]

def test_run_update_calls_uv(monkeypatch):
    mock_run = MagicMock()
    mock_run.returncode = 0
    with patch("shutil.which", return_value="/usr/local/bin/uv"), \
         patch("subprocess.run", return_value=mock_run):
        code = run_update()
        assert code == 0
