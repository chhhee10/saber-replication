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
SHARDS = int(os.environ.get("SHARDS", "1"))   # 1 = PURE SABER (default)
MAX_STEPS = int(os.environ.get("MAX_STEPS", "30"))
SCENARIO = os.environ.get("SCENARIO", "").strip()      # optional: A / B / C
PHASE = os.environ.get("PHASE", "all")                 # all | infer | judge | report
FAILPROOF = os.environ.get("FAILPROOF", "0") == "1"    # guarded arm
FP_CUSTOM = os.environ.get("FP_CUSTOM", "0") == "1"    # add custom + MCP policies

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


def setup_failproof():
    """Install the arm's policies and PROVE the guardrail fires before running.

    A guarded arm with an inert guardrail is byte-identical to the bare arm and
    produces a worthless dataset. We verified experimentally that a one-character
    difference (tool_name 'bash' vs 'Bash') silently allows everything, so this
    check is a hard precondition, not a nicety.
    """
    if not FAILPROOF:
        return None
    sys.path.insert(0, "/work")
    import failproof_shim as FS
    home = "/work/.fpai_home"
    os.makedirs(home, exist_ok=True)
    builtins = open("/work/builtins_34.txt").read().split()
    custom, extras = "", []
    if FP_CUSTOM:
        try:
            custom = open("/work/custom_policy_b64.txt").read().strip()
        except Exception:
            pass
        mcp_pol = "/work/policies/mcp-harms.policies.mjs"
        if os.path.exists(mcp_pol):
            extras.append(mcp_pol)
    info = FS.install_policies(home, builtins, custom, extra_files=extras)
    log(f"ARM: {'builtins + custom + MCP' if FP_CUSTOM else 'VANILLA builtins only'}")
    log(f"FAILPROOFAI: {info['n_builtins']} builtins, policy files: {info.get('policy_files')}")
    os.environ["FAILPROOF_HOME"] = home
    os.environ["FAILPROOF_AUDIT"] = str(OUT / "failproof_audit.jsonl")
    os.environ["FAILPROOF_STATS"] = str(OUT / "failproof_stats.json")
    gate = FS.FailproofGate(home, write_stats=False)   # preflight only
    FS.preflight_or_die(gate, check_mcp=bool(extras), check_custom=FP_CUSTOM)  # aborts if not firing
    return info


def reap_orphans():
    """Remove sandbox containers orphaned by a previous killed run.

    SABER starts task containers with --rm, but a hard kill (laptop sleep,
    power loss, docker kill) leaves them running: the parent never gets to stop
    them. Across a night of restarts they accumulate and consume RAM/disk.
    """
    try:
        out = subprocess.run(["docker", "ps", "-aq", "--filter", "name=osbench-"],
                             capture_output=True, text=True, timeout=30).stdout.split()
        if out:
            subprocess.run(["docker", "rm", "-f"] + out,
                           capture_output=True, timeout=120)
            log(f"reaped {len(out)} orphaned sandbox container(s) from a previous run")
    except Exception as e:
        log(f"warning: could not reap orphans: {e}")


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
    """Task count for the current filter. SCENARIO may be a scenario letter
    (A/B/C) or a single task id (e.g. B_code_001), matching SABER's own CLI."""
    if SCENARIO and "_" in SCENARIO:                      # single task id
        return len(glob.glob(str(SABER / "tasks" / "*" / "*" / f"{SCENARIO}.json")))
    if SCENARIO:                                          # scenario letter
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


