from musa import errors
from musa.errors import (GIAError, AUTH_EXPIRED, UNKNOWN_STYLE,
                                     error_payload, NEXT_ACTION, RETRYABLE)


def test_error_carries_code_and_details():
    err = GIAError(AUTH_EXPIRED, "sesion expirada", hint="run musa auth")
    assert err.code == "auth_expired"
    assert err.details == {"hint": "run musa auth"}
    assert "sesion expirada" in str(err)

def test_unknown_style_code():
    err = GIAError(UNKNOWN_STYLE, "estilo no existe", valid=["anime", "anime2"])
    assert err.code == "unknown_style" and err.details["valid"] == ["anime", "anime2"]

def test_all_codes_are_unique_strings():
    vals = [v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str)]
    assert len(vals) == len(set(vals)) and len(vals) == 29


def test_invalid_query_has_next_action_and_is_not_retryable():
    assert errors.INVALID_QUERY == "invalid_query" and errors.INVALID_QUERY not in RETRYABLE
    p = error_payload(GIAError(errors.INVALID_QUERY, "bad since"))
    assert p["next_action"] == NEXT_ACTION[errors.INVALID_QUERY] and "ISO" in p["next_action"]
    assert p["retryable"] is False

def test_prompt_errors_not_retryable():
    assert "invalid_prompt" not in RETRYABLE and "unknown_prompt" not in RETRYABLE
    assert error_payload(GIAError("unknown_prompt", "x"))["retryable"] is False



def _codes():
    return [v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str)]

def test_every_code_has_a_next_action():
    assert set(_codes()) == set(NEXT_ACTION)

def test_error_payload_is_actionable():
    p = error_payload(GIAError("quota_guard", "5h window at 92%", resets_at="2026-11-10T03:28:32-05:00"))
    assert p["status"] == "error" and p["code"] == "quota_guard" and p["retryable"] is True
    assert "reset" in p["next_action"].lower()
    assert p["details"]["resets_at"].startswith("2026-11-10")
    assert error_payload(GIAError("usage_limit", "x"))["retryable"] is False


def test_next_actions_cover_both_providers():
    assert "auth --chatgpt" in NEXT_ACTION[errors.AUTH_EXPIRED]
    assert "GIA_CHROME_PATH" in NEXT_ACTION[errors.AUTH_FAILED]
    assert "Gemini" not in NEXT_ACTION[errors.NO_IMAGE]
