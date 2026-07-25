import json
import os
import re
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator
from groq_client import chat_with_groq


class QuestionRecord(BaseModel):
    number: str = Field(description="Question number or sub-question label in the paper, such as 1(a)")
    text: str = Field(description="Question stem or prompt")
    options: List[str] = Field(default_factory=list, description="Answer options if present")
    year: Optional[int] = Field(default=None, description="Year of the paper if inferable")
    source_page: Optional[int] = Field(default=None, description="Likely page if inferable")
    marks: Optional[float] = Field(default=None, description="Marks allocated to the question, if stated")
    course_outcome: Optional[int] = Field(default=None, description="CO number printed beside the question, such as CO1")

    @field_validator("number", mode="before")
    @classmethod
    def normalize_number(cls, value: Any) -> str:
        return str(value).strip() if value is not None else ""

    @field_validator("year", mode="before")
    @classmethod
    def normalize_year(cls, value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        match = re.search(r"(?:19|20)\d{2}", str(value))
        return int(match.group(0)) if match else None

    @field_validator("marks", mode="before")
    @classmethod
    def normalize_marks(cls, value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value)
        # Exam papers commonly write marks as [2+1=3] or [2x2=4].
        result_match = re.search(r"=\s*(\d+(?:\.\d+)?)", text)
        if result_match:
            return float(result_match.group(1))
        mark_match = re.search(r"\d+(?:\.\d+)?", text)
        return float(mark_match.group(0)) if mark_match else None

    @field_validator("course_outcome", mode="before")
    @classmethod
    def normalize_course_outcome(cls, value: Any) -> Optional[int]:
        match = re.search(r"(?:co\s*)?(\d+)", str(value or ""), re.IGNORECASE)
        return int(match.group(1)) if match else None


class QuestionBatch(BaseModel):
    questions: List[QuestionRecord] = Field(description="Structured questions extracted from the PDF")


def _parse_json_payload(text: str) -> Optional[Any]:
    if not text or not isinstance(text, str):
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Extract substring between first '{' and last '}'
    start_brace = text.find("{")
    end_brace = text.rfind("}")
    if start_brace != -1:
        if end_brace != -1 and end_brace > start_brace:
            candidate = text[start_brace:end_brace + 1]
        else:
            candidate = text[start_brace:]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Auto-repair truncated JSON arrays inside objects
            for suffix in ["\"}]}", "}]}", "}]", "}", "]"]:
                try:
                    return json.loads(candidate + suffix)
                except json.JSONDecodeError:
                    continue

    # Extract substring between first '[' and last ']'
    start_bracket = text.find("[")
    end_bracket = text.rfind("]")
    if start_bracket != -1:
        if end_bracket != -1 and end_bracket > start_bracket:
            candidate = text[start_bracket:end_bracket + 1]
        else:
            candidate = text[start_bracket:]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            for suffix in ["\"}]", "}]", "]"]:
                try:
                    return json.loads(candidate + suffix)
                except json.JSONDecodeError:
                    continue

    return None





def _normalize_question_item(item: dict) -> dict:
    normalized = dict(item)
    number = normalized.get("number")
    if number is not None:
        normalized["number"] = str(number).strip()
    if normalized.get("options") is None:
        normalized["options"] = []
    elif isinstance(normalized.get("options"), str):
        normalized["options"] = [normalized["options"]]
    elif not isinstance(normalized.get("options"), list):
        normalized["options"] = []
    if "year" in normalized and normalized["year"] is not None:
        if isinstance(normalized["year"], str):
            digits = re.findall(r"\d+", normalized["year"])
            normalized["year"] = int(digits[0]) if digits else None
        elif not isinstance(normalized["year"], int):
            normalized["year"] = None
    if "source_page" in normalized and normalized["source_page"] is not None:
        if isinstance(normalized["source_page"], str):
            digits = re.findall(r"\d+", normalized["source_page"])
            normalized["source_page"] = int(digits[0]) if digits else None
        elif not isinstance(normalized["source_page"], int):
            normalized["source_page"] = None
    
    # Normalize or infer marks
    marks_val = None
    if "marks" in normalized and normalized["marks"] is not None:
        try:
            marks_val = QuestionRecord.normalize_marks(normalized["marks"])
        except (TypeError, ValueError):
            marks_val = None
    if marks_val is None or marks_val <= 0:
        marks_val = _infer_marks(str(normalized.get("text", ""))) or 4.0
    normalized["marks"] = marks_val

    normalized["course_outcome"] = normalized.get("course_outcome", normalized.get("co"))
    return normalized


def _infer_marks(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = text.strip()

    # 1. Multiplication mark syntax: [2 x 4 marks] or [3 * 2 M]
    mult_match = re.search(r"[\[\(]\s*(\d+)\s*[x\*]\s*(\d+(?:\.\d+)?)\s*(?:marks?|m)?\s*[\]\)]", cleaned, re.IGNORECASE)
    if mult_match:
        return float(int(mult_match.group(1)) * float(mult_match.group(2)))

    # 2. Equation mark syntax: [2+3=5] or (4+4=8)
    eq_match = re.search(r"[\[\(]\s*\d+\s*[\+\-]\s*\d+\s*=\s*(\d+(?:\.\d+)?)\s*(?:marks?|m)?\s*[\]\)]", cleaned, re.IGNORECASE)
    if eq_match:
        return float(eq_match.group(1))

    # 3. Explicit bracketed mark indicators: [07], [7], (8M), [Marks: 6], (7 marks), [4M], CO1 [4]
    bracket_match = re.search(r"[\[\(]\s*(?:marks?\s*:?\s*)?(\d+(?:\.\d+)?)\s*(?:marks?|m)?\s*[\]\)]", cleaned, re.IGNORECASE)
    if bracket_match:
        val = float(bracket_match.group(1))
        if 0 < val <= 50:
            return val

    # 4. Explicit keyword attached at the very end of question or in brackets: 6 marks, 10M
    kw_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:marks|mark|M)\b", cleaned, re.IGNORECASE)
    if kw_match:
        val = float(kw_match.group(1))
        after_str = cleaned[kw_match.end():kw_match.end()+5].lower()
        if not any(after_str.startswith(u) for u in ("/", "^", "3", "2", "s", "m", "g", "k", "pa", "bar")):
            if 0 < val <= 50:
                return val

    return None


def _is_plausible_question(question: QuestionRecord) -> bool:
    """Reject OCR headers, page artifacts, and malformed model records before analysis."""
    if not re.search(r"\d", question.number or ""):
        return False
    if len((question.text or "").strip()) < 12:
        return False
    if question.marks is not None and not 0 < question.marks <= 100:
        return False
    return True


def _fallback_questions(text: str, year: Optional[int] = None) -> List[QuestionRecord]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    questions = []
    
    question_patterns = [
        r'^\d+\s*(?:[.)]|\([a-zivx]+\))',
        r'^[Qq](?:uestion)?\s*\d+',
    ]
    
    for line in lines:
        is_question = any(re.match(pattern, line, re.IGNORECASE) for pattern in question_patterns)
        if is_question:
            questions.append(
                QuestionRecord(
                    number=str(len(questions) + 1),
                    text=line,
                    options=[],
                    year=year,
                    source_page=None,
                    marks=_infer_marks(line),
                )
            )
    
    if not questions and text.strip():
        sentences = [s.strip() for s in re.split(r'[.!?]+', text) if len(s.strip()) > 20]
        if sentences:
            for idx, sentence in enumerate(sentences[:10], start=1):
                questions.append(
                    QuestionRecord(
                        number=str(idx),
                        text=sentence[:300],
                        options=[],
                        year=year,
                        source_page=None,
                        marks=_infer_marks(sentence),
                    )
                )
        else:
            questions.append(
                QuestionRecord(
                    number="1",
                    text=text[:500],
                    options=[],
                    year=year,
                    source_page=None,
                    marks=_infer_marks(text),
                )
            )
    
    return [question for question in questions if _is_plausible_question(question)]


def _split_text_for_groq(text: str, chunk_size: int = 3500) -> List[str]:
    """Split long OCR output on line boundaries so every paper can be processed by Groq."""
    lines = [line for line in text.splitlines() if line.strip()]
    chunks: List[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}".strip()
        if current and len(candidate) > chunk_size:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text[:chunk_size]]


