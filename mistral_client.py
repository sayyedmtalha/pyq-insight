"""Dependency-free client for Mistral AI Chat Completions API."""

import json
import os
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL = "mistral-small-latest"


def _api_key() -> str:
    # 1. Environment variable
    value = os.getenv("MISTRAL_API_KEY", "").strip()
    if value:
        return value

    # 2. Streamlit Cloud secrets (st.secrets)
    if st is not None:
        try:
            from streamlit.runtime.secrets import Secrets
            if isinstance(st.secrets, Secrets):
                v = st.secrets.get("MISTRAL_API_KEY")
                if isinstance(v, str) and v.strip():
                    return v.strip()
        except Exception:
            pass

    # 3. Local secrets.toml file (dev only)
    if tomllib is not None:
        for base_dir in (Path.cwd(), Path(__file__).resolve().parent):
            secrets_path = base_dir / ".streamlit" / "secrets.toml"
            if not secrets_path.exists():
                continue
            try:
                with secrets_path.open("rb") as file_handle:
                    value = str(tomllib.load(file_handle).get("MISTRAL_API_KEY", "")).strip()
                if value:
                    return value
            except (OSError, ValueError):
                continue
    return ""


def is_configured() -> bool:
    return bool(_api_key())


def chat_with_mistral(prompt: str, system_prompt: Optional[str] = None, temperature: float = 0.2) -> str:
    """Send chat completion request to Mistral AI API."""
    api_key = _api_key()
    if not api_key:
        return ""

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }

    request = Request(
        MISTRAL_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    print(f"[provider] mistral: generating text response with {DEFAULT_MODEL}")
    try:
        with urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
        return str(result["choices"][0]["message"]["content"] or "")
    except (HTTPError, URLError, json.JSONDecodeError, KeyError, OSError) as exc:
        print(f"[provider] mistral request failed: {exc}")
        return ""
