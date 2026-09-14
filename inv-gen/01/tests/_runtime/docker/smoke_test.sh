#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/root/.opam/col2inv-ocaml/bin:/opt/col2inv-venv/bin:$PATH"

python --version | grep -E 'Python 3\.(9|1[0-2])\.'
frama-c -version | grep -F '31.0 (Gallium)'
why3 --version | grep -F '1.8.1'
alt-ergo --version | grep -F '2.6.3'
z3 --version | grep -F '4.13.3'
cvc5 --version | head -n 1 | grep -F '1.1.2'
coqc --version | head -n 1 | grep -F '8.18.0'
why3 config list-provers | grep -F 'Alt-Ergo 2.6.3'
why3 config list-provers | grep -F 'Z3 4.13.3'
why3 config list-provers | grep -F 'CVC5 1.1.2'

cd "$PROJECT_ROOT"
python -m py_compile src/*.py

PYTHONPATH=src python - <<'PY'
import logging
from pathlib import Path

import processor

root = Path("Benchmark/acsl-algorithms")
sources = sorted(root.glob("*.cbs"))
assert len(sources) == 45, f"expected 45 TrustC samples, found {len(sources)}"

logger = logging.getLogger("docker-smoke")
logger.addHandler(logging.NullHandler())
loop_count = 0
safe_function_count = 0
for source in sources:
    source_text = source.read_text(encoding="utf-8")
    safe_function_count += source_text.count("_Safe ")
    parsed = processor.build_loops(
        source_text,
        source.stem,
        "smoke/model",
        logger,
    )
    assert parsed.loop_list, f"no annotated loop found in {source.name}"
    loop_count += len(parsed.loop_list)

assert loop_count == 121, f"expected 121 loops, found {loop_count}"
assert safe_function_count == 85, f"expected 85 _Safe functions, found {safe_function_count}"
print(
    f"CoL2Inv smoke test passed: samples={len(sources)} "
    f"loops={loop_count} safe_functions={safe_function_count}"
)
PY

./run_col2inv.sh --help >/dev/null
