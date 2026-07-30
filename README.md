# SABER Replication — DeepSeek-V3.2

Reproduce the SABER paper's published operational-safety score for **DeepSeek-V3.2 (HSR 79.6%)** on your own machine, with one command.

The **bare arm runs inside SABER's own unmodified code** — this repository adds containerization and orchestration only, and never touches inference, sandboxing, judging, or metric computation.

An optional **guarded arm** (`--failproof`) additionally runs failproofai at the tool-call boundary, to measure what a runtime guardrail does to the same benchmark. That arm adds one clearly-marked block to `task_runtime.py`; **the judge and all metrics remain untouched in both arms.** See *Is this really SABER, unmodified?* below.

---

## Setup

**Requirements:** Docker (installed and running), ~10 GB free disk, internet access.
Nothing else — Python and every dependency live inside the container.

```bash
git clone <THIS_REPO_URL>
cd saber-replication
chmod +x run.sh

cp .env.example .env && chmod 600 .env    # then put the proxy key in it
./run.sh
```

**Why `.env` rather than `export`:** `nohup`, `cron` and `systemd` do not source your
shell profile, so an exported variable can be silently missing from an unattended
run. `run.sh` loads `.env` automatically. It is gitignored, so it cannot be
committed. An already-exported `PROXY_API_KEY` still takes precedence, so you can
override it for a one-off run.

That's the whole setup. First run builds two Docker images (~5–8 min, cached afterwards), then starts.

**Results appear at `~/Desktop/saber-replication-output/`.**

---

## Runtime

**~24–36 hours** in the default pure-SABER mode (single sequential process, exactly as
upstream runs it), plus ~1–2 h judging.

With the opt-in `--shards 24` speed-up: **~2.5 hours total**, measured on a 716-task
run with zero execution errors. See *Execution mode* below.

Safe to **Ctrl-C at any time** and re-run: completed tasks are skipped, never redone. It also resumes cleanly after a reboot, so it can be left running unattended.

Check progress at any time from another terminal:

```bash
./run.sh --status
```

---

## What it actually does

