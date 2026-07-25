import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.getcwd())

import pdf_extraction


class PDFOCRFallbackTests(unittest.TestCase):
    def test_extract_text_from_pdf_prefers_mistral_ocr_for_scanned_papers(self):
        class FakePage:
            def extract_text(self):
                return None

        class FakeReader:
            pages = [FakePage()]

        with patch.object(pdf_extraction, "PdfReader", return_value=FakeReader()):
            with patch.object(pdf_extraction, "_run_mistral_ocr", return_value="Mistral question text"):
                with patch.object(pdf_extraction, "_run_tesseract_ocr") as mock_tesseract:
                    result = pdf_extraction.extract_text_from_pdf("sample.pdf", force_ocr=True)

        self.assertEqual(result, "Mistral question text")
        mock_tesseract.assert_not_called()

    def test_extract_text_from_pdf_uses_tesseract_fallback_when_mistral_is_unavailable(self):
        class FakePage:
            def extract_text(self):
                return None

        class FakeReader:
            pages = [FakePage()]

        with patch.object(pdf_extraction, "PdfReader", return_value=FakeReader()):
            with patch.object(pdf_extraction, "_run_mistral_ocr", return_value=""):
                with patch.object(pdf_extraction, "_run_tesseract_ocr", return_value="fallback text") as mock_tesseract:
                    result = pdf_extraction.extract_text_from_pdf("sample.pdf", force_ocr=True)

        self.assertEqual(result, "fallback text")
        mock_tesseract.assert_called_once_with("sample.pdf")


if __name__ == "__main__":
    unittest.main()
