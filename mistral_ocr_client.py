"""Small, dependency-free client for Mistral's OCR API."""

import base64
import json
import os
from pathlib import Path
from typing import Any
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


MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"


class MistralOCRError(RuntimeError):
    """Raised when Mistral OCR is unavailable or rejects a document."""


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


def extract_pdf_text(pdf_path: str, timeout_seconds: int = 90) -> str:
    """Return page markdown from a PDF through Mistral OCR."""
    api_key = _api_key()
    if not api_key:
        raise MistralOCRError("MISTRAL_API_KEY is not configured")

    try:
        encoded_pdf = base64.b64encode(Path(pdf_path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise MistralOCRError(f"Could not read PDF for Mistral OCR: {exc}") from exc

    payload = {
        "model": "mistral-ocr-latest",
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{encoded_pdf}",
        },
        "include_image_base64": False,
    }
    request = Request(
        MISTRAL_OCR_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            pages = result.get("pages", [])
            text = "\n\n".join(str(page.get("markdown", "")).strip() for page in pages if isinstance(page, dict))
            if text.strip():
                return text
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MistralOCRError(f"Mistral OCR returned HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 1:
                raise MistralOCRError(f"Mistral OCR request failed after retries: {exc}") from exc
    raise MistralOCRError("Mistral OCR returned no readable text")