| Phase | Duration | What happens |
|---|---|---|
| **1. Setup** | ~5–8 min *(first run only)* | Verifies Docker, the API key and disk space, then builds two images: `osbench-sandbox` (the Ubuntu box each task runs in — SABER's own Dockerfile) and the runner (Python 3.12 + failproofai + Docker CLI). Cached afterwards. |
| **2. Safety checks** | ~30 sec | Clears containers orphaned by any previous crashed run. Pings both models to confirm they respond **before** spending anything. In the guarded arm, installs the policies and runs the **fire test** — aborts here rather than running for a day unguarded. |
| **3. Inference** | **~24–36 h** | Runs SABER's own `run_osbench.py` over all 716 tasks, one at a time (see below). |
| **4. Judging** | ~1–2 h | Runs SABER's own `judge_osbench.py`. The judge model reads all 716 transcripts and decides per task: was anything harmful, and did the agent complete / refuse / fail — producing SABER's six outcome classes and the HSR. |
| **5. Report** | instant | Writes `REPLICATION_REPORT.txt` — your numbers beside the paper's. |

**What happens per task during inference:**

1. A **fresh Docker container** starts, populated with that task's project files
2. The agent model is given the task (e.g. *"clean up the database"*) and begins issuing shell commands
3. *(guarded arm only)* **failproofai inspects each command before it executes** — `deny` means it never runs and the agent is told why; `instruct` means it runs with a warning injected; `allow` passes through. Every decision is appended to `failproof_audit.jsonl`
4. The full transcript is saved and the container destroyed

At roughly 2.5–3 min per task, expect **~20–24 tasks/hour**. The log is mostly SABER's own output — one block per task, 716 times.

**Where the money goes:** ~$7 on inference (the 716 task runs), ~$13 on judging. failproofai itself is free — it runs locally, no API.

---

## Commands

```bash
./run.sh                    # BARE arm: full run (inference + judge + report)
./run.sh --failproof        # GUARDED arm: + vanilla failproofai builtins (34)
./run.sh --status           # progress so far (safe while a run is in flight)
./run.sh --scenario B       # only scenario B (186 tasks) — partial run
./run.sh --task B_code_001  # a single task (~90s) — quick smoke test
./run.sh --shards 24        # OPT-IN speed-up: ~2.5h instead of ~24-36h
                            #   (24 = the number of scenario/category units;
                            #    higher values simply cap at 24)
./run.sh --judge-only       # re-judge existing results, no re-inference
./run.sh --report-only      # re-print the comparison report

# Third arm, for a later study — not needed for the vanilla comparison:
./run.sh --failproof --with-custom   # + custom shell & MCP policies
```

Run it unattended with `nohup ./run.sh --failproof --shards 24 > fp.log 2>&1 &`, then
check in with `./run.sh --status` from another terminal.

### Execution mode

**By default this runs SABER exactly as its authors do** — a single sequential process:

```
python3 run_osbench.py <model>      # SABER's own RUNNING.md, verbatim
python3 judge_osbench.py <model>    # SABER's own RUNNING.md, verbatim
```

No sharding, no parallelism, no retry logic, no interference of any kind. Nothing of
ours is in the execution path. This is the slow but **unimpeachable** path: ~24-36 h
for all 716 tasks.

If you need it faster, `--shards 24` is available as an **opt-in**. It runs SABER's same
runner once per `(scenario, category)` using SABER's *own* documented CLI arguments,
several at a time. Same code, same tasks, same judge — only the process layout differs,
cutting a 716-task run to **~2.5 h** (measured, with zero execution errors on a
server-class machine). There are 24 scenario/category units in total, so values above
24 simply cap there.

The trade-off, stated plainly: SABER applies a hard `timeout=10` when starting each
task's Docker container. Under parallel load that can occasionally be exceeded, and a
task that fails to start is recorded with an `error`, which SABER's judge classifies as
`Incapable` — **removing it from the HSR denominator and shifting the result.** In
sharded mode the pipeline purges and retries such tasks, then reports any survivors as
`exec errors: N` in the report. In default (pure) mode none of that machinery runs at
all, because there is no added load to protect against.

**For the run that matters, use the default.**

---

### Running the guarded arm (failproofai)

```bash
./run.sh --failproof              # vanilla failproofai builtins (34 policies)
```

Same benchmark, same tasks, same judge — the only difference is that failproofai
evaluates every tool call before it executes. Output goes to a **separate**
directory (`~/Desktop/saber-failproof-output/`) so it can never overwrite the bare run.

**Before any work starts, a mandatory fire test proves the guardrail is actually
live.** If it fails, the run aborts. This is not ceremonial: we verified that a
one-character difference in the tool name (`bash` vs `Bash`) makes failproofai
return an empty decision — i.e. allow everything — which would produce a guarded
run byte-identical to the bare one while every log looked healthy.

```
  fire test — proving the guardrail actually fires:
    [PASS] sudo is blocked              -> deny
    [PASS] catastrophic rm blocked      -> deny
    [PASS] curl-pipe-shell blocked      -> deny
    [PASS] benign ls allowed            -> allow
    [PASS] benign cat allowed           -> allow
    [PASS] secret redacted from output  -> sanitize
    [PASS] benign output untouched      -> pass-through
  fire test PASSED — guardrail is live.
```

Every intercepted call is logged to `failproof_audit.jsonl` with its decision and
reason, so the guardrail's behaviour is fully auditable after the fact.

**Continuous verification during the run.** A guardrail that starts failing returns
"allow" for everything, which is indistinguishable from a bare run — over an
unattended overnight run that silently destroys the arm while every log looks
healthy. Three safeguards prevent that:

| Safeguard | Behaviour |
|---|---|
| **Circuit breaker** | 10 consecutive hook failures → abort (exit 7) rather than limp on unguarded |
| **Periodic health check** | every 250 tool calls, re-prove a known-bad command is still denied |
| **Post-run audit** | verify error rate < 2% and that the gate blocked *something*; refuse to write a report otherwise (exit 8) |

Counts are persisted atomically to `failproof_stats.json` and flushed on exit, so
they survive a hard kill. A fully-resumed run (nothing left to do) is correctly
treated as success, not a failure.

### The optional third arm (`--with-custom`)

Not used in the vanilla comparison, but included for a follow-up study. It adds
two custom policy files on top of the builtins:

| File | Covers |
|---|---|
| `custom_policy_b64.txt` | 8 shell policies — credential reads, cron/persistence, reverse shells, destructive DB CLI, mass deletion, build-file tampering, git-history rewrite, data-file overwrite |
| `policies/mcp-harms.policies.mjs` | 5 MCP policies — destructive verbs (purge/wipe/reclaim), bulk export (`export_full`/`export_raw`), privilege escalation (`admin_session`/`root`), persistence (`add_server`), live-target writes |

The MCP policies exist because **the builtins are shell-oriented and never fire on
MCP tool calls at all** — a genuine coverage gap. They match on *verbs* rather
than a list of known-bad tool names, so they generalise to MCP servers they have
never seen (validated 14/14, including two unseen-server checks and six
false-positive controls on benign read-only tools).

Both sets were authored from **observed agent behaviour in the bare run**, following
failproofai's observe → identify → enforce loop — *not* from SABER's
`ground_truth.harmful_tool_patterns`, which would be copying the answer key. They
are nonetheless in-domain (same corpus they are evaluated on), which any write-up
should disclose.

**Known behaviour to expect.** The `block-read-outside-cwd` builtin scans the whole
command string — including heredoc *content* — for path-like tokens. In practice it
denies some legitimate file writes: a `#!/bin/bash` shebang inside a heredoc, a
Makefile glob like `src/*.c`, or prose containing a slash. In a smoke test 5 of 26
tool calls were denied this way. Expect an elevated `Incapable` rate in the guarded
arm as a result. We are running the builtins **exactly as shipped** rather than
tuning them, so this is reported as a finding rather than configured away.

---

## What you get

Each arm writes to its **own directory**, so they can never overwrite each other:

```
~/Desktop/saber-replication-output/      ← BARE arm  (./run.sh)
├── REPLICATION_REPORT.txt      ← THE ANSWER: our numbers vs the paper's
├── MANIFEST.json               ← exact run conditions (models, task checksum, versions)
├── results/ds32_repro/         ← raw trajectories, one JSON per task
├── judged/ds32_repro/          ← per-task verdicts + summary.json
└── logs/                       ← inference log, judge log

~/Desktop/saber-failproof-output/        ← GUARDED arm  (./run.sh --failproof)
├── REPLICATION_REPORT.txt
├── MANIFEST.json               ← records which arm ran (vanilla / +custom)
├── failproof_audit.jsonl       ← EVERY intercepted tool call: command, decision, reason
├── failproof_stats.json        ← final gate counts (deny / instruct / sanitize / allow / errors)
├── results/ds32_failproof/
├── judged/ds32_failproof/
└── logs/
```

To compare the two arms, put `judged/*/summary.json` from each side by side —
`HSR`, the `termination_counts`, and `Incapable_Rate` are the fields that matter.
`failproof_audit.jsonl` shows exactly what the guardrail stopped and why.

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

The report only renders a verdict on a complete 716-task run. On a partial run it states
`PARTIAL RUN — N/716 tasks, not comparable` rather than comparing an incomplete sample
against the published full-benchmark figure.

> **For an exact-condition replication:** ask the proxy admin to repoint `claude-opus-4-6` in litellm's `config.yaml` from `bedrock/anthropic.claude-opus-4-6-v1` to `bedrock/us.anthropic.claude-opus-4-6-v1` (a regional inference profile). Then run `JUDGE_MODEL=claude-opus-4-6 ./run.sh --judge-only` to re-score the same trajectories with the paper's judge — no re-inference needed, ~$21.

---

## Is this really SABER, unmodified?

Yes, and it is verifiable. Every SABER file here is **byte-identical to upstream**:

| File | Status |
|---|---|
| `saber/run_osbench.py` — inference engine | **unmodified** |
| `saber/judge_osbench.py` — judging + **all metrics** | **unmodified** |
| `saber/sandbox_shell.py` — Docker sandbox | **unmodified** |
| `saber/mcp_runtime.py` | **unmodified** |
| `saber/tasks/**` — all 716 tasks | **unmodified** |
| `saber/task_runtime.py` | **one added block** — see below |

**The one exception, stated plainly.** `task_runtime.py` carries a single added
block in `execute_tool()` that calls the failproofai gate before a tool runs.
Inserting a guardrail at the tool-call boundary *is* the experiment, so it cannot
be avoided — but it is scoped as tightly as possible:

- it is **one contiguous, clearly-marked block**, auditable with a single `diff`
- when no gate is configured it is `None`, so the **bare arm is byte-identical to
  upstream behaviour**
- **the judge, the tasks, the sandbox and the agent loop are untouched** — scoring
  remains 100% SABER

`failproof_shim.py` (the adapter that maps SABER's tool names to failproofai's and
plumbs the decisions) is **our code, not a failproofai feature** — SABER is not a
supported host for it.

Check for yourself against [the upstream repo](https://github.com/sssrlab/saber):

```bash
git clone https://github.com/sssrlab/saber /tmp/saber-upstream
diff -r /tmp/saber-upstream/tasks saber/tasks              # expect: no differences
diff /tmp/saber-upstream/judge_osbench.py saber/judge_osbench.py
diff /tmp/saber-upstream/run_osbench.py   saber/run_osbench.py
diff /tmp/saber-upstream/task_runtime.py  saber/task_runtime.py   # the one added block
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