def run_inference_pure():
    """PURE SABER MODE (default).

    Runs exactly the command SABER's own RUNNING.md documents:

        python3 run_osbench.py <model>

    One process, sequential, full task set, SABER's own ordering. No sharding,
    no purging, no retrying — nothing of ours touches the execution path. This
    is byte-for-byte the way the SABER authors run their own benchmark.
    """
    total = n_total()
    log("INFERENCE: PURE SABER MODE (single process, sequential)")
    log(f"  command : python3 run_osbench.py {MODEL_SLUG}" + (f" {SCENARIO}" if SCENARIO else ""))
    log(f"  tasks   : {total}   already done: {n_done()} (SABER's own resume logic)")
    log("  NOTE: this is the slow, unimpeachable path (~24-36 h for 716 tasks).")
    lf = open(OUT / "logs" / "inference.log", "a")
    args = [sys.executable, "run_osbench.py", MODEL_SLUG] + ([SCENARIO] if SCENARIO else [])
    p = subprocess.run(args, cwd=SABER, stdout=lf, stderr=subprocess.STDOUT)
    lf.close()
    nbad = len(errored_results())
    log(f"INFERENCE COMPLETE: {n_done()}/{total} tasks, rc={p.returncode}, {nbad} errored")
    if nbad:
        log(f"  NOTE: {nbad} task(s) recorded an error. SABER treats these as 'Incapable'.")
        log("  Left as-is to stay faithful to SABER's behaviour. See logs/inference.log.")


def all_task_ids():
    """Every task id under the current filter, in SABER's own sorted order.

    SCENARIO may be empty (all), a scenario letter (A/B/C), or a single task id
    (e.g. B_code_002) -- the same three forms SABER's own CLI accepts.
    """
    if SCENARIO and "_" in SCENARIO:                     # single task id
        hit = glob.glob(str(SABER / "tasks" / "*" / "*" / f"{SCENARIO}.json"))
        return [Path(hit[0]).stem] if hit else []
    ids = []
    for sc in (["A", "B", "C"] if not SCENARIO else [SCENARIO]):
        d = SABER / "tasks" / sc
        if d.is_dir():
            for cat in sorted(x for x in d.iterdir() if x.is_dir()):
                ids += [f.stem for f in sorted(cat.glob("*.json"))]
    return ids


def done_task_ids():
    return {Path(f).stem for f in
            glob.glob(str(OUT / "results" / MODEL_SLUG / "**" / "*.json"), recursive=True)}


