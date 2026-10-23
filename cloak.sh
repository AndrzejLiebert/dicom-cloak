#!/usr/bin/env bash
# DICOM Cloak launcher for macOS and Linux (Windows: cloak.bat).
#
#   bash cloak.sh                         asks for the input and output folders (drag and drop works)
#   bash cloak.sh INPUT_FOLDER OUTPUT_FOLDER
#   bash cloak.sh check FOLDER            re-run the leak check on a de-identified folder
#   bash cloak.sh selftest                run the built-in tests inside the container
#
# Options (before the folders):
#   --secrets DIR   folder for the secret files (default: ~/dicom_cloak_secrets)
#   --prefix TEXT   start of automatic pseudonyms (default: SUBJ_ -> SUBJ_0001, SUBJ_0002, ...)
#   --ctp           use the RSNA CTP engine instead of the Python engine
#   --rebuild       rebuild the Docker image (after updating this folder)
#
# The container runs without network access. The input folder is mounted read-only.
set -u

HERE=$(cd "$(dirname "$0")" && pwd -P)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HERE/src/dicom_cloak/__init__.py")
SECRETS=${DICOM_CLOAK_SECRETS:-"$HOME/dicom_cloak_secrets"}
PREFIX=${DICOM_CLOAK_PREFIX:-SUBJ_}
ENGINE=python
REBUILD=0

say()  { printf '%s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- arguments
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --secrets) [ $# -ge 2 ] || fail "--secrets needs a folder"; SECRETS=$2; shift 2 ;;
    --prefix)  [ $# -ge 2 ] || fail "--prefix needs a text"; PREFIX=$2; shift 2 ;;
    --ctp)     ENGINE=ctp; shift ;;
    --rebuild) REBUILD=1; shift ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         ARGS+=("$1"); shift ;;
  esac
done
set -- ${ARGS[@]+"${ARGS[@]}"}

if [ "$ENGINE" = ctp ]; then IMAGE="dicom-cloak:$VERSION-ctp"; else IMAGE="dicom-cloak:$VERSION"; fi
case "$PREFIX" in
  *[!A-Za-z0-9_-]*|"") fail "--prefix may only contain letters, digits, _ and -" ;;
esac

# Paths typed or dragged into a terminal may come quoted ('...' or "...") or with escaped spaces (My\ Data).
clean_path() {
  p=$(printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  case "$p" in
    \'*\') p=${p#\'}; p=${p%\'} ;;
    \"*\") p=${p#\"}; p=${p%\"} ;;
  esac
  case "$p" in *\\*) p=$(printf '%s' "$p" | sed 's/\\\(.\)/\1/g') ;; esac
  case "$p" in "~"|"~/"*) p="$HOME${p#\~}" ;; esac
  printf '%s' "$p"
}

ask() {
  printf '\n%s\n> ' "$1" >&2
  IFS= read -r answer || exit 1
  clean_path "$answer"
}

abspath() { (cd "$1" 2>/dev/null && pwd -P); }

