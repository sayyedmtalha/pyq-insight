import re
from difflib import SequenceMatcher
from typing import List, Dict, Any
import pandas as pd


def deduplicate_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge identical or near-duplicate questions within the same academic year (e.g. Set 1 vs Set 2 variants)."""
    if not records:
        return []
    
    unique_records: List[Dict[str, Any]] = []
    for record in records:
        text = str(record.get("text", "")).strip().lower()
        year = record.get("year")
        norm_text = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text))
        
        is_duplicate = False
        for existing in unique_records:
            if existing.get("year") == year:
                exist_text = str(existing.get("text", "")).strip().lower()
                norm_exist = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", exist_text))
                
                # Sequence similarity test
                similarity = SequenceMatcher(None, norm_text, norm_exist).ratio()
                if similarity >= 0.85:
                    is_duplicate = True
                    break
        if not is_duplicate:
            unique_records.append(record)
            
    if len(records) > len(unique_records):
        print(f"[aggregator] Deduplicated {len(records) - len(unique_records)} duplicate question variant(s) across same-year papers")
    return unique_records


def build_topic_year_pivot(records: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["topic", "year", "count"])
    df["year"] = pd.to_numeric(df["year"], errors="coerce").fillna(0).astype(int)
    grouped = df.groupby(["topic", "year"]).size().reset_index(name="count")
    pivot = grouped.pivot(index="topic", columns="year", values="count").fillna(0)
    return pivot


def build_gap_analysis(records: List[Dict[str, Any]], syllabus_topics: List[str]) -> List[Dict[str, Any]]:
    observed = {record["topic"] for record in records if record.get("topic")}
    gaps = []
    for topic in syllabus_topics:
        if topic not in observed:
            gaps.append({"topic": topic, "status": "never-tested"})
    return gaps


def build_trends(records: List[Dict[str, Any]], period_size: int = 3) -> pd.DataFrame:
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["topic", "period", "change", "trend"])
    df["year"] = pd.to_numeric(df["year"], errors="coerce").fillna(0).astype(int)
    df["period"] = ((df["year"] - df["year"].min()) // period_size) + 1
    summary = df.groupby(["topic", "period"]).size().reset_index(name="count")
    summary = summary.sort_values(["topic", "period"])
    changes = []
    for topic, topic_df in summary.groupby("topic"):
        counts = topic_df["count"].tolist()
        if len(counts) >= 2:
            delta = counts[-1] - counts[0]
            trend = "rising" if delta > 0 else "falling" if delta < 0 else "stable"
            changes.append({"topic": topic, "period": topic_df["period"].max(), "change": delta, "trend": trend})
    return pd.DataFrame(changes)

