"""Collect reproducible static project metrics into JSON and Markdown.

The script never touches the network or a real database. It sets safe dummy
environment variables *before* importing application modules so the pydantic
``Settings`` object validates without real secrets; real environment variables
(and therefore any secrets the operator may have exported) take precedence over
the dummy values for validation only and are never printed.

Run from the repository root::

    python -m benchmarks.static_metrics --with-coverage

Methodology for line counts is line based (see :data:`LINE_METHODOLOGY`): a line
is *non-empty* when ``line.strip()`` is truthy, and *non-comment* when it is
non-empty and does not start with ``#``. Docstrings are counted as code lines.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.routing import APIRoute  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402

from benchmarks._env import configure_environment, isolated_cwd  # noqa: E402

configure_environment()

_DEFAULT_JSON = ROOT / "benchmarks" / "static_metrics.json"
_DEFAULT_MD = ROOT / "benchmarks" / "static_metrics.md"
_EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    # Generated output (e.g. ``eval/reports/``) is git-ignored, so counting it
    # would make the committed static metrics depend on the local workspace.
    "reports",
}
LINE_METHODOLOGY = (
    "line based: total_lines = every text line; non_empty_lines = lines whose "
    "strip() is truthy; non_comment_lines = non-empty lines that do not start "
    "with '#' (Python docstrings count as code lines)"
)
AREAS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("src", "src", (".py",)),
    ("tests", "tests", (".py",)),
    ("eval_python", "eval", (".py",)),
    ("eval_json", "eval", (".json",)),
    ("eval_markdown", "eval", (".md",)),
    ("demo", "demo", (".py",)),
    ("benchmarks", "benchmarks", (".py",)),
    ("alembic_versions", "alembic/versions", (".py",)),
)
_PYTHON_AREAS = ("src", "tests", "eval_python", "demo", "benchmarks", "alembic_versions")


def _iter_files(relative: str, suffixes: tuple[str, ...]) -> list[Path]:
    """Return matching files under ``relative``, skipping cache/virtualenv dirs."""
    base = ROOT / relative
    if not base.is_dir():
        return []
    files: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def _line_stats(paths: list[Path]) -> dict[str, int]:
    """Compute the documented line statistics for a list of text files."""
    total = 0
    non_empty = 0
    non_comment = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            total += 1
            stripped = line.strip()
            if not stripped:
                continue
            non_empty += 1
            if not stripped.startswith("#"):
                non_comment += 1
    return {
        "files": len(paths),
        "total_lines": total,
        "non_empty_lines": non_empty,
        "non_comment_lines": non_comment,
    }


def _collect_loc() -> dict[str, Any]:
    """Collect line and file counts for every configured area."""
    areas: dict[str, Any] = {}
    for name, relative, suffixes in AREAS:
        stats = _line_stats(_iter_files(relative, suffixes))
        areas[name] = {
            **stats,
            "path": relative,
            "suffixes": list(suffixes),
        }
    python_files = {name: int(areas[name]["files"]) for name in _PYTHON_AREAS}
    return {
        "methodology": LINE_METHODOLOGY,
        "areas": areas,
        "python_files": python_files,
        "python_files_total": sum(python_files.values()),
    }


def _walk_routes(routes: list[Any], prefix: str = "") -> list[tuple[str, APIRoute]]:
    """Recursively flatten ``APIRoute`` entries, honouring router prefixes.

    Recent FastAPI versions keep ``include_router`` as an ``_IncludedRouter``
    wrapper (``original_router`` + ``include_context.prefix``) instead of
    copying routes into ``app.routes``, so a plain ``isinstance`` scan would
    miss every path served through a router.
    """
    found: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route))
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            sub_prefix = prefix + str(getattr(context, "prefix", "") or "")
            found.extend(_walk_routes(list(original.routes), sub_prefix))
            continue
        nested = getattr(route, "routes", None)
        if nested:
            found.extend(_walk_routes(list(nested), prefix))
    return found


def _collect_api() -> dict[str, Any]:
    """Import the FastAPI app and count the concrete ``APIRoute`` entries."""
    with isolated_cwd():
        from src.main import app

    routes = _walk_routes(list(app.routes))
    by_method: Counter[str] = Counter()
    listing: list[dict[str, Any]] = []
    for full_path, route in routes:
        methods = sorted(route.methods or [])
        by_method.update(methods)
        listing.append(
            {
                "path": full_path,
                "methods": methods,
                "name": route.name,
                "include_in_schema": bool(getattr(route, "include_in_schema", True)),
            }
        )
    listing.sort(key=lambda item: (item["path"], item["methods"]))
    return {
        "endpoint_count": len(routes),
        "by_method": dict(sorted(by_method.items())),
        "routes": listing,
    }


def _collect_prometheus() -> dict[str, Any]:
    """Count Prometheus families and samples, separating DocMind's own metrics."""
    families = list(REGISTRY.collect())
    docmind = [family for family in families if family.name.startswith("docmind_")]
    return {
        "families_total": len(families),
        "families_docmind": len(docmind),
        "samples_total": sum(len(family.samples) for family in families),
        "samples_docmind": sum(len(family.samples) for family in docmind),
        "docmind_family_names": sorted(family.name for family in docmind),
    }


