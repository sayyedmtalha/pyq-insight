import os
import re
from typing import List, Dict, Any, Optional
from pdf_extraction import extract_pdf_texts
from question_parser import parse_questions_from_text
from classifier import TopicTaxonomy, build_taxonomy_from_syllabus, classify_questions
from aggregator import deduplicate_records, build_topic_year_pivot, build_gap_analysis, build_trends
from excel_builder import build_workbook


def _infer_academic_year_from_filename(path: str) -> Optional[int]:
    name = os.path.basename(path)
    four_digit = re.search(r"(?:19|20)\d{2}", name)
    if four_digit:
        return int(four_digit.group(0))
    two_digit_range = re.search(r"(?<!\d)(\d{2})\s*[-_]\s*\d{2}(?!\d)", name)
    if two_digit_range:
        return 2000 + int(two_digit_range.group(1))
    return None


def _infer_academic_year_from_paper_heading(text: str) -> Optional[int]:
    """Use the printed examination year; filenames are only a last-resort fallback."""
    if not text:
        return None
    range_match = re.search(r"(?<!\d)((?:19|20)\d{2})\s*[-–/]\s*(?:\d{2}|(?:19|20)\d{2})(?!\d)", text)
    if range_match:
        return int(range_match.group(1))
    
    single_match = re.search(r"\b((?:19|20)[2-9]\d)\b", text)
    if single_match:
        return int(single_match.group(1))
    return None


from groq_client import reset_groq_status


def run_pipeline(pdf_paths: List[str], syllabus_text: str, output_path: str, years: Optional[List[int]] = None, force_ocr: bool = False) -> str:
    reset_groq_status()
    extracted = extract_pdf_texts(pdf_paths, force_ocr=force_ocr)
    all_questions: List[Dict[str, Any]] = []
    paper_year_map: Dict[str, Optional[int]] = {}

    taxonomy = TopicTaxonomy(topics=[], subtopics={})
    try:
        taxonomy = build_taxonomy_from_syllabus(syllabus_text)
    except Exception:
        taxonomy = TopicTaxonomy(topics=[], subtopics={})
    print(f"[pipeline] Loaded {len(taxonomy.topics)} syllabus Unit/topic group(s)")

    # Pre-infer year for all papers to allow interpolation if a paper heading lacks a year
    detected_years: List[Optional[int]] = []
    for index, item in enumerate(extracted):
        text = item.get("text", "")
        pdf_path = item.get("path") or None
        explicit_year = years[index] if years and index < len(years) else None
        filename_year = _infer_academic_year_from_filename(pdf_path) if pdf_path else None
        heading_year = _infer_academic_year_from_paper_heading(text)
        detected_years.append(explicit_year or heading_year or filename_year)

    valid_years = [y for y in detected_years if y is not None]
    base_year = min(valid_years) if valid_years else 2022
    resolved_years: List[int] = []
    for idx, yr in enumerate(detected_years):
        resolved_years.append(yr if yr is not None else base_year + idx)

    for index, item in enumerate(extracted):
        text = item.get("text", "")
        pdf_path = item.get("path") or None
        paper_name = os.path.basename(pdf_path or f"paper_{index + 1}.pdf")
        paper_year = resolved_years[index]
        paper_year_map[paper_name] = paper_year

        print(f"[pipeline] Assigned academic year {paper_year}-{str(paper_year + 1)[-2:]} for '{paper_name}'")

        try:
            if pdf_path and os.path.exists(pdf_path):
                questions = parse_questions_from_text(text, year=paper_year, pdf_path=pdf_path)
            else:
                questions = parse_questions_from_text(text, year=paper_year)
        except Exception as exc:
            raise RuntimeError(f"Question extraction failed for {paper_name}: {exc}") from exc

        if not questions:
            raise RuntimeError(f"Complete Paper Parsing Failure: 0 questions extracted from '{paper_name}'. Verify PDF OCR content.")

        paper_marks = sum(float(q.marks or 0) for q in questions)
        print(f"[pipeline] Extracted {len(questions)} sub-questions from '{paper_name}' (Total Marks = {paper_marks:g}M)")

        for question in questions:
            all_questions.append({
                "question_number": question.number,
                "text": question.text,
                "year": question.year or paper_year,
                "source_page": question.source_page,
                "marks": question.marks if (question.marks and question.marks > 0) else 4.0,
                "course_outcome": question.course_outcome,
                "paper_name": paper_name,
            })

    print(f"[pipeline] Prepared {len(all_questions)} question(s) for classification")

    try:
        classifications = classify_questions(all_questions, taxonomy)
    except Exception as exc:
        raise RuntimeError(f"Question classification failed: {exc}") from exc

    raw_records: List[Dict[str, Any]] = []
    for classification in classifications:
        raw_records.append({
            "question_number": classification.question_number,
            "text": classification.text,
            "topic": classification.topic,
            "subtopic": classification.subtopic,
            "difficulty": classification.difficulty,
            "question_type": classification.question_type,
            "confidence": classification.confidence,
            "manual_review": classification.manual_review,
            "year": classification.year,
            "marks": classification.marks,
            "source_page": classification.source_page,
            "unit": classification.unit,
            "course_outcome": classification.course_outcome,
        })

    raw_records = deduplicate_records(raw_records)

    invalid_topic_markers = ("department", "university", "college", "course code", "syllabus")
    invalid_topics = [
        record for record in raw_records
        if any(marker in str(record.get("topic", "")).lower() for marker in invalid_topic_markers)
    ]
    if invalid_topics:
        print(f"[pipeline] Replaced {len(invalid_topics)} institutional-header classification(s) with Unclassified")
        for record in invalid_topics:
            record["topic"] = "Unclassified"
            record["subtopic"] = "Unclassified"
            record["unit"] = "Unit I - CO1"

    if not raw_records:
        raise RuntimeError("No valid classified questions were produced. No workbook was created.")

    extracted_years = {record.get("year") for record in raw_records if record.get("year")}
    for paper_name, year in paper_year_map.items():
        if year and year not in extracted_years:
            print(f"[pipeline] Warning: Year {year} from '{paper_name}' has 0 classified questions after deduplication (possible duplicate paper or aggressive deduplication threshold).")

    grand_total_marks = sum(float(r.get("marks") or 0) for r in raw_records)
    print(f"[pipeline] Successfully parsed dataset across {len(pdf_paths)} paper(s). Grand Total Marks = {grand_total_marks:g}M")

    topic_pivot = build_topic_year_pivot(raw_records)
    gaps = build_gap_analysis(raw_records, taxonomy.topics)
    trends = build_trends(raw_records)
    workbook_path = build_workbook(output_path, raw_records, topic_pivot, gaps, trends, extracted_texts=extracted, taxonomy=taxonomy)
    return workbook_path
