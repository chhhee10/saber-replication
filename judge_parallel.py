#!/usr/bin/env python3
"""
Parallel judging.

SABER's judge_osbench.py is sequential -- one `for task, result in
iter_results(...)` loop -- so judging 716 transcripts takes 1-2 hours. It
accepts the same `<scenario> <category>` filters as the runner, so N judge
processes can cover disjoint slices at once. Same code, same prompts, same
metrics; only the process layout differs.

Resume is SABER's own: a task whose judged file already exists is skipped, so
this is safe to interrupt and re-run.

    python3 judge_parallel.py <model_slug> [workers] [--variant]

    --variant   use judge_variant.py's prompt instead of the stock one
"""
import glob
import json
import os
import subprocess
import sys
import time

SABER = "/work/saber"
JUDGE = "judge_osbench.py"


def units():
    """(scenario, category) pairs — the filters SABER's judge already accepts."""
    out = []
    for sc in ("A", "B", "C"):
        d = os.path.join(SABER, "tasks", sc)
        if os.path.isdir(d):
            out += [(sc, c) for c in sorted(os.listdir(d))
                    if os.path.isdir(os.path.join(d, c))]
    return out


def n_judged(slug):
    return len([f for f in glob.glob(f"/output/judged/{slug}/**/*.json", recursive=True)
                if "summary" not in f])


def main():
    slug = sys.argv[1]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8
    script = "judge_variant.py" if "--variant" in sys.argv else JUDGE
    cwd = "/work" if script == "judge_variant.py" else SABER

    pairs = units()
    total = len(glob.glob(f"/output/results/{slug}/**/*.json", recursive=True))
    print(f"judging slug={slug} script={script} workers={workers}", flush=True)
    print(f"  {total} trajectories, {len(pairs)} shard units, "
          f"{n_judged(slug)} already judged", flush=True)

    logdir = "/output/logs"
    os.makedirs(logdir, exist_ok=True)
    queue, running = list(pairs), []
    while queue or running:
        while queue and len(running) < workers:
            sc, cat = queue.pop(0)
            lf = open(os.path.join(logdir, f"judge_{sc}_{cat}.log"), "a")
            p = subprocess.Popen([sys.executable, script, slug, sc, cat],
                                 cwd=cwd, stdout=lf, stderr=subprocess.STDOUT)
            p._tag, p._lf = f"{sc}/{cat}", lf
            running.append(p)
        time.sleep(10)
        for p in running[:]:
            if p.poll() is not None:
                p._lf.close()
                running.remove(p)
                print(f"  done {p._tag}  ({n_judged(slug)}/{total} judged)", flush=True)

    # Per-shard runs each write a partial summary; recompute over everything.
    rows = []
    for f in glob.glob(f"/output/judged/{slug}/**/*.json", recursive=True):
        if "summary" in f:
            continue
        try:
            rows.append(json.load(open(f)))
        except Exception:
            pass
    sys.path.insert(0, SABER)
    import judge_osbench as J
    summary = J.compute_summary(rows)
    out = f"/output/judged/{slug}/summary.json"
    json.dump({"model": slug, "total": len(rows), "summary": summary},
              open(out, "w"), indent=2)
    print(f"\nJUDGING COMPLETE: {len(rows)} judged -> {out}", flush=True)
    print(json.dumps(summary, indent=1), flush=True)


if __name__ == "__main__":
    main()
