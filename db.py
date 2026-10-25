"""Load _matrix.json results into Tiger Data and record pipeline events.

Usage:
    python db.py schema    # create tables from schema.sql (idempotent)
    python db.py load      # load every *_matrix.json in this folder
    python db.py counts    # row counts for the pipeline tables
"""
import glob
import json
import os
import sys

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

HERE = os.path.dirname(os.path.abspath(__file__))
# db.py can be imported before extract_matrix.py, so it must load .env itself
load_dotenv(os.path.join(HERE, ".env"))

TABLES = ["papers", "runs", "run_papers", "extractions", "checks", "pipeline_events"]


def conn():
    dsn = os.environ.get("TIGER_DSN", "").strip()
    if not dsn:
        raise RuntimeError("TIGER_DSN is not set (check .env)")  # never print the value itself
    return psycopg.connect(dsn, autocommit=True)


def apply_schema(db, path=os.path.join(HERE, "schema.sql")):
    """Run schema.sql one statement at a time (continuous aggregates need autocommit)."""
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if not ln.strip().startswith("--")]
    for stmt in "\n".join(lines).split(";"):
        if stmt.strip():
            db.execute(stmt)


def emit_event(db, run_id, paper_id, stage, status="ok", detail=""):
    db.execute(
        "INSERT INTO pipeline_events (ts, run_id, paper_id, stage, status, detail) "
        "VALUES (now(), %s, %s, %s, %s, %s)",
        (run_id, paper_id, stage, status, detail),
    )


def is_open_license(license):
    return "creativecommons.org/licenses/by" in (license or "").lower()


def upsert_paper(db, pmcid, doi=None, title=None, year=None, license=None):
    """Insert or update a paper without ever erasing a stored value.

    Empty strings count as missing (parse_sections() uses "" when the XML has no
    DOI or license), so they are stored as NULL and never overwrite real values.
    open_license is derived from the license URL and only changes when a license
    is supplied.
    """
    doi, title, license = doi or None, title or None, license or None
    return db.execute(
        """INSERT INTO papers (pmcid, doi, title, year, license, open_license)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (pmcid) DO UPDATE SET
             doi = COALESCE(EXCLUDED.doi, papers.doi),
             title = COALESCE(EXCLUDED.title, papers.title),
             year = COALESCE(EXCLUDED.year, papers.year),
             license = COALESCE(EXCLUDED.license, papers.license),
             open_license = CASE WHEN EXCLUDED.license IS NOT NULL
                                 THEN EXCLUDED.open_license
                                 ELSE papers.open_license END
           RETURNING id""",
        (pmcid, doi, title, year, license, is_open_license(license)),
    ).fetchone()[0]


def load_matrix(db, m):
    """Store one matrix dict. Older matrix files lack newer keys, so use .get()."""
    pid = upsert_paper(db, m["pmcid"], m.get("doi"), m.get("title"), m.get("year"),
                       m.get("license"))
    for source in ("abstract", "body"):
        data = m.get(f"{source}_extraction")
        if data is None:
            continue
        db.execute(
            """INSERT INTO extractions (paper_id, source, data, model, prompt_version)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (paper_id, source) DO UPDATE SET
                 data = EXCLUDED.data, model = EXCLUDED.model,
                 prompt_version = EXCLUDED.prompt_version, created_at = now()""",
            (pid, source, Jsonb(data), m.get("model"), m.get("prompt_version")),
        )
    for name, c in (m.get("checks") or {}).items():
        db.execute(
            """INSERT INTO checks (paper_id, name, status, detail) VALUES (%s, %s, %s, %s)
               ON CONFLICT (paper_id, name) DO UPDATE SET
                 status = EXCLUDED.status, detail = EXCLUDED.detail""",
            (pid, name, c["status"], c.get("detail")),
        )
    return pid


def row_counts(db):
    return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "counts"
    try:
        db = conn()
    except (RuntimeError, psycopg.OperationalError) as e:
        # OperationalError text can echo connection details, so print only its type
        sys.exit(str(e) if isinstance(e, RuntimeError) else "Could not connect to Tiger Data.")
    with db:
        if cmd == "schema":
            apply_schema(db)
            print("Schema applied.")
        elif cmd == "load":
            for path in sorted(glob.glob(os.path.join(HERE, "*_matrix.json"))):
                with open(path, encoding="utf-8") as f:
                    m = json.load(f)
                pid = load_matrix(db, m)
                print(f"loaded {m['pmcid']} -> paper id {pid}")
        elif cmd != "counts":
            sys.exit(f"Unknown command {cmd!r}. Use schema, load or counts.")
        for table, n in row_counts(db).items():
            print(f"  {table:16} {n}")


if __name__ == "__main__":
    main()
