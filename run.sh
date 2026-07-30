#!/usr/bin/env bash
# ============================================================================
#  SABER replication — DeepSeek-V3.2
#  ONE COMMAND.  Results land on your Desktop.
#
#    ./run.sh                     full run  (inference + judge + report)
#    ./run.sh --status            progress so far
#    ./run.sh --judge-only        re-judge existing results
#    ./run.sh --report-only       re-print the comparison report
#    ./run.sh --scenario B        only scenario B (smaller test run)
#    ./run.sh --task B_code_001   single task (~90s smoke test)
#    ./run.sh --failproof         GUARDED arm: vanilla failproofai builtins
#    ./run.sh --failproof --with-custom   + custom & MCP policies (later study)
#    ./run.sh --shards 6          OPT-IN speed-up (default 1 = pure SABER)
#
#  Safe to Ctrl-C and re-run — completed tasks are skipped, never redone.
# ============================================================================
set -uo pipefail

OUTPUT_DIR="${SABER_OUTPUT_DIR:-$HOME/Desktop/saber-replication-output}"
# (guarded arm redirects below, after flag parsing)
RUNNER_IMAGE="saber-replication-runner"
SANDBOX_IMAGE="osbench-sandbox"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT_MODEL="${AGENT_MODEL:-deepseek-v3.2}"
JUDGE_MODEL="${JUDGE_MODEL:-claude-sonnet-4-6}"
PROXY_BASE_URL="${PROXY_BASE_URL:-https://models.aikin.club}"
MODEL_SLUG="${MODEL_SLUG:-ds32_repro}"
SHARDS="${SHARDS:-1}"   # 1 = PURE SABER (single process, exactly as upstream runs it)
PHASE="all"; SCENARIO=""; FAILPROOF="0"; FP_CUSTOM="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --status)      PHASE="status";  shift ;;
    --judge-only)  PHASE="judge";   shift ;;
    --report-only) PHASE="report";  shift ;;
    --scenario)    SCENARIO="$2";   shift 2 ;;
    --task)        SCENARIO="$2";   shift 2 ;;   # single task id, e.g. B_code_001
    --failproof)   FAILPROOF="1"; MODEL_SLUG="${MODEL_SLUG_FP:-ds32_failproof}"; shift ;;
    --with-custom) FP_CUSTOM="1"; shift ;;
    --shards)      SHARDS="$2";     shift 2 ;;
    -h|--help)     sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$FAILPROOF" == "1" && -z "${SABER_OUTPUT_DIR:-}" ]]; then
  OUTPUT_DIR="$HOME/Desktop/saber-failproof-output"
fi

c() { printf "\033[1;36m%s\033[0m\n" "$*"; }
ok(){ printf "  \033[0;32m✓\033[0m %s\n" "$*"; }
er(){ printf "  \033[0;31m✗\033[0m %s\n" "$*"; }

# ---------------------------------------------------------------- status
if [[ "$PHASE" == "status" ]]; then
  c "SABER replication — status"
  R="$OUTPUT_DIR/results/$MODEL_SLUG"; J="$OUTPUT_DIR/judged/$MODEL_SLUG"
  n=$(find "$R" -name '*.json' 2>/dev/null | wc -l)
  j=$(find "$J" -name '*.json' 2>/dev/null | grep -v summary | wc -l)
  echo "  inference : $n / 716 tasks"
  echo "  judged    : $j"
  if docker ps --format '{{.Image}}' 2>/dev/null | grep -q "$RUNNER_IMAGE"; then
    ok "runner is ACTIVE"; else echo "  runner    : not running"; fi
  [[ -f "$OUTPUT_DIR/REPLICATION_REPORT.txt" ]] && { echo; cat "$OUTPUT_DIR/REPLICATION_REPORT.txt"; }
  exit 0
fi

# ---------------------------------------------------------------- preflight
c "SABER replication — DeepSeek-V3.2"
echo
c "[1/4] Preflight"

if ! command -v docker >/dev/null 2>&1; then
  er "Docker is not installed. Install Docker Desktop / docker-ce and re-run."; exit 1; fi
if ! docker info >/dev/null 2>&1; then
  er "Docker is installed but not running (or needs sudo). Start Docker and re-run."; exit 1; fi
ok "docker available"

# Load .env if present. Preferred over `export` for unattended runs: nohup,
# cron and systemd do not source your shell profile, so an exported variable
# can silently be absent. Already in .gitignore, so it cannot be committed.
# An already-exported PROXY_API_KEY takes precedence over the file.
if [[ -f "$HERE/.env" ]]; then
  _preset="${PROXY_API_KEY:-}"
  set -a; # shellcheck disable=SC1091
  source "$HERE/.env"; set +a
  [[ -n "$_preset" ]] && PROXY_API_KEY="$_preset"
  ok "loaded $HERE/.env"