def _collect_dependencies() -> dict[str, Any]:
    """Count runtime, dev and demo dependencies declared in ``pyproject.toml``."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {})
    runtime = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    dev = list(optional.get("dev", []))
    demo = list(optional.get("demo", []))
    return {
        "runtime": len(runtime),
        "dev": len(dev),
        "demo": len(demo),
        "runtime_requirements": runtime,
        "dev_requirements": dev,
        "demo_requirements": demo,
    }


def _collect_make() -> dict[str, Any]:
    """Count unique Make targets (special dot-targets are ignored)."""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line)
        if match:
            targets.append(match.group(1))
    unique = list(dict.fromkeys(targets))
    return {"target_count": len(unique), "targets": unique}


def _collect_dataset() -> dict[str, Any]:
    """Summarise the golden dataset and the corpus it references."""
    path = ROOT / "eval" / "golden_dataset.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = raw.get("cases", [])
    corpus = raw.get("corpus", [])
    answerable = sum(1 for case in cases if case.get("answerable"))
    corpus_words = 0
    for entry in corpus:
        file_path = ROOT / "eval" / "corpus" / str(entry.get("filename", ""))
        if file_path.is_file():
            corpus_words += len(file_path.read_text(encoding="utf-8").split())
    size_bytes = path.stat().st_size
    return {
        "cases": len(cases),
        "answerable": answerable,
        "negative": len(cases) - answerable,
        "corpus_documents": len(corpus),
        "corpus_words": corpus_words,
        "json_bytes": size_bytes,
        "json_kib": round(size_bytes / 1024.0, 2),
    }


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command in the repository root and capture its text output."""
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _collect_tests() -> dict[str, Any]:
    """Collect the number of tests pytest discovers."""
    process = _run_command([sys.executable, "-m", "pytest", "--collect-only", "-q"])
    output = (process.stdout or "") + (process.stderr or "")
    match = re.search(r"(\d+) tests? collected", output)
    tail = [line for line in output.splitlines() if line.strip()][-3:]
    return {
        "collected": int(match.group(1)) if match else None,
        "exit_code": process.returncode,
        "last_lines": tail,
    }


def _collect_coverage() -> dict[str, Any]:
    """Re-run the coverage gate and parse the TOTAL line."""
    process = _run_command(
        [
            sys.executable,
            "-m",
            "pytest",
            "--cov=src",
            "--cov-report=term",
            "--cov-fail-under=60",
            "-q",
        ]
    )
    output = (process.stdout or "") + (process.stderr or "")
    # Branch coverage adds extra numeric columns (stmts, miss, branch, partial,
    # percent), so take the first two integers and the last percentage.
    total = re.search(r"^TOTAL\s+(.+)$", output, re.MULTILINE)
    total_line = total.group(0).strip() if total else None
    numbers = re.findall(r"\d+", total.group(1)) if total else []
    percentages = re.findall(r"(\d+)%", total.group(1)) if total else []
    exact = re.search(r"Total coverage:\s+([\d.]+)%", output)
    passed = re.search(r"(\d+) passed", output)
    skipped = re.search(r"(\d+) skipped", output)
    failed = re.search(r"(\d+) failed", output)
    return {
        "exit_code": process.returncode,
        "statements": int(numbers[0]) if numbers else None,
        "missed": int(numbers[1]) if len(numbers) > 1 else None,
        "coverage_percent": int(percentages[-1]) if percentages else None,
        "coverage_percent_exact": float(exact.group(1)) if exact else None,
        "passed": int(passed.group(1)) if passed else None,
        "skipped": int(skipped.group(1)) if skipped else None,
        "failed": int(failed.group(1)) if failed else None,
        "total_line": total_line,
    }


def _environment() -> dict[str, Any]:
    """Return the machine and interpreter metadata recorded with the numbers."""
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine() or "unknown",
        # Never record an absolute path: this report is committed to the repo.
        "root": ".",
    }


def collect(*, with_coverage: bool, skip_tests: bool) -> dict[str, Any]:
    """Collect every static metric into a JSON-friendly mapping."""
    api = _collect_api()
    report: dict[str, Any] = {
        "meta": _environment(),
        "loc": _collect_loc(),
        "api": api,
        "prometheus": _collect_prometheus(),
        "dependencies": _collect_dependencies(),
        "make": _collect_make(),
        "dataset": _collect_dataset(),
    }
    report["tests"] = (
        {"collected": None, "exit_code": None, "last_lines": [], "skipped": True}
        if skip_tests
        else _collect_tests()
    )
    if with_coverage:
        report["coverage"] = _collect_coverage()
    return report


