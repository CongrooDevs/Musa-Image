import pytest
from musa.upstream.sanitizer import TrafficSanitizer

def test_traffic_sanitizer_redacts_sentinel_cookies():
    fake_jar = [
        {"name": "__Secure-1PSID", "value": "sentinel_psid_val_9876543210"},
        {"name": "__Secure-1PSIDTS", "value": "sentinel_psidts_val_123456789"},
        {"name": "SAPISID", "value": "sentinel_sapisid_val_555666777"},
    ]
    sanitizer = TrafficSanitizer(fake_jar)

    wire_text = (
        'GET /app HTTP/1.1\r\n'
        'Cookie: __Secure-1PSID=sentinel_psid_val_9876543210; __Secure-1PSIDTS=sentinel_psidts_val_123456789\r\n'
        'Authorization: SAPISIDHASH 1731450000_1234567890abcdef1234567890abcdef12345678\r\n\r\n'
        '{"user": "sentinel_sapisid_val_555666777", "token": "SNlM0e\":\"sentinel_snlm0e_secret_token\"}'
    )

    sanitized = sanitizer.sanitize_text(wire_text)

    # Sentinel secrets MUST NOT appear
    assert "sentinel_psid_val_9876543210" not in sanitized
    assert "sentinel_psidts_val_123456789" not in sanitized
    assert "sentinel_sapisid_val_555666777" not in sanitized
    assert "sentinel_snlm0e_secret_token" not in sanitized
    assert "1234567890abcdef1234567890abcdef12345678" not in sanitized

    # Redacted markers MUST appear
    assert "REDACTED___Secure-1PSID" in sanitized
    assert "REDACTED___Secure-1PSIDTS" in sanitized
    assert "REDACTED_SAPISID" in sanitized
    assert "REDACTED_SNLM0E" in sanitized
    assert "REDACTED_HASH" in sanitized

def test_traffic_sanitizer_dict_redaction():
    fake_jar = [
        {"name": "__Secure-1PSID", "value": "secret_session_token_xyz123"},
    ]
    sanitizer = TrafficSanitizer(fake_jar)

    headers = {
        "Set-Cookie": "secret_session_token_xyz123; Path=/",
        "Authorization": "SAPISIDHASH 1731450000_1234567890abcdef1234567890abcdef12345678",
        "X-Custom-Data": "Contains secret_session_token_xyz123 inside payload",
        "Nested": {
            "List": ["safe_value", "secret_session_token_xyz123"]
        }
    }

    sanitized = sanitizer.sanitize_dict(headers)

    assert sanitized["Set-Cookie"] == "REDACTED_SET-COOKIE"
    assert sanitized["Authorization"] == "REDACTED_AUTHORIZATION"
    assert "secret_session_token_xyz123" not in sanitized["X-Custom-Data"]
    assert "REDACTED___Secure-1PSID" in sanitized["X-Custom-Data"]
    assert "secret_session_token_xyz123" not in sanitized["Nested"]["List"][1]
    assert "REDACTED___Secure-1PSID" in sanitized["Nested"]["List"][1]