resolve() {  # absolute path of $1, also for a folder that does not exist yet (creates nothing)
  p=$1; rest=""
  case "$p" in /*) ;; *) p="$PWD/$p" ;; esac
  while [ ! -d "$p" ]; do rest="/$(basename "$p")$rest"; p=$(dirname "$p"); done
  p=$(cd "$p" && pwd -P)
  printf '%s%s' "${p%/}" "$rest"
}

inside() {  # inside A B: true if folder A is B or lies inside B
  case "$1/" in "$2"/*) return 0 ;; esac
  return 1
}

# ---------------------------------------------------------------- Docker
need_docker() {
  command -v docker >/dev/null 2>&1 || fail "Docker is not installed.
       Install Docker Desktop (macOS) or Docker Engine (Linux) as described in README.md, then try again."
  if ! out=$(docker info 2>&1); then
    case "$out" in
      *"permission denied"*) fail "Docker is installed but your user may not use it.
       Linux: run  sudo usermod -aG docker \$USER , log out and in again, then try again." ;;
      *) fail "Docker is installed but not running.
       Start Docker Desktop (macOS) or the Docker service (Linux), wait until it is ready, then try again." ;;
    esac
  fi
  if [ "$REBUILD" = 1 ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    say ""
    say "Building the Docker image $IMAGE (first use only, needs internet, takes a few minutes) ..."
    docker build -f "$HERE/docker/Dockerfile" --target "$ENGINE" -t "$IMAGE" "$HERE" \
      || fail "Building the Docker image failed (see the messages above). Check the internet connection."
  fi
}

user_opt() {  # write output files as the calling user (Linux); harmless on macOS
  printf '%s' "--user=$(id -u):$(id -g)"
}

check_cmd() {
  docker run --rm --network none "$(user_opt)" -v "$1":/out:ro -v "$HERE/config":/opt/dicom_cloak/config:ro \
    --entrypoint dicom-cloak-check "$IMAGE" /out --require-remapped-uids
}

# ---------------------------------------------------------------- commands
if [ "${1:-}" = selftest ]; then
  need_docker
  say "Running the built-in tests in $IMAGE ..."
  if docker run --rm --network none --entrypoint python "$IMAGE" -m pytest -q -p no:cacheprovider tests; then
    say ""; say "SELFTEST PASSED"; exit 0
  fi
  say ""; say "SELFTEST FAILED"; exit 1
fi

if [ "${1:-}" = check ]; then
  [ $# -eq 2 ] || fail "usage: bash cloak.sh check FOLDER"
  DIR=$(abspath "$(clean_path "$2")") || fail "folder not found: $2"
  need_docker
  if check_cmd "$DIR"; then say ""; say "CHECK PASSED: no identifying information found in the headers."; exit 0; fi
  say ""; say "CHECK FAILED: see the list above. Do not share this folder."; exit 1
fi

[ $# -le 2 ] || fail "too many arguments (put folder paths with spaces in quotes). See: bash cloak.sh --help"

say "DICOM Cloak $VERSION (MAGIC Lab): de-identification of MRI DICOM data"
IN=${1:-}
OUT=${2:-}
[ -n "$IN" ] || IN=$(ask "Drag the INPUT folder (the original DICOM data) into this window and press Enter:")
[ -n "$OUT" ] || OUT=$(ask "Drag or type the OUTPUT folder (new or empty) for the de-identified copy and press Enter:")
IN=$(clean_path "$IN"); OUT=$(clean_path "$OUT")
[ -n "$IN" ] && [ -n "$OUT" ] || fail "input and output folder are both needed"

IN_ABS=$(abspath "$IN") || fail "input folder not found: $IN"
OUT_ABS=$(resolve "$OUT")
SEC_ABS=$(resolve "$SECRETS")

inside "$OUT_ABS" "$IN_ABS" && fail "the output folder must not be inside the input folder"
inside "$IN_ABS" "$OUT_ABS" && fail "the input folder must not be inside the output folder"
for d in "$IN_ABS" "$OUT_ABS"; do
  { inside "$SEC_ABS" "$d" || inside "$d" "$SEC_ABS"; } && fail "the secrets folder ($SEC_ABS) must be separate from the input and output folders"
done
if [ -d "$OUT_ABS" ] && [ -n "$(ls -A "$OUT_ABS" | grep -v '^\.DS_Store$')" ]; then
  fail "the output folder is not empty: $OUT_ABS
       Choose a new or empty folder for every run."
fi
mkdir -p "$OUT_ABS" 2>/dev/null || fail "cannot create the output folder: $OUT_ABS"
mkdir -p "$SEC_ABS" 2>/dev/null || fail "cannot create the secrets folder: $SEC_ABS"
chmod 700 "$SEC_ABS" 2>/dev/null

need_docker

mkdir -p "$SEC_ABS/logs" && chmod 700 "$SEC_ABS/logs" 2>/dev/null
LOG="$SEC_ABS/logs/run_$(date +%Y%m%d_%H%M%S).txt"
say ""
say "Input:   $IN_ABS (read-only)"
say "Output:  $OUT_ABS"
say "Secrets: $SEC_ABS (key, pseudonym list, visit registry, logs: keep private)"
say "Engine:  $ENGINE"
say ""

EXTRA=""
[ "$ENGINE" = ctp ] && EXTRA="--work /tmp/ctp_work"
# shellcheck disable=SC2086
docker run --rm --network none "$(user_opt)" \
  -v "$IN_ABS":/in:ro -v "$OUT_ABS":/out -v "$SEC_ABS":/secrets -v "$HERE/config":/opt/dicom_cloak/config:ro \
  "$IMAGE" run --src /in --dst /out $EXTRA \
  --pseudonym-map /secrets/pseudonym_map.csv --auto-pseudonyms "$PREFIX" \
  --uid-key /secrets/uid_key.hex --study-registry /secrets/study_registry.csv 2>&1 | tee "$LOG"
RUN_STATUS=${PIPESTATUS[0]}

say ""
say "Checking the result ..."
check_cmd "$OUT_ABS" 2>&1 | tee -a "$LOG"
CHECK_STATUS=${PIPESTATUS[0]}
chmod 600 "$LOG" 2>/dev/null

say ""
say "=================================================================="
if [ "$RUN_STATUS" = 0 ] && [ "$CHECK_STATUS" = 0 ]; then
  say " RESULT: PASS"
  say " De-identified copy:  $OUT_ABS"
  say " Overview tables:     $OUT_ABS/_deid_meta"
  say " Before sharing, open a few images and look for names or dates burned into the pixels"
  say " (screenshots, MIPs, scanned forms): this tool only cleans the DICOM headers."
else
  say " RESULT: FAILED (run=$RUN_STATUS, check=$CHECK_STATUS). Do NOT share the output folder."
  say " Details: $LOG"
fi
say ""
say " Keep this folder PRIVATE and backed up; never put it next to the shared data:"
say "   $SEC_ABS"
say " It links pseudonyms to real patient IDs and keeps pseudonyms stable across runs."
say "=================================================================="
[ "$RUN_STATUS" = 0 ] && [ "$CHECK_STATUS" = 0 ]
