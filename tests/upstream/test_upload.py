import json
import pytest
from unittest.mock import AsyncMock, patch
from gemini_image_api.upstream.upload import upload_reference_image
from gemini_image_api.upstream.session import UpstreamSession

@pytest.mark.asyncio
async def test_upload_reference_image_flow():
    session = UpstreamSession(cookies={"__Secure-1PSID": "fake_psid"})

    from unittest.mock import MagicMock
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "/contrib_service/ttl_1d/fake_upload_token_12345"

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_resp)

    with patch.object(session, "_get_session", return_value=mock_http):
        token = await upload_reference_image(session, b"\x89PNG\r\n\x1a\nfakeimagebytes", "ref.png")
        assert token == "/contrib_service/ttl_1d/fake_upload_token_12345"
