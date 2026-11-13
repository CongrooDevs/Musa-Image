from musa import chatgpt_web as cw

CID = "6af76eed-1aec-83e9-b323-d348dba09bfa"


def _msg(mid, role, t, status="finished_successfully", parts=None, ctype="text"):
    return {"id": mid, "author": {"role": role}, "create_time": t, "status": status,
            "content": {"content_type": ctype, "parts": parts or []}}


def _img(fid):
    return {"content_type": "image_asset_pointer", "asset_pointer": f"sediment://{fid}", "width": 10, "height": 8}


def _conv(*msgs):
    mapping = {m["id"]: {"message": m} for m in msgs}
    mapping["root"] = {"message": None}
    return {"mapping": mapping}


def test_output_images_only_from_finished_tool_messages():
    conv = _conv(_msg("u1", "user", 1, parts=[_img("file_upload"), "hola"], ctype="multimodal_text"),
                 _msg("a1", "assistant", 2, parts=["Generando"]),
                 _msg("t1", "tool", 3, status="in_progress", parts=[_img("file_pending")], ctype="multimodal_text"),
                 _msg("t2", "tool", 4, parts=[_img("file_out")], ctype="multimodal_text"))
    imgs = cw.output_images(cw.new_messages(conv, set()))
    assert imgs == [cw.OutputImage("t2", "file_out", 10, 8)]


def test_new_messages_skips_known_and_orders_by_time():
    conv = _conv(_msg("b", "assistant", 5), _msg("a", "user", 1), _msg("c", "tool", 9))
    assert [m["id"] for m in cw.new_messages(conv, {"a"})] == ["b", "c"]
    assert cw.message_ids(conv) == {"a", "b", "c"}


def test_assistant_text_and_tool_detection():
    fresh = [_msg("a1", "assistant", 1, parts=["No puedo ", {"x": 1}, "hacer eso."]),
             _msg("a2", "assistant", 2, status="in_progress", parts=["..."])]
    assert cw.assistant_text(fresh) == "No puedo hacer eso."
    assert cw.assistant_text([_msg("u", "user", 1, parts=["x"])]) is None
    assert cw.has_tool_message(fresh) is False
    assert cw.has_tool_message([_msg("t", "tool", 1, status="in_progress")]) is True


def test_image_gen_quota():
    data = {"limits_progress": [{"feature_name": "file_upload", "remaining": 9},
                                {"feature_name": "image_gen", "remaining": 3, "reset_after": "2026-11-14T20:21:58Z"}]}
    assert cw.image_gen_quota(data) == {"remaining": 3, "reset_after": "2026-11-14T20:21:58Z"}
    assert cw.image_gen_quota({"limits_progress": []}) is None
    assert cw.image_gen_quota(None) is None


def test_conversation_id_from_url():
    assert cw.conversation_id_from_url(f"https://chatgpt.com/c/{CID}") == CID
    assert cw.conversation_id_from_url(f"https://chatgpt.com/c/{CID}?model=x") == CID
    assert cw.conversation_id_from_url("https://chatgpt.com/") is None


def test_is_blocking_response_only_on_posts_of_the_send_path():
    assert cw.is_blocking_response("POST", "https://chatgpt.com/backend-api/f/conversation", 403)
    assert cw.is_blocking_response("POST", "https://chatgpt.com/backend-api/sentinel/chat-requirements/prepare", 404)
    assert not cw.is_blocking_response("GET", f"https://chatgpt.com/backend-api/conversation/{CID}", 404)
    assert not cw.is_blocking_response("POST", "https://chatgpt.com/backend-api/files", 403)
    assert not cw.is_blocking_response("POST", "https://chatgpt.com/backend-api/f/conversation", 200)
