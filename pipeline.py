#!/usr/bin/env python3
"""
SABER replication pipeline — runs inside the runner container.

  inference (sharded, resumable)  ->  judge  ->  comparison report

Everything is written to /output (mounted from the host Desktop) so results
survive container restarts and are directly inspectable.
"""
import json, os, sys, subprocess, time, hashlib, glob, shutil
from pathlib import Path
from datetime import datetime, timezone

SABER = Path("/work/saber")
OUT = Path("/output")
MODEL_SLUG = os.environ.get("MODEL_SLUG", "ds32_repro")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "deepseek-v3.2")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-4-6")
BASE_URL = os.environ.get("PROXY_BASE_URL", "https://models.aikin.club")
API_KEY = os.environ.get("PROXY_API_KEY", "")
SHARDS = int(os.environ.get("SHARDS", "4"))
MAX_STEPS = int(os.environ.get("MAX_STEPS", "30"))
SCENARIO = os.environ.get("SCENARIO", "").strip()      # optional: A / B / C
PHASE = os.environ.get("PHASE", "all")                 # all | infer | judge | report

PUBLISHED = {  # SABER paper Table 3 — DeepSeek-V3.2
    "HSR": 0.796, "HSR_A": 0.733, "HSR_B": 0.748, "HSR_C": 0.902,
    "CPR": 0.248, "SRR": 0.010, "LRR": 0.008, "Incapable_Rate": 0.138,
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def setup():
    """Wire SABER's output dirs to the mounted volume and write config.json."""
    OUT.mkdir(parents=True, exist_ok=True)
    for sub in ("results", "judged", "logs"):
        (OUT / sub).mkdir(exist_ok=True)
        link = SABER / sub
        if link.is_symlink() or link.exists():
            if link.is_symlink():
                link.unlink()
            elif link.is_dir():
                shutil.rmtree(link)
        link.symlink_to(OUT / sub)

    if not API_KEY:
        log("FATAL: PROXY_API_KEY is not set."); sys.exit(2)

    cfg = {
        "base_url": BASE_URL,
        "max_steps": MAX_STEPS,
        "judge": {"id": JUDGE_MODEL, "type": "anthropic", "key": API_KEY, "base_url": BASE_URL},
        "models": {MODEL_SLUG: {"id": AGENT_MODEL, "type": "openai", "key": API_KEY, "base_url": BASE_URL}},
    }
    (SABER / "config.json").write_text(json.dumps(cfg, indent=2))
    log(f"config: agent={AGENT_MODEL} judge={JUDGE_MODEL} max_steps={MAX_STEPS} slug={MODEL_SLUG}")


def preflight():
    """Fail fast and loudly rather than silently producing garbage."""
    import httpx, anthropic
    ok = True
    try:
        r = httpx.post(f"{BASE_URL}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {API_KEY}"},
                       json={"model": AGENT_MODEL, "max_tokens": 8,
                             "messages": [{"role": "user", "content": "say OK"}]}, timeout=90)
        j = r.json()
        if "choices" not in j:
            log(f"FATAL: agent model {AGENT_MODEL} unusable: {str(j)[:200]}"); ok = False
        else:
            log(f"preflight OK: agent {AGENT_MODEL}")
    except Exception as e:
        log(f"FATAL: agent probe failed: {e}"); ok = False
    try:
        c = anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL)
        c.messages.create(model=JUDGE_MODEL, max_tokens=8,
                          messages=[{"role": "user", "content": "say OK"}])
        log(f"preflight OK: judge {JUDGE_MODEL}")
    except Exception as e:
        log(f"FATAL: judge model {JUDGE_MODEL} unusable: {str(e)[:200]}"); ok = False
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=30)
        img = subprocess.run(["docker", "images", "-q", "osbench-sandbox"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        if not img:
            log("FATAL: docker image 'osbench-sandbox' not found on the host."); ok = False
        else:
            log("preflight OK: docker + osbench-sandbox image")
    except Exception as e:
        log(f"FATAL: docker unavailable inside container: {e}"); ok = False
    if not ok:
        sys.exit(3)


def shards():
    """(scenario, category) pairs — SABER's runner accepts these natively,
    so we shard with zero changes to its code."""
    pairs = []
    for sc in (["A", "B", "C"] if not SCENARIO else [SCENARIO]):
        d = SABER / "tasks" / sc
        if d.is_dir():
            pairs += [(sc, c.name) for c in sorted(d.iterdir()) if c.is_dir()]
    return pairs


def n_done():
    return len(glob.glob(str(OUT / "results" / MODEL_SLUG / "**" / "*.json"), recursive=True))


def n_total():
    if SCENARIO:
        return len(glob.glob(str(SABER / "tasks" / SCENARIO / "*" / "*.json")))
    return len(glob.glob(str(SABER / "tasks" / "*" / "*" / "*.json")))


def errored_results():
    """Tasks saved with an 'error' field — e.g. a container-start timeout under
    parallel load. SABER's judge turns these into 'Incapable', which silently
    removes them from the HSR denominator, so they must never be left in place."""
    bad = []
    for f in glob.glob(str(OUT / "results" / MODEL_SLUG / "**" / "*.json"), recursive=True):
        try:
            if json.load(open(f)).get("error"):
                bad.append(f)
        except Exception:
            bad.append(f)          # unreadable/truncated counts as bad too
    return bad


def purge_errored():
    """Delete errored results so the resume logic re-runs them instead of
    treating them as completed."""
    bad = errored_results()
    for f in bad:
        try:
            os.remove(f)
        except Exception:
            pass
    if bad:
        log(f"  purged {len(bad)} errored task result(s) — they will be retried")
    return len(bad)


def run_inference():
    pairs = shards()
    total = n_total()
    log(f"INFERENCE: {total} tasks, {len(pairs)} shard units, {SHARDS} parallel workers")
    purge_errored()
    log(f"  already done: {n_done()} (resumable — re-running skips completed tasks)")
    queue, running = list(pairs), []
    logdir = OUT / "logs"
    while queue or running:
        while queue and len(running) < SHARDS:
            sc, cat = queue.pop(0)
            lf = open(logdir / f"infer_{sc}_{cat}.log", "a")
            p = subprocess.Popen([sys.executable, "run_osbench.py", MODEL_SLUG, sc, cat],
                                 cwd=SABER, stdout=lf, stderr=subprocess.STDOUT)
            p._tag, p._lf = f"{sc}/{cat}", lf
            running.append(p)
            log(f"  + shard {sc}/{cat} (pid {p.pid})")
        time.sleep(20)
        for p in running[:]:
            if p.poll() is not None:
                p._lf.close(); running.remove(p)
                log(f"  - shard {p._tag} finished rc={p.returncode} | progress {n_done()}/{total}")

    # Retry pass: errored tasks are purged and re-run once, sequentially, so a
    # transient timeout under parallel load cannot corrupt the denominator.
    bad = errored_results()
    if bad:
        log(f"RETRY: {len(bad)} task(s) errored — re-running them sequentially")
        purge_errored()
        lf = open(OUT / "logs" / "retry.log", "a")
        for sc, cat in pairs:
            subprocess.run([sys.executable, "run_osbench.py", MODEL_SLUG, sc, cat],
                           cwd=SABER, stdout=lf, stderr=subprocess.STDOUT)
        lf.close()

    still_bad = len(errored_results())
    log(f"INFERENCE COMPLETE: {n_done()}/{total} tasks, {still_bad} still errored")
    if still_bad:
        pct = still_bad / max(1, total)
        log("=" * 70)
        log(f"WARNING: {still_bad} task(s) ({pct:.1%}) failed to execute even after retry.")
        log("  These become 'Incapable' in the judge and are EXCLUDED from the HSR")
        log("  denominator, which shifts the result. Check logs/retry.log.")
        if pct > 0.02:
            log("  >2% failure rate — re-run with fewer shards, e.g. ./run.sh --shards 2")
        log("=" * 70)


def run_judge():
    log(f"JUDGE: scoring with {JUDGE_MODEL}")
    lf = open(OUT / "logs" / "judge.log", "a")
    args = [sys.executable, "judge_osbench.py", MODEL_SLUG] + ([SCENARIO] if SCENARIO else [])
    p = subprocess.run(args, cwd=SABER, stdout=lf, stderr=subprocess.STDOUT)
    lf.close()
    # SABER's judge swallows API errors and falls back to Incapable — detect that.
    errs = tot = 0
    for f in glob.glob(str(OUT / "judged" / MODEL_SLUG / "**" / "*.json"), recursive=True):
        if "summary" in f:
            continue
        tot += 1
        try:
            if json.load(open(f)).get("judge_err"):
                errs += 1
        except Exception:
            pass
    log(f"JUDGE COMPLETE: {tot} judged, {errs} with judge errors")
    if tot and errs / tot > 0.02:
        log("=" * 70)
        log(f"FATAL: {errs}/{tot} judge calls FAILED. SABER falls back to 'Incapable'")
        log("       on error, so the summary would be silently WRONG. Not writing a report.")
        log("       Check /output/logs/judge.log — usually an API key or model-access issue.")
        log("=" * 70)
        sys.exit(4)


def report():
    sf = OUT / "judged" / MODEL_SLUG / "summary.json"
    if not sf.exists():
        log("no summary.json — run the judge phase first"); return
    s = json.load(open(sf)).get("summary", {})
    lines = []
    A = lines.append
    A("=" * 74)
    A("SABER REPLICATION — DeepSeek-V3.2")
    A("=" * 74)
    A(f"  agent model : {AGENT_MODEL}")
    A(f"  judge model : {JUDGE_MODEL}   (paper used claude-opus-4-6)")
    A(f"  tasks       : {s.get('total')}   effective: {s.get('effective')}")
    A(f"  generated   : {datetime.now(timezone.utc).isoformat()}")
    nbad = len(errored_results())
    A(f"  exec errors : {nbad}" + ("  <-- WARNING: these are excluded from the denominator" if nbad else "  (clean run)"))
    A("")
    A(f"  {'metric':<18}{'ours':>10}{'published':>12}{'delta':>10}")
    A("  " + "-" * 50)
    for k in ["HSR", "HSR_A", "HSR_B", "HSR_C", "CPR", "SRR", "LRR", "Incapable_Rate"]:
        ours, pub = s.get(k), PUBLISHED.get(k)
        if ours is None or pub is None:
            continue
        A(f"  {k:<18}{ours:>10.3f}{pub:>12.3f}{ours - pub:>+10.3f}")
    A("")
    tc = s.get("termination_counts", {})
    A(f"  outcomes: {tc}")
    A("")
    hsr = s.get("HSR")
    if hsr is not None:
        d = abs(hsr - PUBLISHED["HSR"])
        A(f"  HSR gap vs published: {d:+.3f}")
        if d <= 0.03:
            A("  >>> CLOSE MATCH — replication successful within 3 points.")
        elif d <= 0.07:
            A("  >>> PARTIAL MATCH — within the expected judge-substitution offset")
            A("      (sonnet-4-6 measured ~7 pts more lenient than opus-4-6).")
        else:
            A("  >>> DIVERGENT — investigate before drawing conclusions.")
    A("")
    A("  NOTE: the paper judged with claude-opus-4-6, which is not invokable on")
    A("  this proxy (Bedrock requires an inference profile). claude-sonnet-4-6 was")
    A("  measured at ~7 HSR points more lenient on identical trajectories, so a")
    A("  result near 0.72-0.73 is consistent with a successful replication.")
    A("=" * 74)
    txt = "\n".join(lines)
    print(txt, flush=True)
    (OUT / "REPLICATION_REPORT.txt").write_text(txt + "\n")
    log("report -> /output/REPLICATION_REPORT.txt")


def manifest():
    """Record exact conditions so runs are comparable across machines."""
    tasks = sorted(glob.glob(str(SABER / "tasks" / "*" / "*" / "*.json")))
    h = hashlib.sha256()
    for t in tasks:
        h.update(Path(t).name.encode())
    m = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "agent_model": AGENT_MODEL, "judge_model": JUDGE_MODEL,
        "model_slug": MODEL_SLUG, "base_url": BASE_URL,
        "max_steps": MAX_STEPS, "shards": SHARDS,
        "scenario_filter": SCENARIO or "all",
        "task_count": len(tasks), "task_set_sha256": h.hexdigest()[:16],
        "sampling": "provider default (SABER sets no temperature/seed) — k=1",
        "python": sys.version.split()[0],
        "note": "Paper judged with claude-opus-4-6; see REPLICATION_REPORT.txt",
    }
    (OUT / "MANIFEST.json").write_text(json.dumps(m, indent=2))
    log(f"manifest -> /output/MANIFEST.json (task_set {m['task_set_sha256']})")


def fix_ownership():
    """The container runs as root; without this every output file on the host
    Desktop would be root-owned and undeletable without sudo."""
    uid, gid = os.environ.get("HOST_UID"), os.environ.get("HOST_GID")
    if not (uid and gid):
        return
    try:
        subprocess.run(["chown", "-R", f"{uid}:{gid}", str(OUT)], check=False, timeout=300)
        log(f"output ownership set to {uid}:{gid}")
    except Exception as e:
        log(f"warning: could not chown output: {e}")


if __name__ == "__main__":
    log("SABER replication pipeline starting")
    import atexit
    atexit.register(fix_ownership)
    setup()
    manifest()
    if PHASE in ("all", "infer"):
        preflight()
        run_inference()
    if PHASE in ("all", "judge"):
        preflight() if PHASE == "judge" else None
        run_judge()
    if PHASE in ("all", "judge", "report"):
        report()
    log("DONE")
