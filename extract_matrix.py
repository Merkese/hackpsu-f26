"""Extract Evidence Matrix fields with Gemini and run three trust checks.

Usage:
    pip install -U google-genai pydantic requests python-dotenv
    export GEMINI_API_KEY="your-key"        # Windows: set GEMINI_API_KEY=your-key
    python extract_matrix.py PMC13641949

Needs fetch_sections.py in the same folder.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Literal, Optional

from google import genai
from pydantic import BaseModel, Field

from fetch_sections import fetch_xml, parse_sections

try:  # load GEMINI_API_KEY from a .env file next to this script, if present
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

MODEL = "gemini-3.8-flash"  # change here if your key uses a different model
THINKING = "low"            # extraction needs little reasoning; "low" is much faster
MAX_CHARS = 30000           # cap per call to keep requests fast and cheap

Direction = Literal["improved", "declined", "no difference", "mixed", "not reported"]


class Extraction(BaseModel):
    paper_type: Literal["empirical", "protocol", "review", "meta-analysis",
                        "case report", "other"] = Field(
        description="Protocol if it describes a planned study with no results yet.")
    sample_size_enrolled: Optional[int] = Field(
        description="Participants enrolled or randomized. Null if not stated in this text.")
    sample_size_analyzed: Optional[int] = Field(
        description="Participants in the final analysis. Null if not stated in this text.")
    design: str = Field(description="e.g. randomized controlled trial, cross-sectional, cohort.")
    primary_outcome: str = Field(description="The main outcome measured.")
    direction: Direction = Field(
        description="Effect of the main intervention or exposure on the primary outcome.")
    direction_summary: str = Field(
        description="One line: [Improved/Declined/No difference] + what changed + under what condition.")
    evidence: str = Field(description="Short quote (under 25 words) supporting the direction.")


PROMPT = """You are extracting data from part of a scientific paper for an evidence matrix.
Use ONLY the text below. If a value is not stated, use null or "not reported".
Do not guess. Judge direction on the primary outcome, not secondary ones.
Direction rules:
- Follow the authors' own headline conclusion on the primary outcome.
- Use "mixed" ONLY if the authors themselves state no overall conclusion.
- In randomized trials, the intention-to-treat (ITT) result is the primary
  result. If ITT and per-protocol disagree, report the ITT direction and
  mention the per-protocol result in direction_summary.

Paper title: {title}
Text source: {source}

---
{text}
---"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def extract(client, title, source, text):
    log(f"Calling Gemini on {source} ({len(text):,} chars)...")
    start = time.time()
    interaction = client.interactions.create(
        model=MODEL,
        input=PROMPT.format(title=title, source=source, text=text),
        generation_config={"thinking_level": THINKING},
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": Extraction.model_json_schema(),
        },
    )
    log(f"  done in {time.time() - start:.1f}s")
    return Extraction.model_validate_json(interaction.output_text)


def _norm(s):
    s = (s.replace("“", '"').replace("”", '"')
          .replace("‘", "'").replace("’", "'")
          .replace("–", "-").replace("—", "-"))
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_quote(quote, text):
    """Check that an extracted quote literally appears in the text it came from."""
    q = _norm(quote).strip("\"' .")
    t = _norm(text)
    if not q:
        return {"status": "unclear", "detail": "no evidence quote returned"}
    if q in t:
        return {"status": "pass", "detail": "quote found verbatim in source text"}
    m = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    share = m.size / len(q)
    if share >= 0.9:
        return {"status": "pass", "detail": f"near match ({share:.0%} of quote found verbatim)"}
    return {"status": "flag", "detail": f"quote not found in source text (best match {share:.0%})"}


