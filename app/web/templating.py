"""Sub-path helper: the Agent Hub serves the app under /<slug>/ with
strip_prefix, so the app sees "/" but browser URLs must carry the prefix.
root_path (set on the FastAPI app) holds that prefix; join_base prepends it."""


def join_base(root_path: str, path: str) -> str:
    base = (root_path or "").rstrip("/")
    suffix = path if path.startswith("/") else "/" + path
    return f"{base}{suffix}"
