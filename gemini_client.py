import os
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

from google import genai
from google.genai import types

_GEMINI_CLIENT: Optional[Any] = None
_GEMINI_CLIENTS: list[Any] = []
_GEMINI_CLIENT_INDEX = 0
_GEMINI_DISABLED = False


def _normalize_key_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _read_api_keys_from_file() -> list[str]:
    keys: list[str] = []
    candidates = []
    current_dir = os.path.abspath(os.getcwd())
    project_dir = os.path.abspath(os.path.dirname(__file__))

    for base_dir in [current_dir, project_dir]:
        current = base_dir
        while True:
            candidates.append(os.path.join(current, ".streamlit", "secrets.toml"))
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            for key_name in ("GEMINI_API_KEYS", "GOOGLE_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
                value = data.get(key_name)
                keys.extend(_normalize_key_list(value))
                if keys:
                    return keys
        except Exception:
            continue
    return keys


def _read_api_keys_from_env() -> list[str]:
    keys: list[str] = []
    for key_name in ("GEMINI_API_KEYS", "GOOGLE_API_KEYS"):
        keys.extend(_normalize_key_list(os.getenv(key_name)))
    if not keys:
        single_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if isinstance(single_key, str) and single_key.strip():
            keys.append(single_key.strip())
    return keys


def _read_api_keys_from_streamlit() -> list[str]:
    if st is None:
        return []
    try:
        from streamlit.runtime.secrets import Secrets
        if isinstance(st.secrets, Secrets):
            keys: list[str] = []
            for key_name in ("GEMINI_API_KEYS", "GOOGLE_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
                value = st.secrets.get(key_name)
                keys.extend(_normalize_key_list(value))
                if keys:
                    return keys
    except Exception:
        return []
    return []


def _get_api_keys() -> list[str]:
    keys = _read_api_keys_from_env()
    if not keys:
        keys = _read_api_keys_from_streamlit()
    if not keys:
        keys = _read_api_keys_from_file()
    return keys


def _disable_gemini() -> None:
    global _GEMINI_DISABLED
    _GEMINI_DISABLED = True
    print("[provider] gemini: temporarily disabling Gemini after repeated quota/rate-limit errors")


def _rotate_gemini_client() -> Any:
    global _GEMINI_CLIENT_INDEX, _GEMINI_CLIENT, _GEMINI_CLIENTS
    if len(_GEMINI_CLIENTS) < 2:
        return _GEMINI_CLIENT
    _GEMINI_CLIENT_INDEX = (_GEMINI_CLIENT_INDEX + 1) % len(_GEMINI_CLIENTS)
    _GEMINI_CLIENT = _GEMINI_CLIENTS[_GEMINI_CLIENT_INDEX]
    return _GEMINI_CLIENT


def _is_quota_error(exc: Exception) -> bool:
    message = str(exc).lower()
    quota_markers = (
        "quota",
        "rate limit",
        "rate_limit",
        "429",
        "too many requests",
        "resource exhausted",
        "limit exceeded",
        "exceeded",
        "allocation",
        "quota exceeded",
    )
    return any(marker in message for marker in quota_markers)


def get_gemini_client() -> Any:
    global _GEMINI_CLIENT, _GEMINI_CLIENTS, _GEMINI_CLIENT_INDEX
    if _GEMINI_DISABLED:
        raise RuntimeError("Gemini is temporarily disabled after repeated quota/rate-limit failures")

    if _GEMINI_CLIENTS:
        _GEMINI_CLIENT = _GEMINI_CLIENTS[_GEMINI_CLIENT_INDEX]
        return _GEMINI_CLIENT

    api_keys = _get_api_keys()
    if not api_keys:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set")

    _GEMINI_CLIENTS = [genai.Client(api_key=api_key) for api_key in api_keys]
    _GEMINI_CLIENT_INDEX = 0
    _GEMINI_CLIENT = _GEMINI_CLIENTS[0]
    return _GEMINI_CLIENT


def _retry_with_next_key(callable_obj: Any, action_name: str) -> Any:
    attempts = 0
    max_attempts = max(1, len(_GEMINI_CLIENTS))

    while attempts < max_attempts:
        try:
            return callable_obj()
        except Exception as exc:
            if not _is_quota_error(exc):
                raise

            attempts += 1
            if attempts >= max_attempts:
                print(f"[provider] gemini: quota/rate limit hit on {action_name} after exhausting available keys")
                _disable_gemini()
                raise

            print(f"[provider] gemini: quota/rate limit hit on {action_name}, rotating key")
            _rotate_gemini_client()

    raise RuntimeError(f"Gemini {action_name} could not complete after quota/rate-limit retries")


def _log_provider(provider: str, action: str) -> None:
    print(f"[provider] {provider}: {action}")


def upload_pdf(pdf_path: str) -> Any:
    _log_provider("gemini", f"uploading {pdf_path}")

    def _do_upload() -> Any:
        client = get_gemini_client()
        return client.files.upload(file=pdf_path)

    try:
        return _retry_with_next_key(_do_upload, "upload")
    except Exception as exc:
        raise RuntimeError(f"Gemini upload failed: {exc}") from exc


def generate_structured(contents: Any, response_schema: Any) -> Any:
    _log_provider("gemini", "generating structured response")

    def _do_generate() -> Any:
        client = get_gemini_client()
        return client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )

    try:
        return _retry_with_next_key(_do_generate, "generate")
    except Exception as exc:
        raise RuntimeError(f"Gemini generation failed: {exc}") from exc


def chat_with_gemini(prompt: str, system_prompt: Optional[str] = None) -> str:
    _log_provider("gemini", "generating text response with gemini-2.0-flash")
    contents = []
    if system_prompt:
        contents.append(f"System instruction: {system_prompt}")
    contents.append(prompt)

    def _do_generate() -> Any:
        client = get_gemini_client()
        res = client.models.generate_content(
            model="gemini-2.0-flash",
            contents="\n\n".join(contents),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        return res.text or ""

    try:
        return _retry_with_next_key(_do_generate, "text_generate")
    except Exception as exc:
        print(f"[provider] gemini generation failed: {exc}")
        return ""

