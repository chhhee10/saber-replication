# SABER Replication — DeepSeek-V3.2

Reproduce the SABER paper's published operational-safety score for **DeepSeek-V3.2 (HSR 79.6%)** on your own machine, with one command.

Everything runs inside **SABER's own unmodified code**. This repository adds containerization and orchestration only — it does not touch inference, sandboxing, judging, or metric computation.

---

## Setup

**Requirements:** Docker (installed and running), ~10 GB free disk, internet access.
Nothing else — Python and every dependency live inside the container.

```bash
git clone <THIS_REPO_URL>
cd saber-replication
chmod +x run.sh

export PROXY_API_KEY='sk-...'        # the litellm proxy key
./run.sh
```

That's the whole setup. First run builds two Docker images (~5–8 min, cached afterwards), then starts.

**Results appear at `~/Desktop/saber-replication-output/`.**

---

## Runtime

**~24–36 hours** in the default pure-SABER mode (single sequential process, exactly as upstream runs it), plus ~1–2 h judging.

With the opt-in `--shards 6` speed-up: ~4–6 hours total. See *Execution mode* below.

Safe to **Ctrl-C at any time** and re-run: completed tasks are skipped, never redone. It also resumes cleanly after a reboot, so it can be left running unattended.

Check progress at any time from another terminal:

```bash
./run.sh --status
```

---

## Commands

```bash
./run.sh                    # full run: inference + judge + report
./run.sh --status           # progress so far (safe while a run is in flight)
./run.sh --scenario B       # only scenario B (186 tasks) — quick partial run
./run.sh --shards 6         # OPT-IN speed-up (default is 1 = pure SABER)
./run.sh --judge-only       # re-judge existing results, no re-inference
./run.sh --report-only      # re-print the comparison report
```

### Execution mode

**By default this runs SABER exactly as its authors do** — a single sequential process:

```
python3 run_osbench.py <model>      # SABER's own RUNNING.md, verbatim
python3 judge_osbench.py <model>    # SABER's own RUNNING.md, verbatim
```

No sharding, no parallelism, no retry logic, no interference of any kind. Nothing of
ours is in the execution path. This is the slow but **unimpeachable** path: ~24-36 h
for all 716 tasks.

If you need it faster, `--shards 6` is available as an **opt-in**. It runs SABER's same
runner once per `(scenario, category)` using SABER's *own* documented CLI arguments,
six at a time. Same code, same tasks, same judge — only the process layout differs,
cutting the run to ~4 h.

The trade-off, stated plainly: SABER applies a hard `timeout=10` when starting each
task's Docker container. Under parallel load that can occasionally be exceeded, and a
task that fails to start is recorded with an `error`, which SABER's judge classifies as
`Incapable` — **removing it from the HSR denominator and shifting the result.** In
sharded mode the pipeline purges and retries such tasks, then reports any survivors as
`exec errors: N` in the report. In default (pure) mode none of that machinery runs at
all, because there is no added load to protect against.

**For the run that matters, use the default.**

---

## What you get

```
~/Desktop/saber-replication-output/
├── REPLICATION_REPORT.txt      ← THE ANSWER: our numbers vs the paper's
├── MANIFEST.json               ← exact run conditions (models, task checksum, versions)
├── results/ds32_repro/         ← raw trajectories, one JSON per task
├── judged/ds32_repro/          ← per-task verdicts + summary.json
└── logs/                       ← per-shard inference logs, judge log
```

The report prints a side-by-side table against SABER Table 3:

| Metric | Published (DeepSeek-V3.2) |
|---|---|
| HSR (overall) | 0.796 |
| HSR_A / HSR_B / HSR_C | 0.733 / 0.748 / 0.902 |
| CPR | 0.248 |
| Incapable Rate | 0.138 |

---

## How to read the result

**Expect roughly 0.72–0.75, not exactly 0.796 — and that still means the replication succeeded.** Two reasons, both documented up front:

**1. The judge model differs (forced).** The paper judged with `claude-opus-4-6`. That model is listed on our proxy but **is not invokable** — AWS Bedrock rejects it without an inference-profile ARN:

```
"Invocation of model ID anthropic.claude-opus-4-6-v1 with on-demand
 throughput isn't supported. Retry with the ID or ARN of an inference profile."
```

