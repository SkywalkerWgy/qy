#!/usr/bin/env python3
"""Analyze CoL2Inv loopinvinfer experiment results.

Success criterion:
  A sample is successful when its result directory contains a Pass artifact or
  its log.log ends with the loopinvinfer success summary.

Metric criterion:
  Proposal is parsed from "Proposal number:" in log.log.
  Time is parsed from "Running time:" in log.log.

Loop-count criterion:
  The script strips C comments and string/character literals before counting
  for/while keywords in the CoL2Inv benchmark source directories.
"""

from __future__ import annotations

import argparse
import csv
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path


IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
DATASETS = ("ACSL", "OOPSLA", "SVCOMP")
PROPOSAL_RE = re.compile(r"Proposal number:\s*([0-9]+)")
RUNNING_TIME_RE = re.compile(r"Running time:\s*([0-9]+(?:\.[0-9]+)?)")

DEFAULT_RESULT_ROOTS = [
    "ACSL=../ResultQY45/acsl-algorithms/deepseek-flash",
    "OOPSLA=../ResultQY45/OOPSLA/deepseek-flash",
    "SVCOMP=../ResultQY45/SVCOMP/deepseek-flash",
]
DEFAULT_DATASET_DIRS = [
    "ACSL=../Benchmark/acsl-algorithms",
    "OOPSLA=../Benchmark/OOPSLA",
    "SVCOMP=../Benchmark/SVCOMP",
]


@dataclass(frozen=True)
class ResultRow:
    dataset: str
    sample: str
    loop_count: int | None
    status: str
    success: bool
    proposal: float | None
    time: float | None
    result_dir: str
    source_path: str

    @property
    def group(self) -> str:
        if self.loop_count is None:
            return "Unknown Loop Count"
        if self.loop_count <= 1:
            return "Single Loop"
        return "Multi Loop"


def strip_comments_and_literals(source: str) -> str:
    result: list[str] = []
    i = 0
    state = "code"

    while i < len(source):
        char = source[i]
        next_char = source[i + 1] if i + 1 < len(source) else ""

        if state == "code":
            if char == "/" and next_char == "/":
                state = "line_comment"
                result.extend("  ")
                i += 2
                continue
            if char == "/" and next_char == "*":
                state = "block_comment"
                result.extend("  ")
                i += 2
                continue
            if char == '"':
                state = "string_literal"
                result.append(" ")
                i += 1
                continue
            if char == "'":
                state = "char_literal"
                result.append(" ")
                i += 1
                continue
            result.append(char)
            i += 1
            continue

        if state == "line_comment":
            result.append("\n" if char == "\n" else " ")
            if char == "\n":
                state = "code"
            i += 1
            continue

        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "code"
                result.extend("  ")
                i += 2
                continue
            result.append("\n" if char == "\n" else " ")
            i += 1
            continue

        if state == "string_literal":
            if char == "\\":
                result.extend("  ")
                i += 2
                continue
            if char == '"':
                state = "code"
            result.append("\n" if char == "\n" else " ")
            i += 1
            continue

        if state == "char_literal":
            if char == "\\":
                result.extend("  ")
                i += 2
                continue
            if char == "'":
                state = "code"
            result.append("\n" if char == "\n" else " ")
            i += 1
            continue

    return "".join(result)


def count_loops(source_path: Path) -> int | None:
    try:
        source = source_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    tokens = IDENT_RE.findall(strip_comments_and_literals(source))
    return tokens.count("for") + tokens.count("while")