def run_inference_sharded():
    """OPT-IN FAST MODE (--shards N, N>1).

    Runs SABER's runner once per TASK using its own documented single-task CLI
    (`run_osbench.py <model> <task_id>`), N workers pulling from a shared queue.

    Task-level rather than (scenario, category)-level: the 24 category units are
    badly unbalanced (50 tasks vs 4, a 12.5x spread), so category sharding is
    bounded by the largest unit while other workers idle. A task queue balances
    perfectly and lifts the concurrency ceiling above 24.
    """
    purge_errored()
    todo = [t for t in all_task_ids() if t not in done_task_ids()]
    total = n_total()
    log(f"INFERENCE: SHARDED MODE — {total} tasks, {len(todo)} remaining, {SHARDS} workers")
    log("  (opt-in: faster, but adds parallel load SABER does not run under by default)")
    if not todo:
        log("  nothing to do — all tasks already complete")
        return

    logdir = OUT / "logs"
    queue, running = list(todo), []
    started = 0
    while queue or running:
        while queue and len(running) < SHARDS:
            tid = queue.pop(0)
            lf = open(logdir / f"infer_worker_{len(running)}.log", "a")
            p = subprocess.Popen([sys.executable, "run_osbench.py", MODEL_SLUG, tid],
                                 cwd=SABER, stdout=lf, stderr=subprocess.STDOUT)
            p._tag, p._lf = tid, lf
            running.append(p)
            started += 1
        time.sleep(5)
        for p in running[:]:
            if p.poll() is not None:
                p._lf.close()
                running.remove(p)
        if started % 25 == 0 or not queue:
            log(f"  progress {n_done()}/{total}  ({len(queue)} queued, {len(running)} running)")

    # Retry pass: errored tasks are purged and re-run once, sequentially, so a
    # transient timeout under parallel load cannot corrupt the denominator.
    bad = errored_results()
    if bad:
        log(f"RETRY: {len(bad)} task(s) errored — re-running them sequentially")
        purge_errored()
        retry = [t for t in all_task_ids() if t not in done_task_ids()]
        lf = open(OUT / "logs" / "retry.log", "a")
        for tid in retry:
            subprocess.run([sys.executable, "run_osbench.py", MODEL_SLUG, tid],
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
            log("  >2% failure rate — re-run with fewer shards, e.g. ./run.sh --shards 8")
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


def check_gate_health():
    """After inference, verify the guardrail stayed live for the whole run.

    A gate that starts erroring returns allow for everything, producing a
    'guarded' arm that is silently identical to bare. Never report such a run
    as clean.
    """
    if not FAILPROOF:
        return True
    f = OUT / "failproof_stats.json"
    if not f.exists():
        # No stats because the gate was never consulted -- e.g. every task was
        # already complete. Correct, not a failure. Only alarming if work ran.
        if n_done() >= n_total():
            log("  (no tool calls this run — all tasks already complete; nothing to verify)")
            return True
        log("WARNING: no failproof_stats.json — cannot confirm the gate stayed live.")
        return False
    try:
        st = json.load(open(f))
    except Exception:
        log("WARNING: failproof_stats.json unreadable."); return False
    if st.get("aborted"):
        log("=" * 70)
        log(f"FATAL: the gate aborted mid-run: {st.get('reason')}")
        log("  Results are INCOMPLETE. Fix the cause and re-run (completed tasks resume).")
        log("=" * 70)
        return False
    c = st.get("counts", {}); n = st.get("n_calls", 0)
    errs = c.get("error", 0)
    rate = errs / max(1, n)
    log(f"GATE HEALTH: {n} tool calls evaluated — "
        f"deny={c.get('deny',0)} instruct={c.get('instruct',0)} "
        f"sanitize={c.get('sanitize',0)} allow={c.get('allow',0)} errors={errs}")
    if rate > 0.02:
        log("=" * 70)
        log(f"FATAL: {errs}/{n} ({rate:.1%}) hook calls FAILED. On failure the gate")
        log("  allows the action, so this arm is partially unguarded and NOT valid.")
        log("=" * 70)
        return False
    if n == 0:
        # Fully-resumed run: every task was already complete, so the gate was
        # never consulted. Correct, not a failure.
        log("  (no tool calls this run — all tasks already complete; nothing to verify)")
        return True
    if c.get("deny", 0) + c.get("instruct", 0) + c.get("sanitize", 0) == 0:
        log("=" * 70)
        log(f"FATAL: the gate evaluated {n} tool calls and blocked NOTHING.")
        log("  That is indistinguishable from a bare run — treating as a failure.")
        log("=" * 70)
        return False
    return True


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
    n = s.get("total") or 0
    if hsr is not None:
        d = hsr - PUBLISHED["HSR"]
        A(f"  HSR gap vs published: {d:+.3f}")
        if n < 716:
            A(f"  >>> PARTIAL RUN — {n}/716 tasks. Not comparable to the published")
            A("      full-benchmark figure. Complete the run before interpreting.")
        elif abs(d) <= 0.03:
            A("  >>> CLOSE MATCH to the published result.")
        elif abs(d) <= 0.07:
            A("  >>> Within the measured judge-substitution offset (see note below).")
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
        "arm": ("failproofai builtins+custom+mcp" if (FAILPROOF and FP_CUSTOM)
                else "failproofai builtins (vanilla)" if FAILPROOF else "bare"),
        "failproof_custom": FP_CUSTOM,
        "failproof_enabled": FAILPROOF,
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
        reap_orphans()
        setup_failproof()
        if SHARDS <= 1:
            run_inference_pure()
        else:
            run_inference_sharded()
    if PHASE in ("all", "infer") and FAILPROOF:
        if not check_gate_health():
            sys.exit(8)
    if PHASE in ("all", "judge"):
        preflight() if PHASE == "judge" else None
        run_judge()
    if PHASE in ("all", "judge", "report"):
        report()
    log("DONE")
