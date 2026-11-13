import pytest
from unittest.mock import AsyncMock, patch
from musa.upstream.client import GeminiClient
from musa.upstream.types import AccountStatus, UpstreamModel, UpstreamGem

@pytest.mark.asyncio
async def test_native_client_lifecycle_and_models():
    client = GeminiClient(secure_1psid="mock_psid")

    import json
    from pathlib import Path
    fixture_path = Path(__file__).resolve().parent.parent / "fixtures" / "upstream" / "call_01_POST.json"
    raw_fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["response_text"]

    with patch.object(client.session, "init", new=AsyncMock()), \
         patch.object(client.session, "post_batchexecute", new=AsyncMock(return_value=raw_fixture)):
        await client.init()
        assert client.account_status == AccountStatus.AVAILABLE

        models = client.list_models()
        assert len(models) >= 3
        assert any(m["model_name"] == "gemini-flash" for m in models)

        resolved = client.resolve_model("gemini-flash")
        assert resolved.model_name == "gemini-flash"

        await client.close()

@pytest.mark.asyncio
async def test_native_client_gem_crud():
    client = GeminiClient(secure_1psid="mock_psid")

    with patch.object(client.session, "post_batchexecute", new=AsyncMock()) as mock_post:
        mock_post.return_value = ')]}\'\n\n[["wrb.fr","oMH3Zd","[\\"gem_real_123\\"]",null,null,null,"generic"]]'
        # Mock create_gem
        created = await client.create_gem(name="Musa Style", prompt="Do art", description="Art desc")
        assert created.id == "gem_real_123"
        assert created.name == "Musa Style"

        # Mock update_gem
        await client.update_gem(created.id, name="Musa Style 2", prompt="Updated prompt")

        # Mock delete_gem
        await client.delete_gem(created.id)

        # Mock fetch_gems
        mock_post.return_value = ')]}\'\n\n[["wrb.fr","CNgdBe","[null,null,[[\\"gem_1\\",[\\"Custom\\",\\"desc\\",null,null,null,null,null,null,null,\\"inst\\"]]]]",null,null,null,"custom"]]'
        gems = await client.fetch_gems()
        assert isinstance(gems, list)

@pytest.mark.asyncio
async def test_native_client_generate_content_parses_real_stream():
    import json
    from pathlib import Path
    from contextlib import asynccontextmanager

    fixture_path = Path(__file__).resolve().parent.parent / "fixtures" / "upstream" / "call_13_StreamGenerate.json"
    fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
    raw_text = fixture_data["response_text"]

    client = GeminiClient(secure_1psid="mock_psid")
    client.session.snlm0e = "mock_snlm0e"

    mock_http = AsyncMock()

    @asynccontextmanager
    async def mock_stream(*args, **kwargs):
        resp = AsyncMock()
        resp.status_code = 200
        async def aiter():
            yield raw_text.encode("utf-8")
        resp.aiter_content = aiter
        yield resp

    mock_http.stream = mock_stream

    with patch.object(client.session, "_get_session", return_value=mock_http):
        res = await client.generate_content("un cuadrado verde")
        assert len(res.images) >= 1
        assert "googleusercontent.com" in res.images[0].url
        assert res.images[0].client_ref is client
        assert res.images[0].cid.startswith("c_")
        assert res.images[0].rid.startswith("r_")

@pytest.mark.asyncio
async def test_generate_content_stream_429_raises_temporarily_blocked():
    from contextlib import asynccontextmanager
    from musa.upstream.exceptions import TemporarilyBlockedError

    client = GeminiClient(secure_1psid="mock_psid")
    client.session.snlm0e = "mock_snlm0e"

    mock_http = AsyncMock()

    @asynccontextmanager
    async def mock_stream_429(*args, **kwargs):
        resp = AsyncMock()
        resp.status_code = 429
        yield resp

    mock_http.stream = mock_stream_429

    with patch.object(client.session, "_get_session", return_value=mock_http):
        with pytest.raises(TemporarilyBlockedError, match="Rate limited"):
            await client.generate_content("un cuadrado verde")