def parse_number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def average(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def format_average(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:.4f}"


def parse_labeled_path(value: str, default_dataset: str) -> tuple[str, Path]:
    if "=" in value:
        label, path = value.split("=", 1)
        dataset = label.strip().upper()
    else:
        dataset = default_dataset
        path = value
    if dataset in {"ACSL-ALGORITHMS", "ALG"}:
        dataset = "ACSL"
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset label {dataset!r}; expected one of {', '.join(DATASETS)}")
    return dataset, Path(path)


def canonical_samples(dataset: str, source_dir: Path) -> list[tuple[str, str, Path]]:
    samples: list[tuple[str, str, Path]] = []
    extension = "*.cbs" if dataset == "ACSL" else "*.c"
    for source in sorted(source_dir.glob(extension)):
        source_stem = source.stem
        if dataset == "OOPSLA":
            sample = source_stem.removeprefix("oopsla_")
        else:
            sample = source_stem
        result_leaf = source.name
        samples.append((sample, result_leaf, source))
    return samples


def read_log(result_dir: Path) -> str:
    log_path = result_dir / "log.log"
    try:
        return log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def has_pass_artifact(result_dir: Path) -> bool:
    return any(path.is_file() for path in result_dir.glob("*_Pass_*.txt"))


def parse_result(result_dir: Path) -> tuple[str, bool, float | None, float | None]:
    if not result_dir.exists():
        return "missing_result", False, None, None

    log_text = read_log(result_dir)
    success = has_pass_artifact(result_dir) or bool(re.search(r"\bSuccess\b", log_text))
    if success:
        status = "pass"
    elif any(path.is_file() for path in result_dir.glob("*_Fail_*.txt")):
        status = "fail"
    elif any(path.is_file() for path in result_dir.glob("*_Invalid.txt")):
        status = "invalid"
    elif log_text:
        status = "no_pass"
    else:
        status = "missing_log"

    proposal = None
    proposal_match = PROPOSAL_RE.search(log_text)
    if proposal_match:
        proposal = parse_number(proposal_match.group(1))

    time = None
    time_match = RUNNING_TIME_RE.search(log_text)
    if time_match:
        time = parse_number(time_match.group(1))

    return status, success, proposal, time


def progress_line(index: int, total: int, row: ResultRow) -> str:
    loops = "unknown" if row.loop_count is None else str(row.loop_count)
    proposal = "NA" if row.proposal is None else f"{row.proposal:g}"
    time = "NA" if row.time is None else f"{row.time:.4f}"
    outcome = "success" if row.success else "fail"
    return (
        f"[{index:>3}/{total:<3}] {row.dataset:<6} {row.sample:<32} "
        f"group={row.group:<18} loops={loops:<7} "
        f"result={row.status:<14} proposal={proposal:<7} "
        f"time={time:<10} {outcome}"
    )


def collect_rows(
    result_roots: dict[str, Path],
    dataset_dirs: dict[str, Path],
    show_progress: bool,
) -> list[ResultRow]:
    rows: list[ResultRow] = []
    work_items: list[tuple[str, str, str, Path]] = []
    for dataset in DATASETS:
        work_items.extend(
            (dataset, sample, result_leaf, source)
            for sample, result_leaf, source in canonical_samples(dataset, dataset_dirs[dataset])
        )

    for index, (dataset, sample, result_leaf, source_path) in enumerate(work_items, start=1):
        result_dir = result_roots[dataset] / result_leaf
        status, success, proposal, time = parse_result(result_dir)
        loop_count = count_loops(source_path)
        row = ResultRow(
            dataset=dataset,
            sample=sample,
            loop_count=loop_count,
            status=status,
            success=success,
            proposal=proposal,
            time=time,
            result_dir=str(result_dir if result_dir.exists() else ""),
            source_path=str(source_path),
        )
        rows.append(row)
        if show_progress:
            print(progress_line(index, len(work_items), row))
    return rows


def success_count(rows: list[ResultRow], dataset: str | None = None, group: str | None = None) -> int:
    return sum(
        row.success
        and (dataset is None or row.dataset == dataset)
        and (group is None or row.group == group)
        for row in rows
    )


def format_success_by_loop(rows: list[ResultRow], dataset: str | None = None) -> str:
    return ", ".join(
        f"{group}={success_count(rows, dataset=dataset, group=group)}"
        for group in ("Single Loop", "Multi Loop", "Unknown Loop Count")
    )


def print_statistics(label: str, rows: list[ResultRow]) -> None:
    total = len(rows)
    successful = sum(row.success for row in rows)
    fail_count = total - successful
    loop_known = [row for row in rows if row.loop_count is not None]
    total_loops = sum(row.loop_count for row in loop_known if row.loop_count is not None)
    success_rows = [row for row in rows if row.success]
    proposals = [row.proposal for row in success_rows if row.proposal is not None]
    times = [row.time for row in success_rows if row.time is not None]

    print(f"\n----- {label} -----")
    print(f"Sample count        : {total}")
    print(f"Success count       : {successful}")
    print(f"Fail count          : {fail_count}")
    print(f"Success rate        : {(successful / total * 100) if total else 0:.2f}%")
    print(f"Avg proposal/success: {format_average(average(proposals))}")
    print(f"Avg time/success    : {format_average(average(times))}")
    print(f"Proposal samples    : {len(proposals)}")
    print(f"Time samples        : {len(times)}")
    print(f"Known loop samples  : {len(loop_known)}")
    if loop_known:
        print(f"Total loops         : {total_loops}")
        print(f"Avg loops/sample    : {total_loops / len(loop_known):.4f}")


def print_summary(rows: list[ResultRow], details: bool) -> None:
    known_rows = [row for row in rows if row.loop_count is not None]
    unknown_rows = [row for row in rows if row.loop_count is None]
    total_loops = sum(row.loop_count for row in known_rows if row.loop_count is not None)
    success_rows = [row for row in rows if row.success]
    proposals = [row.proposal for row in success_rows if row.proposal is not None]
    times = [row.time for row in success_rows if row.time is not None]

    by_result: dict[str, int] = {}
    for row in rows:
        by_result[row.status] = by_result.get(row.status, 0) + 1

    print("\n===== COL2INV RESULT SUMMARY =====")
    print("Success criterion: Pass artifact exists or log.log reports Success")
    print("Loop source dirs  : ../Benchmark/{acsl-algorithms,OOPSLA,SVCOMP}")
    print(f"Total samples             : {len(rows)}")
    print(f"Success samples           : {sum(row.success for row in rows)}")
    print(f"Failed/missing samples    : {sum(not row.success for row in rows)}")
    print("Success by loop type      : " + format_success_by_loop(rows))
    print("Success by dataset        : " + ", ".join(
        f"{dataset}={success_count(rows, dataset=dataset)}" for dataset in DATASETS
    ))
    print("Success dataset/loop      :")
    for dataset in DATASETS:
        print(f"  {dataset:<6}: {format_success_by_loop(rows, dataset=dataset)}")
    print("Result distribution       : " + ", ".join(f"{key}={value}" for key, value in sorted(by_result.items())))
    print(f"Avg proposal/success      : {format_average(average(proposals))}")
    print(f"Avg time/success          : {format_average(average(times))}")
    print(f"Proposal samples          : {len(proposals)}")
    print(f"Time samples              : {len(times)}")
    print(f"Known loop-count samples  : {len(known_rows)}")
    print(f"Unknown loop-count samples: {len(unknown_rows)}")
    print(f"Total loops               : {total_loops}")
    if known_rows:
        print(f"Average loops/sample      : {total_loops / len(known_rows):.4f}")

    for group in ("Single Loop", "Multi Loop", "Unknown Loop Count"):
        grouped = [row for row in rows if row.group == group]
        if grouped:
            print_statistics(group, grouped)

    if details:
        print("\ndataset,sample,group,loops,status,success,proposal,time,result_dir,source_path")
        writer = csv.writer(sys.stdout)
        for row in rows:
            writer.writerow(
                [
                    row.dataset,
                    row.sample,
                    row.group,
                    "" if row.loop_count is None else row.loop_count,
                    row.status,
                    row.success,
                    "" if row.proposal is None else row.proposal,
                    "" if row.time is None else row.time,
                    row.result_dir,
                    row.source_path,
                ]
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result-root",
        action="append",
        default=None,
        help="CoL2Inv result root as DATASET=PATH. Can be repeated.",
    )
    parser.add_argument(
        "--dataset-dir",
        action="append",
        default=None,
        help="Source directory as DATASET=PATH. Can be repeated.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Print per-sample CSV details after the summary.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Do not print real-time per-sample classification while analyzing.",
    )
    return parser.parse_args()


def main() -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = parse_args()

    try:
        result_roots = {
            dataset: path
            for dataset, path in (
                parse_labeled_path(value, "ACSL")
                for value in (args.result_root if args.result_root is not None else DEFAULT_RESULT_ROOTS)
            )
        }
        dataset_dirs = {
            dataset: path
            for dataset, path in (
                parse_labeled_path(value, "ACSL")
                for value in (args.dataset_dir if args.dataset_dir is not None else DEFAULT_DATASET_DIRS)
            )
        }
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    rows = collect_rows(
        result_roots=result_roots,
        dataset_dirs=dataset_dirs,
        show_progress=not args.no_progress,
    )
    print_summary(rows, args.details)


if __name__ == "__main__":
    main()