fi

if [[ -z "${PROXY_API_KEY:-}" ]]; then
  er "PROXY_API_KEY is not set."
  echo
  echo "     Either put it in a .env file next to run.sh (recommended — survives"
  echo "     nohup/cron, and is gitignored):"
  echo
  echo "       printf \"PROXY_API_KEY='sk-...'\\n\" > .env && chmod 600 .env"
  echo "       ./run.sh"
  echo
  echo "     ...or export it for the current shell only:"
  echo "       export PROXY_API_KEY='sk-...'"
  exit 1
fi
ok "API key present"

mkdir -p "$OUTPUT_DIR"
ok "output dir: $OUTPUT_DIR"

FREE_GB=$(df -BG --output=avail "$OUTPUT_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)
[[ -n "$FREE_GB" && "$FREE_GB" -lt 10 ]] && er "WARNING: only ${FREE_GB}GB free — recommend 10GB+"
ok "disk space checked"

# ---------------------------------------------------------------- images
c "[2/4] Building images (first run only; cached afterwards)"

if [[ -z "$(docker images -q $SANDBOX_IMAGE 2>/dev/null)" ]]; then
  echo "  building $SANDBOX_IMAGE (task sandbox, ~3-6 min)..."
  # Built on the HOST: task containers are spawned as siblings, not nested.
  if ! docker build -q -t "$SANDBOX_IMAGE" -f "$HERE/saber/Dockerfile.sandbox" "$HERE/saber" >/dev/null; then
    er "sandbox image build failed."
    echo "     If you are outside China the Aliyun mirrors may be unreachable; edit"
    echo "     saber/Dockerfile.sandbox and remove the 'mirrors.aliyun.com' sed line."
    exit 1
  fi
fi
ok "$SANDBOX_IMAGE ready"

# ALWAYS rebuild the runner. It bakes in pipeline.py / failproof_shim.py / the
# policy files, so a cached image silently runs STALE CODE after a git pull --
# which is how a `--failproof` run can come out bare with no error at all.
# Docker's layer cache makes this near-instant when nothing has changed.
echo "  building $RUNNER_IMAGE (cached if unchanged)..."
if ! docker build -q -t "$RUNNER_IMAGE" -f "$HERE/Dockerfile.runner" "$HERE" >/dev/null; then
  er "runner build failed"; exit 1
fi
ok "$RUNNER_IMAGE ready (up to date with local source)"

# ---------------------------------------------------------------- run
c "[3/4] Running pipeline"
case "$PHASE" in
  all)    if [[ "$SHARDS" -le 1 ]]; then
            echo "  PURE SABER MODE: single process, sequential (~24-36h) then judge (~1-2h)"
            echo "  (use --shards 6 to parallelise; see README)"
          else
            echo "  SHARDED MODE: ${SHARDS} workers (~4-6h) then judge (~1-2h)"
          fi;;
  judge)  echo "  judge only";;
  report) echo "  report only";;
esac
echo "  Ctrl-C is safe — progress is saved and resumed."
echo

# Only allocate a TTY when one actually exists. Without this, running the
# script under nohup / screen / a cron job / redirected output fails with
# "cannot attach stdin to a TTY-enabled container" — i.e. every unattended run.
DOCKER_TTY=""
if [[ -t 0 && -t 1 ]]; then DOCKER_TTY="-it"; fi

docker run --rm $DOCKER_TTY \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$OUTPUT_DIR":/output \
  -e PROXY_API_KEY="$PROXY_API_KEY" \
  -e PROXY_BASE_URL="$PROXY_BASE_URL" \
  -e AGENT_MODEL="$AGENT_MODEL" \
  -e JUDGE_MODEL="$JUDGE_MODEL" \
  -e MODEL_SLUG="$MODEL_SLUG" \
  -e SHARDS="$SHARDS" \
  -e SCENARIO="$SCENARIO" \
  -e PHASE="$PHASE" \
  -e FAILPROOF="$FAILPROOF" \
  -e FP_CUSTOM="$FP_CUSTOM" \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  --name saber-replication-run \
  "$RUNNER_IMAGE"
RC=$?

echo
c "[4/4] Done"
if [[ $RC -eq 0 ]]; then
  ok "pipeline finished"
  echo "  results : $OUTPUT_DIR"
  echo "  report  : $OUTPUT_DIR/REPLICATION_REPORT.txt"
else
  er "pipeline exited with code $RC — see $OUTPUT_DIR/logs/"
fi
exit $RC
