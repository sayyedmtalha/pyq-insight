import os
import time
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

from groq import Groq
from mistral_client import chat_with_mistral, is_configured as mistral_is_configured


def _read_api_key_from_file() -> str | None:
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
            value = data.get("GROQ_API_KEY")
            if isinstance(value, str) and value.strip():
                return value.strip()
        except Exception:
            continue
    return None


def get_groq_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY")

    if not api_key and st is not None:
        try:
            from streamlit.runtime.secrets import Secrets
            if isinstance(st.secrets, Secrets):
                api_key = st.secrets.get("GROQ_API_KEY")
        except Exception:
            api_key = None

    if not api_key:
        api_key = _read_api_key_from_file()

    if not api_key:
        raise RuntimeError("GROQ_API_KEY environment variable is not set")
    return Groq(api_key=api_key)


_GROQ_DISABLED = False


def reset_groq_status() -> None:
    global _GROQ_DISABLED
    _GROQ_DISABLED = False


def chat_with_groq(prompt: str, system_prompt: Optional[str] = None, model: str = "llama-3.3-70b-versatile", temperature: float = 0.2) -> str:
    """Execute chat completion using 3-tier zero-downtime failover: Groq (LPU) -> Mistral -> Gemini 2.0 Flash."""
    global _GROQ_DISABLED

    # 1. Primary: Try Groq first if not previously disabled
    if not _GROQ_DISABLED:
        try:
            client = get_groq_client()
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            for attempt in range(2):
                try:
                    print(f"[provider] groq: generating text response with {model} (ultra-fast LPU)")
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        response_format={"type": "json_object"},
                    )
                    content = response.choices[0].message.content or ""
                    if content:
                        return content
                except Exception as exc:
                    if attempt == 1 or "rate_limit" in str(exc).lower() or "429" in str(exc):
                        print(f"[provider] groq rate-limited/failed ({exc}); disabling Groq for session and switching to Mistral")
                        _GROQ_DISABLED = True
                        break
                    else:
                        time.sleep(0.5)
        except Exception as exc:
            print(f"[provider] groq unavailable ({exc}); disabling Groq for session and switching to Mistral")
            _GROQ_DISABLED = True

    # 2. Secondary: Fallback to Mistral
    if mistral_is_configured():
        response = chat_with_mistral(prompt, system_prompt=system_prompt, temperature=temperature)
        if response:
            return response
        print("[provider] mistral: empty response; trying Gemini fallback")

    # 3. Tertiary: Fallback to Gemini 2.0 Flash
    try:
        from gemini_client import chat_with_gemini
        return chat_with_gemini(prompt, system_prompt=system_prompt)
    except Exception as exc:
        print(f"[provider] gemini fallback failed: {exc}")

    return ""
