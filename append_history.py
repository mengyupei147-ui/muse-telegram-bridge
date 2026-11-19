#!/usr/bin/env python3
"""Append one history line to history.jsonl, only after a send succeeded.

Usage: append_history.py --role user|assistant --text "..." [--media "photo: file.jpg"]
"""
import argparse
import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(BASE, "history.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True)
    ap.add_argument("--text", default="")
    ap.add_argument("--media", default="")
    args = ap.parse_args()
    record = {"ts": int(time.time()), "role": args.role, "text": args.text}
    if args.media:
        record["media"] = args.media
    with open(HISTORY, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
