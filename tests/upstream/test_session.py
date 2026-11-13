import hashlib
import time
import pytest
from unittest.mock import AsyncMock, patch
from musa.upstream.session import UpstreamSession, compute_sapisid_hash
from musa.upstream.exceptions import AuthError, TemporarilyBlockedError

def test_compute_sapisid_hash():
    sapisid = "test_sapisid_val"
    origin = "https://gemini.google.com"
    h = compute_sapisid_hash(sapisid, origin)
    ts, digest = h.split("_")
    assert ts.isdigit()
    expected = hashlib.sha1(f"{ts} {sapisid} {origin}".encode("utf-8")).hexdigest()
    assert digest == expected

@pytest.mark.asyncio
async def test_session_init_extracts_snlm0e():
    cookies = {"__Secure-1PSID": "fake_psid", "SAPISID": "fake_sapisid"}
    session = UpstreamSession(cookies=cookies)

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html><script>window._F_userData = []; "SNlM0e":"snlm0e_extracted_token_123"</script></html>'

    mock_http = AsyncMock()
    mock_http.get.return_value = mock_resp

    with patch.object(session, "_get_session", return_value=mock_http):
        await session.init()
        assert session.snlm0e == "snlm0e_extracted_token_123"

@pytest.mark.asyncio
async def test_session_init_expired_raises_auth_error():
    cookies = {"__Secure-1PSID": "fake_psid"}
    session = UpstreamSession(cookies=cookies)

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html><body>Please sign in to continue</body></html>'

    mock_http = AsyncMock()
    mock_http.get.return_value = mock_resp

    with patch.object(session, "_get_session", return_value=mock_http):
        with pytest.raises(AuthError, match="SNlM0e"):
            await session.init()

@pytest.mark.asyncio
async def test_session_post_batchexecute_rate_limited():
    cookies = {"__Secure-1PSID": "fake_psid"}
    session = UpstreamSession(cookies=cookies)
    session.snlm0e = "mock_snlm0e"

    mock_resp = AsyncMock()
    mock_resp.status_code = 429
    mock_resp.text = "Too Many Requests"

    mock_http = AsyncMock()
    mock_http.post.return_value = mock_resp

    with patch.object(session, "_get_session", return_value=mock_http):
        with pytest.raises(TemporarilyBlockedError):
            await session.post_batchexecute([("rpc_1", [], "generic")])
