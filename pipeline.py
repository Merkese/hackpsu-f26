"""Run a topic end to end: search, fetch, extract, check, store.

Usage:
    python pipeline.py "sleep deprivation" --n 3
    python pipeline.py "sleep deprivation" --n 15 --question "Does it impair gait?"

Each paper costs two Gemini calls (abstract and body), so start with a small --n.
Papers that already have a _matrix.json from the current PROMPT_VERSION are loaded
from it and cost nothing.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from db import conn, emit_event, load_matrix, upsert_paper
from extract_matrix import (PROMPT_VERSION, MissingText, log, make_client,
                            matrix_path, process_paper)
from fetch_sections import NoFullText
from search import search

# Stages process_paper() reports that also move a paper forward in run_papers.
PROGRESS_STAGES = {"fetched", "extracted", "checked"}


def cached_matrix(pmcid, cache_dir="."):
    """Return the saved matrix dict if it was made by the current prompt, else None."""
    path = matrix_path(pmcid, cache_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    return m if m.get("prompt_version") == PROMPT_VERSION else None


def set_stage(db, run_id, paper_id, stage, reason=None):
    db.execute("UPDATE run_papers SET stage = %s, reason = %s WHERE run_id = %s AND paper_id = %s",
               (stage, reason, run_id, paper_id))


def run_topic(topic, n=15, workers=1, question=None, cache_dir=".",
              connect=conn, search_fn=search, process_fn=process_paper, client_fn=make_client):
    """Search a topic and process every hit. Returns the run id.

    Each paper makes one Gemini call, so workers=1 keeps one call in flight. The *_fn and `connect` arguments exist so tests can
    swap in fakes.
    """
    found = search_fn(topic, n)
    unique = list(dict.fromkeys(p["pmcid"] for p in found["papers"]))  # drop repeats, keep order
    # Fail fast, before any database write, if Gemini is needed but not configured.
    # Doing it here keeps a missing key from being recorded as an exclusion per paper.
    needs_gemini = any(cached_matrix(pm, cache_dir) is None for pm in unique)
    client = client_fn() if needs_gemini else None

    db = connect()
    run_id = db.execute(
        "INSERT INTO runs (topic, research_question, hit_count) VALUES (%s, %s, %s) RETURNING id",
        (topic, question, found["hit_count"])).fetchone()[0]
    log(f"Run {run_id}: {found['hit_count']:,} hits for {topic!r}, processing {len(found['papers'])}")

    ids = {}
    for p in found["papers"]:
        if p["pmcid"] in ids:
            continue
        pid = upsert_paper(db, p["pmcid"], p.get("doi"), p.get("title"), p.get("year"))
        ids[p["pmcid"]] = pid
        db.execute("INSERT INTO run_papers (run_id, paper_id, stage) VALUES (%s, %s, 'searched') "
                   "ON CONFLICT DO NOTHING", (run_id, pid))
        emit_event(db, run_id, pid, "searched")

    def one(pmcid):
        pid = ids[pmcid]
        own_db = connect()  # one connection per worker thread

        def emit(stage, _pmcid, status="ok", detail=""):
            emit_event(own_db, run_id, pid, stage, status, detail)
            if stage in PROGRESS_STAGES and status == "ok":
                set_stage(own_db, run_id, pid, stage)

        stage, reason = "checked", None
        try:
            m = cached_matrix(pmcid, cache_dir)
            if m is not None:
                emit("checked", pmcid, "ok", "cached matrix")
            else:
                m = process_fn(pmcid, client, emit, cache_dir)
            load_matrix(own_db, m)
        except NoFullText:
            stage, reason = "excluded", "no full text"
        except MissingText:
            stage, reason = "excluded", "no abstract"
        except Exception as e:  # API failure after retries, parse errors, database errors
            # Only the type name is stored: messages can carry request or connection details.
            stage, reason = "excluded", f"error: {type(e).__name__}"
        try:
            set_stage(own_db, run_id, pid, stage, reason)
            if reason:
                emit_event(own_db, run_id, pid, "excluded", "fail", reason)
        finally:
            own_db.close()
        return pmcid, stage, reason

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, pm) for pm in ids]
        for f in as_completed(futures):
            try:
                pmcid, stage, reason = f.result()
            except Exception as e:  # e.g. the database went away while recording a paper
                log(f"  a paper could not be recorded: {type(e).__name__}")
                continue
            log(f"  {pmcid}: {stage}" + (f" ({reason})" if reason else ""))
    db.close()
    return run_id


def run_summary(run_id, connect=conn):
    """Stage counts and exclusion reasons for one run (the PRISMA numbers)."""
    db = connect()
    try:
        hit_count = db.execute("SELECT hit_count FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
        stages = dict(db.execute(
            "SELECT stage, count(*) FROM run_papers WHERE run_id = %s GROUP BY stage", (run_id,)))
        exclusions = db.execute(
            "SELECT p.pmcid, rp.reason FROM run_papers rp JOIN papers p ON p.id = rp.paper_id "
            "WHERE rp.run_id = %s AND rp.stage = 'excluded' ORDER BY p.pmcid", (run_id,)).fetchall()
        events = db.execute("SELECT count(*) FROM pipeline_events WHERE run_id = %s",
                            (run_id,)).fetchone()[0]
    finally:
        db.close()
    return {"run_id": run_id, "hit_count": hit_count, "stages": stages,
            "exclusions": [{"pmcid": a, "reason": b} for a, b in exclusions], "events": events}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("topic")
    ap.add_argument("--n", type=int, default=3, help="papers to process (default 3)")
    ap.add_argument("--workers", type=int, default=1, help="parallel papers (default 1)")
    ap.add_argument("--question", help="optional research question, stored with the run")
    args = ap.parse_args()
    try:
        run_id = run_topic(args.topic, args.n, args.workers, args.question)
    except RuntimeError as e:  # missing TIGER_DSN or GEMINI_API_KEY
        sys.exit(str(e))
    s = run_summary(run_id)
    print(f"\nRun {s['run_id']}: {s['hit_count']:,} records identified")
    for stage, count in sorted(s["stages"].items()):
        print(f"  {stage:10} {count}")
    for x in s["exclusions"]:
        print(f"  excluded {x['pmcid']}: {x['reason']}")
    print(f"  {s['events']} pipeline events recorded")


if __name__ == "__main__":
    main()