def run_checks(abstract, body, abstract_text, body_text):
    """Compare the abstract extraction against the methods+results extraction."""
    checks = {}

    # 1. Paper type flag
    types = {abstract.paper_type, body.paper_type}
    checks["paper_type"] = {
        "status": "pass" if types == {"empirical"} else "flag",
        "detail": f"abstract says {abstract.paper_type}, body says {body.paper_type}",
    }

    # 2. Abstract vs. results direction agreement
    pair = {abstract.direction, body.direction}
    if "not reported" in pair:
        status = "unclear"
    elif len(pair) == 1:
        status = "pass"
    elif "mixed" in pair:
        status = "partial"  # overlapping, not contradictory
    else:
        status = "flag"     # e.g. improved vs. no difference
    checks["direction_agreement"] = {
        "status": status,
        "detail": f"abstract: {abstract.direction} | results: {body.direction}",
    }

    # 3. Sample size consistency
    abstract_ns = {abstract.sample_size_enrolled, abstract.sample_size_analyzed} - {None}
    body_ns = {body.sample_size_enrolled, body.sample_size_analyzed} - {None}
    if not abstract_ns or not body_ns:
        status = "unclear"
    else:
        status = "pass" if abstract_ns & body_ns else "flag"
    detail = (f"abstract N {sorted(abstract_ns) or 'not stated'} | "
              f"methods/results enrolled {body.sample_size_enrolled}, "
              f"analyzed {body.sample_size_analyzed}")
    if (body.sample_size_enrolled and body.sample_size_analyzed
            and body.sample_size_enrolled != body.sample_size_analyzed):
        detail += " (attrition between enrolled and analyzed)"
    checks["sample_size_consistency"] = {"status": status, "detail": detail}

    # 4-5. Evidence quotes must appear in the text each extraction came from
    checks["evidence_abstract"] = verify_quote(abstract.evidence, abstract_text)
    checks["evidence_body"] = verify_quote(body.evidence, body_text)

    return checks


def build_texts(paper):
    """Results first, then methods up to the remaining budget, truncated once.

    Returns (abstract_text, body_text, truncated). The texts returned here are
    exactly what is sent to Gemini, so quote checks run against the same text.
    """
    secs = paper["sections"]
    results = secs.get("results", {}).get("text", "")
    methods = secs.get("methods", {}).get("text", "")
    body_text = (results + " " + methods[: max(0, MAX_CHARS - len(results) - 1)]).strip()[:MAX_CHARS]
    abstract_text = paper["abstract"][:MAX_CHARS]
    truncated = len(results) + len(methods) + 1 > MAX_CHARS
    return abstract_text, body_text, truncated


def main():
    pmcid = sys.argv[1] if len(sys.argv) > 1 else "PMC13641949"
    key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    if not key:
        here = os.path.dirname(os.path.abspath(__file__))
        sys.exit("GEMINI_API_KEY not found.\n"
                 f"  .env next to script: {os.path.exists(os.path.join(here, '.env'))}\n"
                 "  Fix: pip install python-dotenv, or set the key in THIS terminal.")
    os.environ["GEMINI_API_KEY"] = key  # strips stray quotes from Windows `set`

    cache = f"{pmcid}_sections.json"
    if os.path.exists(cache):
        with open(cache) as f:
            paper = json.load(f)
        log(f"Loaded cached {cache}")
    else:
        log(f"Downloading {pmcid} from Europe PMC...")
        paper = parse_sections(fetch_xml(pmcid))

    abstract_text, body_text, truncated = build_texts(paper)
    if not paper["abstract"] or not body_text.strip():
        sys.exit(f"{pmcid}: missing abstract or methods/results text, cannot run checks.")

    client = genai.Client()  # reads GEMINI_API_KEY
    with ThreadPoolExecutor(max_workers=2) as pool:  # run both calls at once
        a_job = pool.submit(extract, client, paper["title"], "abstract", abstract_text)
        b_job = pool.submit(extract, client, paper["title"],
                            "results and methods sections", body_text)
        abstract, body = a_job.result(), b_job.result()
    checks = run_checks(abstract, body, abstract_text, body_text)

    print(f"{paper['title']}\n")
    print(f"Design:    {body.design}")
    print(f"Outcome:   {body.primary_outcome}")
    print(f"N:         enrolled {body.sample_size_enrolled}, analyzed {body.sample_size_analyzed}")
    print(f"Direction: {body.direction_summary}")
    print(f"Evidence:  \"{body.evidence}\"\n")
    for name, c in checks.items():
        print(f"[{c['status'].upper():7}] {name}: {c['detail']}")

    out = {"pmcid": pmcid, "title": paper["title"], "truncated": truncated,
           "abstract_extraction": abstract.model_dump(),
           "body_extraction": body.model_dump(), "checks": checks}
    with open(f"{pmcid}_matrix.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved {pmcid}_matrix.json")


if __name__ == "__main__":
    main()
