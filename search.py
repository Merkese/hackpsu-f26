"""Search Europe PMC for open-access papers on a topic.

Usage:
    python search.py "sleep deprivation"
"""
import re
import sys

import requests

SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def search(topic, n=25, result_type="core"):
    params = {
        "query": f"({topic}) AND OPEN_ACCESS:y AND IN_EPMC:y",
        "format": "json",
        "resultType": result_type,  # core carries the abstract (needed for screening)
        "pageSize": n,
    }
    resp = requests.get(SEARCH, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("resultList", {}).get("result", [])
    papers = [
        {"pmcid": h.get("pmcid"), "doi": h.get("doi"),
         "title": h.get("title"),
         # PMC-only records have no pubYear; take it from the first publication date
         "year": h.get("pubYear") or (h.get("firstPublicationDate") or "")[:4] or None,
         "abstract": " ".join(re.sub(r"<[^>]+>", " ", h.get("abstractText") or "").split())}
        for h in hits if h.get("pmcid")
    ]
    return {"hit_count": data.get("hitCount", 0), "papers": papers}


def main():
    topic = " ".join(sys.argv[1:]) or "sleep deprivation"
    result = search(topic, n=10)
    print(f"{topic!r}: {result['hit_count']:,} hits, {len(result['papers'])} returned")
    for p in result["papers"][:5]:
        print(f"  {p['pmcid']}  {p['year']}  abstract {len(p['abstract'])} chars  {p['title']}")


if __name__ == "__main__":
    main()
