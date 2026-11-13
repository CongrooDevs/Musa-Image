import json
from pathlib import Path
import pytest
from gemini_image_api.upstream.batchexecute import (
    build_batchexecute_req, parse_batchexecute_chunks, get_rpc_result
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "upstream"

def test_build_batchexecute_req():
    calls = [("otAQ7b", [], "generic")]
    req_str = build_batchexecute_req(calls)
    parsed = json.loads(req_str)
    assert len(parsed) == 1
    assert parsed[0][0][0] == "otAQ7b"
    assert parsed[0][0][1] == "[]"
    assert parsed[0][0][3] == "generic"

def test_parse_batchexecute_chunks_against_real_fixture():
    fixture_path = FIXTURES_DIR / "call_11_POST.json"
    assert fixture_path.exists()
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    raw_response = fixture["response_text"]

    chunks = parse_batchexecute_chunks(raw_response)
    assert len(chunks) >= 2

    rpc_names = [c[0] for c in chunks]
    assert "CNgdBe" in rpc_names

    # Check custom and system results
    res_custom = get_rpc_result(raw_response, "CNgdBe", label="custom")
    assert res_custom is not None
    assert isinstance(res_custom, list)

    res_system = get_rpc_result(raw_response, "CNgdBe")
    assert res_system is not None

def test_parse_batchexecute_models_fixture():
    fixture_path = FIXTURES_DIR / "call_01_POST.json"
    assert fixture_path.exists()
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    raw_response = fixture["response_text"]

    models_data = get_rpc_result(raw_response, "otAQ7b")
    assert models_data is not None
    assert isinstance(models_data, list)
    # Check that Flash-Lite model is present in the payload
    serialized = json.dumps(models_data)
    assert "Flash-Lite" in serialized
