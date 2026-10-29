"""Crawl official manufacturer manuals into the knowledge base (as pending review).

    python -m scripts.crawl_manuals ge --max-pdfs 50 --max-pages 500
    python -m scripts.crawl_manuals --stats
"""
from __future__ import annotations

import argparse
import json
import logging

from app import db
from app.crawler import SOURCES, crawl, frontier_stats

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="*", help=f"one or more of {sorted(SOURCES)} (default: all)")
    ap.add_argument("--max-pdfs", type=int, default=50)
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    db.migrate()
    if args.stats:
        print(json.dumps(frontier_stats(), indent=2, default=str))
    else:
        for name in args.sources or sorted(SOURCES):
            print(name, crawl(name, args.max_pdfs, args.max_pages,
                              args.max_minutes * 60 if args.max_minutes else None))