def _f(value: Any, digits: int = 3) -> str:
    """Format a scalar for the Markdown tables."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the static metrics as a Markdown document."""
    meta = report["meta"]
    loc = report["loc"]
    api = report["api"]
    prometheus = report["prometheus"]
    dependencies = report["dependencies"]
    make = report["make"]
    dataset = report["dataset"]
    lines = [
        "# DocMind static metrics",
        "",
        f"Generated: {meta['generated_at']}  ",
        f"Python: {meta['python']}  ",
        f"Platform: {meta['platform']}",
        "",
        "## Lines of code",
        "",
        f"Methodology: {loc['methodology']}",
        "",
        "| Area | Path | Suffix | Files | Total lines | Non-empty | Non-comment |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for name, area in loc["areas"].items():
        suffixes = ", ".join(area["suffixes"])
        lines.append(
            f"| {name} | `{area['path']}` | {suffixes} | {area['files']} | "
            f"{area['total_lines']} | {area['non_empty_lines']} | "
            f"{area['non_comment_lines']} |"
        )
    lines.extend(
        [
            "",
            f"Python files total: {loc['python_files_total']}",
            "",
            "## API endpoints",
            "",
            f"Endpoints (`APIRoute`): {api['endpoint_count']}",
            "",
            "| Method | Count |",
            "| --- | ---: |",
        ]
    )
    for method, count in api["by_method"].items():
        lines.append(f"| {method} | {count} |")
    lines.extend(["", "Paths:", ""])
    for route in api["routes"]:
        methods = ", ".join(route["methods"])
        schema = "" if route["include_in_schema"] else " (schema hidden)"
        lines.append(f"- `{methods} {route['path']}`{schema}")
    lines.extend(
        [
            "",
            "## Prometheus registry",
            "",
            f"- Families total: {prometheus['families_total']}",
            f"- DocMind families: {prometheus['families_docmind']}",
            f"- Samples total: {prometheus['samples_total']}",
            f"- DocMind samples: {prometheus['samples_docmind']}",
            "",
            "DocMind metric families: "
            + ", ".join(f"`{name}`" for name in prometheus["docmind_family_names"]),
            "",
            "## Tests and coverage",
            "",
            f"- Tests collected: {_f(report['tests'].get('collected'), 0)}",
        ]
    )
    coverage = report.get("coverage")
    if coverage:
        lines.extend(
            [
                f"- Coverage TOTAL: {_f(coverage.get('coverage_percent'), 0)}% "
                f"({coverage.get('missed')} missed of {coverage.get('statements')} statements)",
                f"- Passed / skipped / failed: {coverage.get('passed')} / "
                f"{coverage.get('skipped')} / {coverage.get('failed')}",
            ]
        )
    lines.extend(
        [
            "",
            "## Dependencies",
            "",
            f"- Runtime: {dependencies['runtime']}",
            f"- Dev: {dependencies['dev']}",
            f"- Demo: {dependencies['demo']}",
            "",
            "## Make targets",
            "",
            f"Targets: {make['target_count']}",
            "",
            "## Golden dataset",
            "",
            f"- Cases: {dataset['cases']} "
            f"(answerable {dataset['answerable']}, negative {dataset['negative']})",
            f"- Corpus documents: {dataset['corpus_documents']}",
            f"- Corpus words: {dataset['corpus_words']}",
            f"- JSON size: {dataset['json_bytes']} bytes ({dataset['json_kib']} KiB)",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.static_metrics",
        description="Collect reproducible static DocMind project metrics.",
    )
    parser.add_argument(
        "--json-out",
        default=str(_DEFAULT_JSON),
        dest="json_out",
        help=f"JSON output path (default: {_DEFAULT_JSON})",
    )
    parser.add_argument(
        "--markdown-out",
        default=str(_DEFAULT_MD),
        dest="markdown_out",
        help=f"Markdown output path (default: {_DEFAULT_MD})",
    )
    parser.add_argument(
        "--with-coverage",
        action="store_true",
        dest="with_coverage",
        help="re-run pytest with coverage and record the TOTAL line",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        dest="skip_tests",
        help="do not invoke pytest --collect-only",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Collect metrics, write JSON and Markdown, and return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    report = collect(with_coverage=args.with_coverage, skip_tests=args.skip_tests)

    json_path = Path(args.json_out)
    markdown_path = Path(args.markdown_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report) + "\n", encoding="utf-8")
    print(f"wrote {json_path} and {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
