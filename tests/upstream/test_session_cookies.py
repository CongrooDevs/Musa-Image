import pytest

from musa.upstream.session import UpstreamSession


@pytest.mark.asyncio
async def test_session_sends_gemini_host_cookies_and_explicit_cookies_win():
    jar = [{"name": "__Secure-1PSID", "value": "jar-psid", "domain": ".google.com"},
           {"name": "__Secure-1PSIDTS", "value": "old-ts", "domain": ".google.com"},
           {"name": "SAPISID", "value": "sap", "domain": ".google.com"},
           {"name": "__Secure-OSID", "value": "osid", "domain": "gemini.google.com"}]
    s = UpstreamSession({"__Secure-1PSID": "psid", "__Secure-1PSIDTS": "rotated"}, cookie_jar=jar)
    assert s.cookies == {"SAPISID": "sap", "__Secure-1PSID": "psid", "__Secure-1PSIDTS": "rotated"}
    http = await s._get_session()
    sent = {(c.name, c.domain, c.value) for c in http.cookies.jar}
    assert ("__Secure-OSID", "gemini.google.com", "osid") in sent
    assert ("__Secure-1PSIDTS", ".google.com", "rotated") in sent
    await s.close()
