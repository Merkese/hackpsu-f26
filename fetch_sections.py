"""Fetch a paper's full-text XML from Europe PMC and split it into sections.

Usage:
    pip install requests
    python fetch_sections.py PMC13641949
"""
import json
import sys
import xml.etree.ElementTree as ET

import requests

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/{}/fullTextXML"

# Checked in this order, so "Results and Discussion" counts as results.
SECTION_KEYWORDS = [
    ("methods", ["method", "materials", "participants", "procedure"]),
    ("results", ["result", "findings"]),
    ("discussion", ["discussion", "conclusion"]),
    ("introduction", ["introduction", "background"]),
]

SKIP_TAGS = {"table-wrap", "fig"}  # kept out of the text, counted separately


class NoFullText(Exception):
    """Paper has no full-text XML in Europe PMC."""


def fetch_xml(pmcid):
    resp = requests.get(BASE.format(pmcid), timeout=30)
    # Europe PMC answers an unknown or non-open-access ID with 404 or a bare 500.
    if resp.status_code in (404, 500):
        raise NoFullText(f"{pmcid}: no full-text XML (probably not in the open-access "
                         f"subset; HTTP {resp.status_code})")
    resp.raise_for_status()
    return resp.content


def clean_text(el):
    """Text of an element, skipping tables and figures."""
    parts = [el.text or ""]

    def walk(e):
        if e.tag not in SKIP_TAGS:
            parts.append(e.text or "")
            for child in e:
                walk(child)
        parts.append(e.tail or "")

    for child in el:
        walk(child)
    return " ".join(" ".join(parts).split())


def classify(sec):
    sec_type = (sec.get("sec-type") or "").lower()
    title_el = sec.find("title")
    title = clean_text(title_el).lower() if title_el is not None else ""
    for name, keywords in SECTION_KEYWORDS:
        if any(k in sec_type or k in title for k in keywords):
            return name, title
    return "other", title


def parse_sections(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = {"title": "", "abstract": "", "sections": {}, "unmatched": []}

    title_el = root.find(".//front//article-title")
    if title_el is not None:
        out["title"] = clean_text(title_el)

    abstract_el = root.find(".//front//abstract")
    if abstract_el is not None:
        out["abstract"] = clean_text(abstract_el)

    body = root.find(".//body")
    if body is None:
        return out  # abstract-only record

    for sec in body.findall("sec"):  # top-level sections only
        name, title = classify(sec)
        if name == "other":
            out["unmatched"].append(title or "(untitled)")
            continue
        entry = out["sections"].setdefault(name, {"text": "", "tables": 0})
        entry["text"] = (entry["text"] + " " + clean_text(sec)).strip()
        entry["tables"] += len(sec.findall(".//table-wrap"))
    return out


def main():
    pmcid = sys.argv[1] if len(sys.argv) > 1 else "PMC13641949"
    try:
        data = parse_sections(fetch_xml(pmcid))
    except NoFullText as e:
        sys.exit(str(e))

    print(f"Title: {data['title']}\n")
    print(f"Abstract: {len(data['abstract'])} chars")
    for name, entry in data["sections"].items():
        print(f"{name:13} {len(entry['text']):>7} chars, {entry['tables']} tables")
    if data["unmatched"]:
        print(f"Unmatched sections: {data['unmatched']}")

    with open(f"{pmcid}_sections.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"\nSaved {pmcid}_sections.json")


if __name__ == "__main__":
    main()