def _clean_ocr_header_noise(text: str) -> str:
    if not text:
        return ""
    header_patterns = [
        r"^\s*page\s+\d+\s+(?:of|/)\s+\d+",
        r"^\s*total\s+(?:no\.?\s+of\s+)?pages?\s*:\s*\d+",
        r"^\s*code\s*(?:no\.?|code)?\s*:\s*[\w\d-]+",
        r"^\s*seat\s+no\.?\s*:\s*",
        r"^\s*roll\s+no\.?\s*:\s*",
        r"^\s*b\.?\s*tech\s+.*examination",
        r"^\s*answer\s+any\s+(?:five|all|\d+)\s+questions",
        r"^\s*time\s*:\s*\d+\s*hours?",
        r"^\s*max(?:\.|\s+)?marks?\s*:\s*\d+",
    ]
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append(line)
            continue
        if any(re.search(pat, stripped, re.IGNORECASE) for pat in header_patterns):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def parse_questions_from_text(text: str, year: Optional[int] = None, pdf_path: Optional[str] = None) -> List[QuestionRecord]:
    if not isinstance(text, str) or not text.strip():
        return []

    cleaned_text = _clean_ocr_header_noise(text)
    questions: List[QuestionRecord] = []
    system_prompt = (
        "You extract exam questions from OCR text with exact sub-question granularity. Return JSON only with a 'questions' array. "
        "Each item must have number, text, options, year, source_page, marks, and course_outcome. "
        "MUST extract individual sub-questions down to exact sub-parts (such as 1(a), 1(b.i), 2(d.iii)) rather than merging them into broad parent blocks. "
        "Each sub-question must have its specific allocated marks (e.g. 1M, 4M, 7M). "
        "Include course_outcome as the numeric CO code printed beside it (e.g. CO1 becomes 1). "
        "Extract only actual question prompts; never create entries for headers, instructions, or page numbers. "
        "Parse bracketed marks such as [4] CO1 as marks=4."
    )
    for chunk in _split_text_for_groq(cleaned_text):
        try:
            response = chat_with_groq(
                f"Paper year: {year or 'unknown'}.\n\nOCR text:\n{chunk}",
                system_prompt=system_prompt,
            )
            parsed = _parse_json_payload(response)
            raw_items = []
            if isinstance(parsed, list):
                raw_items = parsed
            elif isinstance(parsed, dict):
                raw_items = (
                    parsed.get("questions")
                    or parsed.get("sub_questions")
                    or parsed.get("extracted_questions")
                    or parsed.get("items")
                    or parsed.get("data")
                    or parsed.get("results")
                    or []
                )

            if isinstance(raw_items, list) and raw_items:
                batch = QuestionBatch(questions=[_normalize_question_item(item) for item in raw_items if isinstance(item, dict)])
                questions.extend(question for question in batch.questions if _is_plausible_question(question))
        except Exception as exc:
            print(f"[question_parser] LLM question extraction failed for chunk: {exc}")

    if questions:
        print(f"[question_parser] LLM extracted {len(questions)} valid question(s)")
        return questions
    
    print("[question_parser] LLM extraction produced no questions; attempting fallback regex extraction")
    fallback = _fallback_questions(cleaned_text, year=year)
    if fallback:
        print(f"[question_parser] Fallback regex extracted {len(fallback)} question(s)")
        return fallback

    raise RuntimeError("No valid structured questions could be extracted from paper text.")
