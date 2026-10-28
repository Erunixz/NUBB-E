#!/usr/bin/env python3
"""Find the best --n-cpu-moe value for NUBB-E's MoE model.

    python scripts/bench_moe.py
    python scripts/bench_moe.py --from 22 --to 30 --step 1

Runs llama-bench once per --n-cpu-moe value (lower = more experts on the
GPU = faster, until it runs out of VRAM). Prints generation and prompt
speed, peak VRAM, and whether the model loaded. Pick the fastest value
that loads with some VRAM left over (about 0.5 GB or more).

Standard library only. Needs nvidia-smi for the VRAM column.
"""
import argparse
import json
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""
DEFAULT_BENCH = ROOT / "bin" / "llama.cpp" / f"llama-bench{EXE}"
DEFAULT_MODEL = ROOT / "models" / "Qwen3.6-35B-A3B-MTP-UD-IQ4_XS.gguf"


def vram_used_mb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout
        used, total = (int(x) for x in out.split(",")[:2])
        return used, total
    except Exception:
        return None, None


def run_one(args, ncmoe):
    cmd = [str(args.bench), "-m", str(args.model), "-ngl", "99",
           "--n-cpu-moe", str(ncmoe), "-fa", "1", "-ctk", "q8_0", "-ctv", "q8_0",
           "-p", str(args.prompt), "-n", str(args.gen), "-r", str(args.reps),
           "-o", "json"]
    peak = [0]
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            used, _ = vram_used_mb()
            if used:
                peak[0] = max(peak[0], used)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        done.set()
        return {"ok": False, "why": "timeout", "peak": peak[0]}
    done.set()
    t.join()

    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        rows = []
    if proc.returncode != 0 or not rows:
        last = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        return {"ok": False, "why": last[:60], "peak": peak[0]}

    res = {"ok": True, "peak": peak[0], "pp": None, "tg": None}
    for r in rows:
        if r.get("n_gen", 0) > 0:
            res["tg"] = r["avg_ts"]
        elif r.get("n_prompt", 0) > 0:
            res["pp"] = r["avg_ts"]
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", type=Path, default=DEFAULT_BENCH, help="path to llama-bench")
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="GGUF model file")
    ap.add_argument("--from", dest="lo", type=int, default=20)
    ap.add_argument("--to", dest="hi", type=int, default=34)
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--prompt", type=int, default=512, help="prompt tokens to test")
    ap.add_argument("--gen", type=int, default=128, help="tokens to generate")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=600, help="seconds per run")
    args = ap.parse_args()

    for p in (args.bench, args.model):
        if not p.exists():
            sys.exit(f"Not found: {p}")

    _, total = vram_used_mb()
    print(f"Model: {args.model.name}")
    print(f"GPU VRAM: {total or '?'} MB\n")
    print(f"{'n-cpu-moe':>9}  {'gen tok/s':>9}  {'prompt tok/s':>12}  {'peak VRAM':>10}  status")
    print("-" * 60)

    results = []
    for n in range(args.lo, args.hi + 1, args.step):
        r = run_one(args, n)
        results.append((n, r))
        if r["ok"]:
            print(f"{n:>9}  {r['tg'] or 0:>9.1f}  {r['pp'] or 0:>12.1f}  "
                  f"{r['peak']:>7} MB  loaded")
        else:
            print(f"{n:>9}  {'-':>9}  {'-':>12}  {r['peak']:>7} MB  FAILED: {r['why']}")
        sys.stdout.flush()

    headroom = 512
    good = [(n, r) for n, r in results
            if r["ok"] and r["tg"] and (not total or r["peak"] <= total - headroom)]
    if good:
        n, r = max(good, key=lambda x: x[1]["tg"])
        print(f"\nBest: --n-cpu-moe {n}  ({r['tg']:.1f} tok/s, {r['peak']} MB peak)")
    else:
        print("\nNo value loaded with enough VRAM left. Try a higher --to.")


if __name__ == "__main__":
    main()
