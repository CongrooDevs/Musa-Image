import json
from pathlib import Path
import pytest
from musa.upstream.batchexecute import parse_stream_generate_response

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "upstream" / "call_13_StreamGenerate.json"

def test_parse_stream_generate_fixture():
    assert FIXTURE_PATH.exists()
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    raw_response = data["response_text"]

    turn = parse_stream_generate_response(raw_response)
    assert turn is not None
    assert turn.chat.cid.startswith("c_")
    assert turn.chat.rid.startswith("r_")
    assert turn.chat.rcid.startswith("rc_")
    assert len(turn.images) >= 1
    for img in turn.images:
        assert "googleusercontent.com" in img.url
        assert img.image_id.startswith("img_")
        assert len(img.image_id) == 20  # "img_" + 16 hex chars
