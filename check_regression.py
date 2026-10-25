"""Compare saved matrix outputs against hand-scored expectations in pinned_papers.json.

Run it after every prompt or schema change (after re-running the pinned papers).
Usage:
    python check_regression.py

Exit code is 1 if any paper FAILs. Papers with no saved matrix, or no `expected`
block, are skipped with a message.
"""
import json
import os
import sys

from extract_matrix import PROMPT_VERSION, matrix_path

HERE = os.path.dirname(os.path.abspath(__file__))


def load_pinned(path=os.path.join(HERE, "pinned_papers.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["papers"]


def compare(m, exp):
    """Return a list of problems (empty means the matrix matches the expectations)."""
    abstract = m.get("abstract_extraction") or {}
    body = m.get("body_extraction") or abstract
    problems = []
    if "paper_type" in exp and abstract.get("paper_type") != exp["paper_type"]:
        problems.append(f"type {abstract.get('paper_type')}")
    if "design_contains" in exp and exp["design_contains"] not in (body.get("design") or "").lower():
        problems.append(f"design {body.get('design')!r}")
    for k in ("enrolled", "analyzed"):
        if k in exp and body.get(f"sample_size_{k}") != exp[k]:
            problems.append(f"{k} {body.get(f'sample_size_{k}')}")
    if "analyzed_in" in exp and body.get("sample_size_analyzed") not in exp["analyzed_in"]:
        problems.append(f"analyzed {body.get('sample_size_analyzed')}")
    if "direction_in" in exp and body.get("direction") not in exp["direction_in"]:
        problems.append(f"direction {body.get('direction')}")
    return problems


def main(folder=HERE):
    failed = checked = 0
    for paper in load_pinned():
        pmcid, exp = paper["pmcid"], paper.get("expected")
        if not exp:
            print(f"{pmcid}: SKIP (no expected values yet)")
            continue
        path = matrix_path(pmcid, folder)
        if not os.path.exists(path):
            print(f"{pmcid}: SKIP (no {path}, run extract_matrix.py first)")
            continue
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        problems = compare(m, exp)
        stale = "" if m.get("prompt_version") == PROMPT_VERSION else \
            f" (matrix is from prompt_version {m.get('prompt_version')!r}, current is {PROMPT_VERSION!r})"
        print(f"{pmcid}: {'OK' if not problems else 'FAIL ' + ', '.join(problems)}{stale}")
        checked += 1
        failed += bool(problems)
    print(f"\n{checked} checked, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
