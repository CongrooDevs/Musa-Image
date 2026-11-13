import json
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from musa.upstream.session import UpstreamSession
from musa.upstream.client import GeminiClient
from musa.upstream.batchexecute import parse_stream_generate_response
from musa.upstream.exceptions import (
    UsageLimitExceededError, ModelInvalidError, TemporarilyBlockedError
)

@pytest.mark.asyncio
async def test_rotate_cookies_atomic(tmp_path):
    cookies = {"__Secure-1PSID": "test_psid_12345", "__Secure-1PSIDTS": "old_ts"}
    session = UpstreamSession(cookies)

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_resp)

    # Mock curl_cffi cookies jar having updated cookie
    cookie_mock = MagicMock()
    cookie_mock.name = "__Secure-1PSIDTS"
    cookie_mock.value = "new_rotated_ts_999"
    mock_http.cookies.jar = [cookie_mock]

    with patch.object(session, "_get_session", return_value=mock_http):
        res = await session.rotate_cookies(cache_dir=tmp_path)
        assert res == "new_rotated_ts_999"
        assert session.cookies["__Secure-1PSIDTS"] == "new_rotated_ts_999"

        # Check atomic cache file
        cache_file = tmp_path / ".cached_cookies_test_psid_12345.json"
        assert cache_file.exists()
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert any(c["name"] == "__Secure-1PSIDTS" and c["value"] == "new_rotated_ts_999" for c in data)

@pytest.mark.asyncio
async def test_client_fetch_usage_info():
    client = GeminiClient(secure_1psid="mock_psid")
    fixture_path = Path(__file__).resolve().parent.parent / "fixtures" / "upstream" / "call_06_POST.json"
    assert fixture_path.exists()
    raw_fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["response_text"]

    with patch.object(client.session, "post_batchexecute", new=AsyncMock(return_value=raw_fixture)):
        info = await client.fetch_usage_info()
        assert info is not None
        assert info["tier"]["label"] == "PRO"
        assert "current_5h" in info
        assert "weekly" in info
        assert client.usage_info == info

def test_parse_stream_generate_error_codes():
    raw_limit_err = ')]}\'\n\n[["wrb.fr",null,null,null,null,[3,null,[["type.googleapis.com/assistant.boq.bard.application.BardErrorInfo",[1037]]]]]]'
    with pytest.raises(UsageLimitExceededError):
        parse_stream_generate_response(raw_limit_err)

    raw_model_err = ')]}\'\n\n[["wrb.fr",null,null,null,null,[3,null,[["type.googleapis.com/assistant.boq.bard.application.BardErrorInfo",[1050]]]]]]'
    with pytest.raises(ModelInvalidError):
        parse_stream_generate_response(raw_model_err)

    raw_blocked_err = ')]}\'\n\n[["wrb.fr",null,null,null,null,[3,null,[["type.googleapis.com/assistant.boq.bard.application.BardErrorInfo",[1060]]]]]]'
    with pytest.raises(TemporarilyBlockedError):
        parse_stream_generate_response(raw_blocked_err)

    from musa.upstream.exceptions import ImageGenerationError
    raw_content_blocked = ')]}\'\n\n[["wrb.fr",null,null,null,null,[3,null,[["type.googleapis.com/assistant.boq.bard.application.BardErrorInfo",[1003]]]]]]'
    with pytest.raises(ImageGenerationError):
        parse_stream_generate_response(raw_content_blocked)

@pytest.mark.asyncio
async def test_client_auto_rotation_lifecycle(tmp_path):
    client = GeminiClient(secure_1psid="mock_psid", cache_dir=tmp_path)
    with patch.object(client.session, "init", new=AsyncMock()), \
         patch.object(client, "fetch_usage_info", new=AsyncMock()):
        await client.init(auto_refresh=True, refresh_interval=0.01)
        assert client._rotation_task is not None
        assert not client._rotation_task.done()
        await client.close()
        assert client._rotation_task is None
