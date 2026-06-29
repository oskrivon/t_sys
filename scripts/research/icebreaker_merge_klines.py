"""Merge per-coin 1m OHLC jsonl from multiple dirs into one (dedup by ts, ascending).
    python scripts/research/icebreaker_merge_klines.py --out data/klines_2y \
        --dirs data/klines_ext data/klines_wide
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dirs", nargs="+", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    syms = set()
    for d in args.dirs:
        for p in Path(d).glob("*.jsonl"):
            syms.add(p.stem)
    for sym in sorted(syms):
        bars = {}
        for d in args.dirs:
            p = Path(d) / f"{sym}.jsonl"
            if not p.exists():
                continue
            for line in open(p):
                if line.strip():
                    b = json.loads(line)
                    bars[b["ts"]] = b
        rows = [bars[k] for k in sorted(bars)]
        with open(out / f"{sym}.jsonl", "w") as f:
            for b in rows:
                f.write(json.dumps(b) + "\n")
        print(f"  {sym}: {len(rows)} bars", flush=True)
    print(f"merged {len(syms)} coins -> {out}")


if __name__ == "__main__":
    main()
