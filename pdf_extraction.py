import os
from typing import List, Dict, Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from mistral_ocr_client import MistralOCRError, extract_pdf_text

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
except ImportError:  # pragma: no cover
    pytesseract = None

try:
    import fitz
except ImportError:  # pragma: no cover
    fitz = None


class PDFExtractionError(Exception):
    pass


def _run_tesseract_ocr(pdf_path: str) -> str:
    if pytesseract is None or fitz is None:
        return ""

    try:
        doc = fitz.open(pdf_path)
        text_chunks = []
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            image_bytes = pix.tobytes("png")
            image_path = f"{pdf_path}_{page_num}.png"
            with open(image_path, "wb") as fh:
                fh.write(image_bytes)
            page_text = (pytesseract.image_to_string(image_path) or "").strip()
            text_chunks.append(page_text)
            os.remove(image_path)
        return "\n\n".join(text_chunks)
    except Exception as exc:
        print(f"[pdf_extraction] Tesseract OCR failed: {exc}")
        return ""


import threading

_mistral_ocr_lock = threading.Lock()


def _run_mistral_ocr(pdf_path: str) -> str:
    with _mistral_ocr_lock:
        try:
            print(f"[provider] mistral-ocr: extracting {os.path.basename(pdf_path)}")
            return extract_pdf_text(pdf_path)
        except MistralOCRError as exc:
            print(f"[pdf_extraction] Mistral OCR unavailable: {exc}")
            return ""


def extract_text_from_pdf(pdf_path: str, force_ocr: bool = False) -> str:
    combined_text = ""
    try:
        reader = PdfReader(pdf_path)
        text_chunks = []
        for page in reader.pages:
            page_text = (page.extract_text() or "").strip()
            text_chunks.append(page_text)
        combined_text = "\n\n".join(text_chunks)
        # If native PDF text is rich (>100 chars), use it directly unless force_ocr is requested
        if len(combined_text.strip()) > 100 and not force_ocr:
            return combined_text
    except Exception:
        combined_text = ""

    # Attempt Mistral OCR (serialized via lock to prevent Mistral API concurrency limits)
    mistral_text = _run_mistral_ocr(pdf_path)
    if mistral_text.strip():
        return mistral_text

    # Attempt Tesseract OCR
    tesseract_text = _run_tesseract_ocr(pdf_path)
    if tesseract_text.strip():
        return tesseract_text

    # Final Safety Fallback: Return native extracted text if available
    return combined_text


def extract_pdf_texts(pdf_paths: List[str], force_ocr: bool = False) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for pdf_path in pdf_paths:
        text = extract_text_from_pdf(pdf_path, force_ocr=force_ocr)
        results.append({"path": pdf_path, "file": None, "text": text})
    return results

