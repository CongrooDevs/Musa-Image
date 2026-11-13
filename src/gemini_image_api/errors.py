AUTH_EXPIRED = "auth_expired"
USAGE_LIMIT = "usage_limit"
TEMPORARILY_BLOCKED = "temporarily_blocked"
NO_IMAGE = "no_image"
TIMEOUT = "timeout"
GENERATED_NOT_SAVED = "generated_not_saved"
CONTENT_BLOCKED = "content_blocked"
INVALID_REF = "invalid_ref"
REF_NOT_ALLOWED = "ref_not_allowed"
SSRF_BLOCKED = "ssrf_blocked"
UNKNOWN_STYLE = "unknown_style"
TEMPLATE_MISSING_VAR = "template_missing_var"
UNKNOWN_CONVERSATION = "unknown_conversation"
UNKNOWN_MODEL = "unknown_model"
AUTH_FAILED = "auth_failed"
UPSTREAM_ERROR = "upstream_error"
QUOTA_GUARD = "quota_guard"
PATH_NOT_ALLOWED = "path_not_allowed"
INVALID_ASPECT = "invalid_aspect"
INVALID_COLLECTION = "invalid_collection"
UNKNOWN_COLLECTION = "unknown_collection"
INVALID_STYLE = "invalid_style"
INVALID_PROMPT = "invalid_prompt"
UNKNOWN_PROMPT = "unknown_prompt"
INVALID_QUERY = "invalid_query"
INVALID_ANIMATION = "invalid_animation"
UNKNOWN_ANIMATION = "unknown_animation"
INVALID_COMPOSITION = "invalid_composition"
UNKNOWN_COMPOSITION = "unknown_composition"

RETRYABLE = frozenset({TIMEOUT, TEMPORARILY_BLOCKED, GENERATED_NOT_SAVED, QUOTA_GUARD, UPSTREAM_ERROR})

NEXT_ACTION = {
    AUTH_EXPIRED: "Session expired: call auth_login (or run `gia auth`), then retry.",
    USAGE_LIMIT: "Google usage limit reached: stop generating and wait for the quota window to reset.",
    TEMPORARILY_BLOCKED: "Temporarily blocked by Google: wait several minutes before retrying; do not loop.",
    NO_IMAGE: "Gemini answered without an image: read gemini_text and rephrase the prompt.",
    TIMEOUT: "Timed out: retry once; if it repeats, simplify the prompt or reduce refs.",
    GENERATED_NOT_SAVED: "Image was generated but not saved: call retry_download (no quota cost); see details.reason.",
    CONTENT_BLOCKED: "Blocked by Gemini policy: change the prompt or refs.",
    INVALID_REF: "Fix the ref: local image path, https URL, or gen:<id>[#index] from list_generations.",
    REF_NOT_ALLOWED: "Ref path is outside the allowed directories: use a file under output_root or the working directory.",
    SSRF_BLOCKED: "URL points to a private/local address: use a public https URL.",
    UNKNOWN_STYLE: "Unknown style or template: call list_styles / list_templates and use one of details.valid.",
    TEMPLATE_MISSING_VAR: "Provide every variable listed in details.required via template_vars.",
    UNKNOWN_CONVERSATION: "Unknown conversation_id: call list_generations to find it, or generate_image to start one.",
    UNKNOWN_MODEL: "Model not available on this account: call list_models and pick one.",
    AUTH_FAILED: "Guided login failed: check details/message (Firefox installed? GIA_FIREFOX_PATH).",
    UPSTREAM_ERROR: "Unclassified Gemini error: retry once later; if it persists, check auth_status.",
    QUOTA_GUARD: "Quota guard stopped the call before reaching Google's limit: wait until details.resets_at.",
    PATH_NOT_ALLOWED: "copy_to must be inside output_root or the working directory.",
    INVALID_ASPECT: "Use one of details.valid for aspect_hint.",
    INVALID_COLLECTION: "Collection name is empty or already exists: pick another name or use the existing one.",
    UNKNOWN_COLLECTION: "Unknown collection: call list_collections and use one of those ids or names.",
    INVALID_STYLE: "Fix the style: it needs a name, an image upload must be png/jpeg/webp/gif under 20 MB, and built-in styles cannot be deleted.",
    INVALID_PROMPT: "Fix the prompt: name (max 80 chars, unique ignoring case) and text (max 4000 chars) must be non-empty, and at most 200 prompts can be stored; see message.",
    UNKNOWN_PROMPT: "Unknown prompt: call list_prompts and use one of those ids or names.",
    INVALID_QUERY: "Fix the arguments: see message (e.g. since must be an ISO date/time such as 2026-11-09; limit must be a positive integer).",
    INVALID_ANIMATION: "Check the grid (cols×rows, 2–36 frames), mode (sprite/scene), key color (green/magenta), tolerance (5–80), fps (2–30) and loop (loop/pingpong).",
    UNKNOWN_ANIMATION: "This image has no animation; create one with set_animation (MCP) or “Convertir en animación” (Musa).",
    INVALID_COMPOSITION: "Check the composition data (name ≤80, prompt ≤4000, 1–6 unique pieces, ≤3 refs, layer order with the background first) and that the layer is not busy and its images still exist.",
    UNKNOWN_COMPOSITION: "List compositions with list_compositions (MCP) or GET /api/compositions.",
}


class GIAError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def error_payload(e: "GIAError") -> dict:
    return {"status": "error", "code": e.code, "message": str(e), "details": e.details,
            "retryable": e.code in RETRYABLE,
            "next_action": NEXT_ACTION.get(e.code, NEXT_ACTION[UPSTREAM_ERROR])}