We use `claude-sonnet-4-6` instead. We measured the difference directly by judging **the paper authors' own trajectories** with both: Sonnet is about **7 HSR points more lenient**, with **86.7% verdict agreement** on identical inputs.

**2. k=1 sampling.** Each task runs once and models are stochastic. SABER sets no temperature or seed — that is the paper's condition too — so ±2–3 points of run-to-run variance is expected.

The report classifies the outcome automatically (close match / partial match / divergent).

> **For an exact-condition replication:** ask the proxy admin to repoint `claude-opus-4-6` in litellm's `config.yaml` from `bedrock/anthropic.claude-opus-4-6-v1` to `bedrock/us.anthropic.claude-opus-4-6-v1` (a regional inference profile). Then run `JUDGE_MODEL=claude-opus-4-6 ./run.sh --judge-only` to re-score the same trajectories with the paper's judge — no re-inference needed, ~$21.

---

## Is this really SABER, unmodified?

Yes, and it is verifiable. Every SABER file here is **byte-identical to upstream**:

| File | Lines | Status |
|---|---|---|
| `saber/run_osbench.py` — inference engine | 532 | unmodified |
| `saber/judge_osbench.py` — judging + all metrics | 977 | unmodified |
| `saber/sandbox_shell.py` — Docker sandbox | 562 | unmodified |
| `saber/task_runtime.py`, `saber/mcp_runtime.py` | 136 / 210 | unmodified |
| `saber/tasks/**` — all 716 tasks | — | unmodified |

Check for yourself against [the upstream repo](https://github.com/sssrlab/saber):

```bash
diff -r /path/to/upstream/saber/tasks saber/tasks
diff /path/to/upstream/saber/run_osbench.py saber/run_osbench.py
```

**The entire measurement path is two SABER commands**, both straight from their `RUNNING.md`:

```
run_osbench.py   <model> <scenario> <category>
judge_osbench.py <model>
```

Our `pipeline.py` writes `config.json` (SABER requires this — it's step 1 of their own setup), runs those two commands, and reads the `summary.json` SABER produces. **It never computes or adjusts a metric.** Every number in the report comes verbatim from SABER's own output.

Parallelism uses SABER's *own* `<scenario> <category>` CLI arguments to run 6 shards — no code changes, just 6 invocations of their runner instead of 1.

---

## Consistency across machines

Every run writes `MANIFEST.json`:

```json
{
  "agent_model": "deepseek-v3.2",
  "judge_model": "claude-sonnet-4-6",
  "max_steps": 30,
  "task_count": 716,
  "task_set_sha256": "d43224fe6007279f",
  "sampling": "provider default (SABER sets no temperature/seed) — k=1",
  "python": "3.12.13"
}
```

Two runs with matching manifests are directly comparable. Pinned in the container: Python 3.12, `httpx==0.28.1`, `anthropic==0.120.0`, `max_steps=30`, the full 716-task set.

---

## Cost

Roughly **$20 per full run** — about **$7** for inference (DeepSeek-V3.2 is cheap) and **~$13** for judging with Sonnet. Approximately 10,300 inference calls and 735 judge calls.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Docker is not running` | Start Docker Desktop, or `sudo systemctl start docker` |
| `PROXY_API_KEY is not set` | `export PROXY_API_KEY='sk-...'` before running |
| Sandbox image build fails | The upstream Dockerfile uses Aliyun mirrors (fast in China). Outside China, edit `saber/Dockerfile.sandbox` and delete the `mirrors.aliyun.com` `sed` line. |
| `FATAL: N judge calls FAILED` | Deliberate guard. SABER's judge silently falls back to "Incapable" on API errors, which would yield a wrong-but-plausible summary. Check `logs/judge.log` — usually key or model access. |
| Start completely clean | `rm -rf ~/Desktop/saber-replication-output` |

---

## Credits

**SABER** — *SABER: Benchmarking Operational Safety of LLM Coding Agents in Stateful Project Workspaces*, arXiv:[2606.01317](https://arxiv.org/abs/2606.01317). Copyright 2026 The OSBench Authors. Task set, sandbox runtime, and judge are theirs and used unmodified.

Redistributed under upstream terms — code under Apache-2.0, task data under CC BY 4.0. See [`NOTICE`](NOTICE) and [`saber/LICENSES/`](saber/LICENSES/).
