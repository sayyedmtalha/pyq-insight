import json
import os
import re
from typing import Any, List, Dict, Optional
from pydantic import BaseModel, Field
from groq_client import chat_with_groq


class TopicTaxonomy(BaseModel):
    topics: List[str] = Field(description="Top-level fixed topics")
    subtopics: Dict[str, List[str]] = Field(description="Mapping of topic to its subtopics")


class QuestionClassification(BaseModel):
    topic: str = Field(description="Primary topic")
    subtopic: str = Field(description="Subtopic")
    difficulty: str = Field(description="Easy, Medium, Hard")
    question_type: str = Field(description="Short Answer, Essay, Calculation, MCQ, etc.")
    confidence: float = Field(description="Confidence score between 0 and 1")


class ClassifiedQuestion(BaseModel):
    question_number: str
    text: str
    topic: str
    subtopic: str
    difficulty: str
    question_type: str
    confidence: float
    manual_review: bool = Field(default=False)
    year: int | None = Field(default=None)
    marks: float | None = Field(default=None)
    source_page: int | None = Field(default=None)
    unit: str = Field(default="Unassigned")
    course_outcome: int | None = Field(default=None)


def _parse_json_object(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract substring between first '{' and last '}'
    start_brace = text.find("{")
    end_brace = text.rfind("}")
    if start_brace != -1 and end_brace != -1 and end_brace > start_brace:
        try:
            return json.loads(text[start_brace:end_brace + 1])
        except json.JSONDecodeError:
            pass

    # Extract substring between first '[' and last ']'
    start_bracket = text.find("[")
    end_bracket = text.rfind("]")
    if start_bracket != -1 and end_bracket != -1 and end_bracket > start_bracket:
        try:
            return json.loads(text[start_bracket:end_bracket + 1])
        except json.JSONDecodeError:
            pass

    return None


def _normalize_topics(raw_topics: Any) -> List[str]:
    if isinstance(raw_topics, str):
        return [raw_topics]
    if isinstance(raw_topics, list):
        normalized = []
        for item in raw_topics:
            if isinstance(item, str):
                normalized.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("title") or item.get("topic")
                if isinstance(name, str) and name.strip():
                    normalized.append(name.strip())
        return normalized
    return []


def _valid_topics(topics: List[str]) -> List[str]:
    """Do not allow single-character OCR artifacts to become workbook topics."""
    document_headers = {
        "department", "university", "college", "syllabus",
        "semester", "scheme", "regulation", "academic year", "course code",
    }
    valid = []
    for topic in topics:
        normalized = re.sub(r"\s+", " ", topic).strip()
        lowered = normalized.lower()
        if len(normalized) < 3 or len(re.findall(r"[A-Za-z]", normalized)) < 2:
            continue
        if any(header in lowered for header in document_headers):
            continue
        valid.append(normalized)
    return valid


def _match_taxonomy_topic(candidate: str, topics: List[str]) -> str | None:
    """Match only a meaningful overlap; never map every question to the first topic."""
    candidate_words = set(re.findall(r"[a-z]+", candidate.lower()))
    if not candidate_words:
        return None
    best_topic = None
    best_score = 0.0
    for topic in topics:
        topic_words = set(re.findall(r"[a-z]+", topic.lower()))
        if not topic_words:
            continue
        score = len(candidate_words & topic_words) / min(len(candidate_words), len(topic_words))
        if score > best_score:
            best_topic, best_score = topic, score
    return best_topic if best_score >= 0.6 else None


def _unit_from_topic(topic: str) -> str:
    match = re.search(r"\s*(Unit|Module)\s*([IVXLCDM]+|\d+)\b", topic, re.IGNORECASE)
    if match:
        unit_str = match.group(2).upper()
        roman_map = {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V", "6": "VI"}
        roman = roman_map.get(unit_str, unit_str)
        co_num = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}.get(roman, int(unit_str) if unit_str.isdigit() else 1)
        return f"Unit {roman} - CO{co_num}"
    return "Unit I - CO1"


def _match_subtopic(candidate: str, subtopics: List[str]) -> str:
    return _match_taxonomy_topic(candidate, subtopics) or "Unclassified"


def _resolve_syllabus_location(
    candidate_topic: str, 
    candidate_subtopic: str, 
    question_text: str, 
    taxonomy: TopicTaxonomy,
    target_unit: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve a returned topic/subtopic or question text to one exact syllabus Unit and subtopic, enforcing strict Unit/CO boundaries if specified."""
    stop_words = {
        "the", "and", "for", "from", "with", "under", "design", "of", "to", "a", "an", "in", "or", "is", "are", 
        "what", "how", "why", "state", "explain", "derive", "calculate", "find", "determine", "show", "that",
        "question", "marks", "unit", "co1", "co2", "co3", "co4", "co5"
    }

    # Filter search pool strictly if target_unit is declared (e.g. "Unit II - CO2")
    search_subtopics = taxonomy.subtopics
    if target_unit:
        target_unit_clean = _unit_from_topic(target_unit).lower()
        filtered = {
            unit_topic: subs 
            for unit_topic, subs in taxonomy.subtopics.items() 
            if _unit_from_topic(unit_topic).lower() == target_unit_clean
        }
        if filtered:
            search_subtopics = filtered

    topic = _match_taxonomy_topic(candidate_topic, list(search_subtopics.keys()))
    if topic:
        subtopic = _match_subtopic(candidate_subtopic, search_subtopics.get(topic, []))
        if subtopic != "Unclassified":
            return topic, subtopic
    
    probe_words = {word for word in re.findall(r"[a-z]{3,}", f"{candidate_topic} {candidate_subtopic} {question_text}".lower()) if word not in stop_words}
    if not probe_words:
        return "Unclassified", "Unclassified"

    best_topic, best_subtopic, best_score = None, None, 0.0
    for unit_topic, subtopics in search_subtopics.items():
        for subtopic in (subtopics or [unit_topic]):
            subtopic_words = {w for w in re.findall(r"[a-z]{3,}", f"{unit_topic} {subtopic}".lower()) if w not in stop_words}
            overlap = probe_words & subtopic_words
            if not overlap:
                continue
            score = len(overlap) * 2.0 + (len(overlap) / max(len(subtopic_words), 1))
            if score > best_score:
                best_topic, best_subtopic, best_score = unit_topic, subtopic, score

    if best_topic and best_score > 0:
        return best_topic, best_subtopic

    if target_unit and list(search_subtopics.keys()):
        first_topic = list(search_subtopics.keys())[0]
        first_subs = search_subtopics.get(first_topic, [first_topic])
        return first_topic, (first_subs[0] if first_subs else first_topic)

    return "Unclassified", "Unclassified"


def _normalize_subtopics(raw_subtopics: Any) -> Dict[str, List[str]]:
    if not isinstance(raw_subtopics, dict):
        return {}
    normalized: Dict[str, List[str]] = {}
    for key, value in raw_subtopics.items():
        if isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, str):
                    items.append(item)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("title") or item.get("subtopic")
                    if isinstance(name, str) and name.strip():
                        items.append(name.strip())
            normalized[str(key)] = items
        elif isinstance(value, str):
            normalized[str(key)] = [value]
    return normalized


def _taxonomy_from_unit_sections(syllabus_text: str) -> TopicTaxonomy | None:
    """Extract the common `Unit N: ...` syllabus structure without spending an LLM call."""
    pattern = re.compile(
        r"\b(Unit\s*(?:[IVX]+|\d+))\s*[:\-–]?\s*(.*?)(?=\bUnit\s*(?:[IVX]+|\d+)\b|\bBooks?\s*:|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    topics: List[str] = []
    subtopics: Dict[str, List[str]] = {}
    for match in pattern.finditer(syllabus_text):
        unit = re.sub(r"\s+", " ", match.group(1)).strip().title()
        content = re.sub(r"\s+", " ", match.group(2)).strip().rstrip(".")
        if not content:
            continue

        raw_items: List[str] = []
        chunks = re.split(r"[;\n\u2022\u25cf]+|(?<=\w)\.\s+(?=[A-Z])", content)
        for chunk in chunks:
            chunk_str = chunk.strip().rstrip(".")
            if not chunk_str:
                continue
            if "," in chunk_str and len(re.findall(r"[A-Za-z]", chunk_str)) > 15:
                parts = [re.sub(r"^(?:and|or)\s+", "", p.strip(), flags=re.IGNORECASE).strip() for p in chunk_str.split(",")]
                clean_parts = [p for p in parts if len(p) >= 3]
                if len(clean_parts) > 1:
                    raw_items.extend(clean_parts)
                    continue
            raw_items.append(chunk_str)

        items = raw_items or [content]
        topic_header = f"{unit} - {items[0]}"
        topics.append(topic_header)
        subtopics[topic_header] = items

    if not topics:
        syllabus_match = re.search(r"\bsyllabus\s*:\s*(.*?)(?=\bBooks?\s*:|\Z)", syllabus_text, re.IGNORECASE | re.DOTALL)
        if syllabus_match:
            content = re.sub(r"\s+", " ", syllabus_match.group(1)).strip().rstrip(".")
            if content:
                topic = f"Unit 1 - {content[:40]}"
                topics.append(topic)
                subtopics[topic] = [content]
    print(f"[classifier] Parsed {len(topics)} syllabus unit(s) with subtopics directly: {' | '.join(topics)}")
    return TopicTaxonomy(topics=topics, subtopics=subtopics) if topics else None


def build_taxonomy_from_syllabus(syllabus_text: str) -> TopicTaxonomy:
    raw_syllabus = syllabus_text or ""
    direct_taxonomy = _taxonomy_from_unit_sections(raw_syllabus)
    if direct_taxonomy is not None:
        return direct_taxonomy

    cleaned = re.sub(r"\s+", " ", raw_syllabus).strip()
    if not cleaned:
        return TopicTaxonomy(topics=[], subtopics={})

    try:
        system_prompt = (
            "You are a curriculum expert. Extract a clean, standardized syllabus taxonomy with 5 Units and ~18-25 concise sub-concepts in total. "
            "Separate theoretical concepts, derivations, and numerical application topics into distinct buckets where appropriate. "
            "Exclude institutional headers, department names, course codes, and book references. "
            "Keep Unit labels (Unit I, Unit II, etc.) in topic titles. "
            "Return valid JSON only with 'topics' and 'subtopics' keys."
        )
        prompt = (
            "Analyze the syllabus text and return a JSON taxonomy with ~18-25 clean concept subtopics distributed across Units.\n\n"
            f"Syllabus text:\n{cleaned[:14000]}"
        )
        response = chat_with_groq(prompt, system_prompt=system_prompt)
        payload = _parse_json_object(response)
        if isinstance(payload, dict):
            topics = _valid_topics(_normalize_topics(payload.get("topics")))
            subtopics = _normalize_subtopics(payload.get("subtopics"))
            if topics:
                print(f"[classifier] Groq extracted {len(topics)} syllabus topic(s): {' | '.join(topics[:6])}")
                return TopicTaxonomy(topics=topics, subtopics=subtopics)
    except Exception as exc:
        print(f"[classifier] Groq taxonomy fallback failed: {exc}")

    topics = []
    subtopics: Dict[str, List[str]] = {}
    for token in re.split(r"[.;,\n]+", cleaned):
        token = token.strip()
        if len(token) >= 3 and len(re.findall(r"[A-Za-z]", token)) >= 2:
            topic = token[:80]
            topics.append(topic)
            subtopics[topic] = []
    return TopicTaxonomy(topics=topics, subtopics=subtopics)


def _infer_difficulty(text: str, marks: float | None = None) -> str:
    lowered = text.lower()
    if any(k in lowered for k in ("define", "state", "list", "what is", "name the", "give two")):
        return "Easy"
    if any(k in lowered for k in ("design", "calculate", "derive", "prove", "evaluate", "optimize")):
        return "Hard" if (marks and marks >= 7) else "Medium"
    if marks and marks >= 10:
        return "Hard"
    if marks and marks <= 3:
        return "Easy"
    return "Medium"


def _infer_question_type(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("derive", "derivation", "prove", "expression for", "show that")):
        return "Derivation"
    if any(word in lowered for word in ("calculate", "determine", "find", "compute", "estimate", "numerical", "problem", "evaluate")):
        return "Numerical"
    return "Theoretical"


def _lookup_syllabus_item(
    item: dict | None, 
    candidate_lookup: Dict[str, dict], 
    question_text: str, 
    taxonomy: TopicTaxonomy, 
    q_index: int = 0, 
    total_questions: int = 1,
    target_unit: Optional[str] = None,
) -> dict:
    if item and candidate_lookup:
        sid_raw = str(item.get("syllabus_id", "")).strip()
        if sid_raw in candidate_lookup:
            res = candidate_lookup[sid_raw]
            if not target_unit or _unit_from_topic(res["unit_topic"]).lower() == _unit_from_topic(target_unit).lower():
                return res
        if sid_raw.upper() in candidate_lookup:
            res = candidate_lookup[sid_raw.upper()]
            if not target_unit or _unit_from_topic(res["unit_topic"]).lower() == _unit_from_topic(target_unit).lower():
                return res
        digits = re.findall(r"\d+", sid_raw)
        if digits:
            key = f"S{digits[0]}"
            if key in candidate_lookup:
                res = candidate_lookup[key]
                if not target_unit or _unit_from_topic(res["unit_topic"]).lower() == _unit_from_topic(target_unit).lower():
                    return res

    # Dynamic keyword matching with strict Unit/CO boundary constraint
    best_topic, best_subtopic = _resolve_syllabus_location("", "", question_text, taxonomy, target_unit=target_unit)
    if best_topic != "Unclassified":
        return {"unit_topic": best_topic, "subtopic": best_subtopic}

    # Proportional fallback by question position in paper within declared target_unit or across taxonomy
    if taxonomy.topics:
        if target_unit:
            unit_topics = [t for t in taxonomy.topics if _unit_from_topic(t).lower() == _unit_from_topic(target_unit).lower()]
            if unit_topics:
                topic = unit_topics[0]
                subtopics = taxonomy.subtopics.get(topic, [topic])
                return {"unit_topic": topic, "subtopic": subtopics[0] if subtopics else topic}

        ratio = q_index / max(total_questions, 1)
        unit_idx = min(int(ratio * len(taxonomy.topics)), len(taxonomy.topics) - 1)
        topic = taxonomy.topics[unit_idx]
        subtopics = taxonomy.subtopics.get(topic, [topic])
        return {"unit_topic": topic, "subtopic": subtopics[0] if subtopics else topic}

    if candidate_lookup:
        return list(candidate_lookup.values())[0]
    return {"unit_topic": "Unit I - General Concepts", "subtopic": "General Concepts"}


def classify_questions(questions: List[Dict[str, Any]], taxonomy: TopicTaxonomy) -> List[ClassifiedQuestion]:
    if not questions:
        return []

    candidate_lookup: Dict[str, dict] = {}
    candidate_lines: List[str] = []
    cand_idx = 1
    for topic in taxonomy.topics:
        subtopics = taxonomy.subtopics.get(topic, [])
        if subtopics:
            for subtopic in subtopics:
                key = f"S{cand_idx}"
                candidate_lookup[key] = {"unit_topic": topic, "subtopic": subtopic}
                candidate_lines.append(f"{key}: Unit='{topic}' | Subtopic='{subtopic}'")
                cand_idx += 1
        else:
            key = f"S{cand_idx}"
            candidate_lookup[key] = {"unit_topic": topic, "subtopic": topic}
            candidate_lines.append(f"{key}: Unit='{topic}' | Subtopic='{topic}'")
            cand_idx += 1

    # Send ALL questions to the LLM for smart semantic classification
    system_prompt = (
        "You are an expert university professor and engineering curriculum classifier. "
        "Classify each exam question strictly and accurately to its best matching syllabus candidate ID (e.g. S1, S2, S3...). "
        "RULES:\n"
        "1. Identify the core engineering concept being tested (e.g. Rankine cycle, Maxwell relation, Psychrometric process, Volumetric efficiency).\n"
        "2. If a unit_constraint is specified (e.g. 'Unit II - CO2'), map the question ONLY to candidate IDs belonging to that specific Unit.\n"
        "3. Determine question_type strictly: 'Theoretical' (definitions/explanations), 'Derivation' (mathematical proofs/formulas), or 'Numerical' (calculations/problems).\n"
        "4. Determine difficulty strictly: 'Easy' (simple recall <= 3M), 'Medium' (standard derivation/diagram 4-7M), or 'Hard' (multi-stage numerical >= 8M).\n"
        "Return valid JSON only with a 'classifications' array containing objects with 'id', 'syllabus_id', 'confidence', 'difficulty', and 'question_type'."
    )

    classified_items: Dict[str, Dict[str, Any]] = {}
    batch_size = 10
    for start in range(0, len(questions), batch_size):
        batch_questions = questions[start:start + batch_size]
        compact_questions = []
        for idx_offset, q in enumerate(batch_questions):
            q_idx = start + idx_offset
            co_val = q.get("course_outcome")
            unit_val = q.get("unit")
            target_unit = "Any"
            if co_val:
                co_match = re.search(r"\d+", str(co_val))
                if co_match:
                    co_num = int(co_match.group(0))
                    roman_map = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}
                    target_unit = f"Unit {roman_map.get(co_num, 'I')} - CO{co_num}"
            elif unit_val:
                target_unit = _unit_from_topic(str(unit_val))
            compact_questions.append({
                "id": f"q{q_idx}",
                "unit_constraint": target_unit,
                "marks": q.get("marks"),
                "question": q.get("text", "")[:400]
            })

        user_prompt = (
            f"Syllabus Candidates:\n" + "\n".join(candidate_lines) +
            f"\n\nQuestions to classify:\n" + json.dumps(compact_questions, indent=2)
        )
        try:
            response = chat_with_groq(user_prompt, system_prompt=system_prompt)
            payload = _parse_json_object(response)
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and item.get("id"):
                        classified_items[str(item["id"])] = item
            elif isinstance(payload, dict):
                # Handle key map format: {"q0": {"syllabus_id": "S1"}, ...}
                for k, v in payload.items():
                    if isinstance(v, dict) and ("syllabus_id" in v or "candidate_id" in v or "topic" in v):
                        item_obj = dict(v)
                        item_obj.setdefault("id", k)
                        classified_items[str(item_obj["id"])] = item_obj

                # Handle list properties
                for list_key in ("classifications", "items", "results", "questions", "output", "data"):
                    val = payload.get(list_key)
                    if isinstance(val, list) and val:
                        for item in val:
                            if isinstance(item, dict) and item.get("id"):
                                classified_items[str(item["id"])] = item
        except Exception as exc:
            print(f"[classifier] LLM classification batch failed: {exc}")

    classifications: List[ClassifiedQuestion] = []
    for index, question in enumerate(questions):
        number = str(question.get("question_number", ""))
        text_str = str(question.get("text", ""))
        marks_val = question.get("marks")

        co_val = question.get("course_outcome")
        unit_val = question.get("unit")
        target_unit = None
        if co_val:
            co_match = re.search(r"\d+", str(co_val))
            if co_match:
                co_num = int(co_match.group(0))
                roman_map = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}
                target_unit = f"Unit {roman_map.get(co_num, 'I')} - CO{co_num}"
        elif unit_val:
            target_unit = _unit_from_topic(str(unit_val))

        item = classified_items.get(f"q{index}")
        syllabus_item = _lookup_syllabus_item(item, candidate_lookup, text_str, taxonomy, q_index=index, total_questions=len(questions), target_unit=target_unit)

        topic = syllabus_item["unit_topic"]
        subtopic = syllabus_item["subtopic"]
        confidence = float(item.get("confidence", 0.85) or 0.85) if item else 0.5
        raw_diff = str(item.get("difficulty", "Unknown")) if item else "Unknown"
        difficulty = raw_diff if raw_diff in ("Easy", "Medium", "Hard") else _infer_difficulty(text_str, marks_val)
        classified_question_type = str(item.get("question_type", "Theoretical")) if item else _infer_question_type(text_str)

        classifications.append(ClassifiedQuestion(
            question_number=number,
            text=text_str,
            topic=topic,
            subtopic=subtopic,
            difficulty=difficulty,
            question_type=classified_question_type,
            confidence=confidence,
            manual_review=confidence < 0.6,
            year=question.get("year"), marks=marks_val, source_page=question.get("source_page"),
            unit=_unit_from_topic(topic),
            course_outcome=question.get("course_outcome"),
        ))

    if not classifications:
        raise RuntimeError("Question classification failed completely. No workbook was created.")
    print(f"[classifier] LLM successfully classified {len(classified_items)} / {len(classifications)} question(s)")
    return classifications

