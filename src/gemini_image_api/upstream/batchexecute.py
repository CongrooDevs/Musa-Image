"""Parser and builder for Google's batchexecute RPC protocol."""
import hashlib
import json
import re
from typing import Any
from .exceptions import APIError

_ANTI_XSSI_RE = re.compile(r"^\)\]\}'\s*")

def clean_anti_xssi(text: str) -> str:
    """Strips Google's anti-XSSI header )]}'\n."""
    return _ANTI_XSSI_RE.sub("", text).strip()

def build_batchexecute_req(calls: list[tuple[str, Any, str | None]]) -> str:
    """Builds the string for the f.req POST parameter.
    Each item in calls is (rpc_id, payload_obj, label_or_generic).
    """
    req_list = []
    for rpc_id, payload, label in calls:
        if isinstance(payload, str):
            payload_str = payload
        else:
            payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        target_label = label or "generic"
        req_list.append([rpc_id, payload_str, None, target_label])
    return json.dumps([req_list], separators=(",", ":"), ensure_ascii=False)

def parse_batchexecute_chunks(raw_response: str) -> list[tuple[str, Any, str | None]]:
    """Parses batchexecute chunked response into a list of (rpc_id, decoded_payload, label)."""
    cleaned = clean_anti_xssi(raw_response)
    results = []

    lines = cleaned.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.isdigit():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(parsed, list):
            continue

        for chunk in parsed:
            if not isinstance(chunk, list) or len(chunk) < 3:
                continue

            # Look for ["wrb.fr", rpc_id, payload_str, ...]
            if chunk[0] == "wrb.fr":
                rpc_id = chunk[1]
                payload_raw = chunk[2]
                label = chunk[6] if len(chunk) > 6 and isinstance(chunk[6], str) else None

                # Check for RPC-level error
                if len(chunk) > 5 and chunk[5] is not None:
                    err = chunk[5]
                    msg = err[1] if isinstance(err, list) and len(err) > 1 else str(err)
                    raise APIError(f"RPC {rpc_id} error: {msg}")

                if payload_raw is None:
                    results.append((rpc_id, None, label))
                    continue

                if isinstance(payload_raw, str):
                    try:
                        decoded = json.loads(payload_raw)
                    except json.JSONDecodeError:
                        decoded = payload_raw
                else:
                    decoded = payload_raw

                results.append((rpc_id, decoded, label))

    return results

def get_rpc_result(raw_response: str, rpc_id: str, label: str | None = None) -> Any:
    """Finds and returns the first matching RPC payload."""
    chunks = parse_batchexecute_chunks(raw_response)
    for c_rpc, c_data, c_label in chunks:
        if c_rpc == rpc_id:
            if label is None or c_label == label:
                return c_data
    raise APIError(f"RPC {rpc_id} (label={label}) not found in response")
def build_stream_generate_req(
    prompt: str,
    cid: str = "",
    rid: str = "",
    rcid: str = "",
    gem_id: str | None = None,
    file_data: list[tuple[str, str]] | None = None,
    temporary: bool = False,
    lang: str = "es-419",
) -> str:
    """Builds the f.req string for Endpoint.GENERATE (StreamGenerate)."""
    import uuid
    inner = [None] * 81
    files_payload = None
    if file_data:
        files_payload = [[[u], fname] for u, fname in file_data]
    inner[0] = [prompt, 0, None, files_payload, None, None, 0]
    inner[1] = [lang]
    inner[2] = [cid or "", rid or "", rcid or "", None, None, None, None, None, None, ""]
    inner[6] = [1]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[0]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [1]
    if temporary:
        inner[45] = 1
    inner[53] = 0
    inner[59] = str(uuid.uuid4()).upper()
    inner[61] = []
    inner[68] = 1
    inner[79] = 6
    inner[80] = 1
    if gem_id:
        inner[19] = gem_id
    payload = [None, json.dumps(inner, separators=(",", ":"), ensure_ascii=False)]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

def parse_stream_generate_response(raw_response: str) -> Any:
    """Parses Google's StreamGenerate chunked response and returns an UpstreamTurn."""
    from .types import UpstreamTurn, GeneratedImage, UpstreamChatSession

    cleaned = clean_anti_xssi(raw_response)
    cid = ""
    rid = ""
    rcid = ""
    text = ""
    image_urls = []

    for line in cleaned.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(parsed, list):
            continue

        for chunk in parsed:
            if not isinstance(chunk, list) or len(chunk) < 3:
                continue
            # Check chunk[5] for BardErrorInfo codes
            if len(chunk) > 5 and isinstance(chunk[5], list) and len(chunk[5]) > 2:
                err_details = chunk[5][2]
                if isinstance(err_details, list):
                    for detail in err_details:
                        if isinstance(detail, list) and len(detail) > 1 and detail[0] == "type.googleapis.com/assistant.boq.bard.application.BardErrorInfo":
                            codes = detail[1]
                            if isinstance(codes, list) and codes:
                                err_code = codes[0]
                                if err_code == 1037:
                                    from .exceptions import UsageLimitExceededError
                                    raise UsageLimitExceededError("Gemini usage limit reached (code 1037)")
                                elif err_code in (1050, 1052):
                                    from .exceptions import ModelInvalidError
                                    raise ModelInvalidError(f"Model invalid or unavailable (code {err_code})")
                                elif err_code == 1060:
                                    from .exceptions import TemporarilyBlockedError
                                    raise TemporarilyBlockedError("IP temporarily flagged by Google (code 1060)")
                                elif err_code == 1003:
                                    from .exceptions import ImageGenerationError
                                    raise ImageGenerationError("Image generation refused by safety or content policy (code 1003)")

            inner_str = chunk[2]
            if not inner_str or not isinstance(inner_str, str):
                continue

            try:
                inner = json.loads(inner_str)
            except json.JSONDecodeError:
                continue

            if not isinstance(inner, list):
                continue

            # Extract conversation metadata [cid, rid]
            if len(inner) > 1 and isinstance(inner[1], list):
                if len(inner[1]) > 0 and inner[1][0]:
                    cid = inner[1][0]
                if len(inner[1]) > 1 and inner[1][1]:
                    rid = inner[1][1]

            # Extract candidates
            if len(inner) > 4 and isinstance(inner[4], list):
                for cand in inner[4]:
                    if not isinstance(cand, list) or not cand:
                        continue
                    if len(cand) > 0 and cand[0]:
                        rcid = cand[0]
                    if len(cand) > 1 and isinstance(cand[1], list) and cand[1]:
                        text = cand[1][0] or ""

                    # Extract image URLs from any candidate elements
                    for element in cand:
                        if not element:
                            continue
                        from urllib.parse import urlparse
                        def find_urls(obj):
                            if isinstance(obj, str) and obj.startswith(("http://", "https://")):
                                try:
                                    p_url = urlparse(obj)
                                    if p_url.hostname == "lh3.googleusercontent.com":
                                        if obj not in image_urls:
                                            image_urls.append(obj)
                                except Exception:
                                    pass
                            elif isinstance(obj, list):
                                for x in obj:
                                    find_urls(x)
                            elif isinstance(obj, dict):
                                for x in obj.values():
                                    find_urls(x)
                        find_urls(element)

    images = [
        GeneratedImage(
            url=u,
            cid=cid,
            rid=rid,
            rcid=rcid,
            image_id=f"img_{hashlib.sha256(u.encode('utf-8')).hexdigest()[:16]}",
        )
        for u in image_urls
    ]

    return UpstreamTurn(
        text=text,
        images=images,
        chat=UpstreamChatSession(cid=cid, rid=rid, rcid=rcid),
    )
