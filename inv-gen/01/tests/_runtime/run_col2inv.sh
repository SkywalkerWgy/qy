#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_HOME="${COL2INV_CONDA_HOME:-}"
CONDA_ENV="${COL2INV_CONDA_ENV:-col2inv}"

sample="_binary_search.cbs"
dataset="acsl-algorithms"
model="deepseek-flash"
output="../ResultQY45/"

usage() {
    cat <<'EOF'
run_col2inv.sh - run one CoL2Inv sample by default.

Usage:
  ./run_col2inv.sh [options] [-- extra CoL2Inv arguments]

Options:
  -f, --file FILE       Sample file (default: _binary_search.cbs)
  -d, --dataset NAME    Dataset directory (default: acsl-algorithms)
  -m, --model MODEL     API/local model identifier (default: deepseek-flash)
  -o, --output DIR      Result root relative to src/ (default: ../ResultQY45/)
  -h, --help            Show this help

Examples:
  export DEEPSEEK_API_KEY="sk-..."
  ./run_col2inv.sh
  ./run_col2inv.sh --file _bubble_sort.cbs
  ./run_col2inv.sh --file _quick_sort.cbs
EOF
}

# run_col2inv.sh - run one CoL2Inv sample by default.
#
# Usage:
#   ./run_col2inv.sh [options] [-- extra CoL2Inv arguments]
#
# Options:
#   -f, --file FILE       Sample file (default: _binary_search.cbs)
#   -d, --dataset NAME    Dataset directory (default: acsl-algorithms)
#   -m, --model MODEL     API/local model identifier (default: deepseek-flash)
#   -o, --output DIR      Result root relative to src/ (default: ../ResultQY45/)
#   -h, --help            Show this help
#
# Examples:
#   export DEEPSEEK_API_KEY="sk-..."
#   ./run_col2inv.sh
#   ./run_col2inv.sh --file _bubble_sort.cbs
#   ./run_col2inv.sh --file _quick_sort.cbs

extra_args=()
while (($#)); do
    case "$1" in
        -f|--file)
            sample="${2:?missing value for $1}"
            shift 2
            ;;
        -d|--dataset)
            dataset="${2:?missing value for $1}"
            shift 2
            ;;
        -m|--model)
            model="${2:?missing value for $1}"
            shift 2
            ;;
        -o|--output)
            output="${2:?missing value for $1}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            extra_args=("$@")
            break
            ;;
        *)
            printf 'Unknown option: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! -f "$PROJECT_ROOT/Benchmark/$dataset/$sample" ]]; then
    printf 'Sample not found: %s\n' "$PROJECT_ROOT/Benchmark/$dataset/$sample" >&2
    exit 2
fi

# The host setup uses Conda; the slim Docker image supplies an isolated venv
# on PATH and therefore does not need to carry a complete Conda distribution.
if [[ -n "$CONDA_HOME" && -f "$CONDA_HOME/etc/profile.d/conda.sh" ]]; then
    # shellcheck disable=SC1091
    source "$CONDA_HOME/etc/profile.d/conda.sh"
    conda activate "$CONDA_ENV"
fi

cd "$PROJECT_ROOT/src"
exec python -B loopinvinfer.py \
    --dataset "$dataset" \
    --file "$sample" \
    --model "$model" \
    --output "$output" \
    "${extra_args[@]}"
