from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from agent.multi_agent import MultiAgentRouter
from agent.tools.react_agent import ReactAgent
from rag.rag_service import RagSummarizeService


ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "evaluation" / "datasets"
DEFAULT_DATASET = DATASET_DIR / "calibration.jsonl"
LEGACY_CSV = ROOT / "tests.csv"
REPORT_DIR = ROOT / "evaluation" / "reports"
LATEST_JSON = REPORT_DIR / "latest.json"
LATEST_MD = REPORT_DIR / "latest.md"
REJECTION_MARKERS = (
    "知识库中没有",
    "没有足够证据",
    "无法回答",
    "联系人工客服",
    "不能提供",
    "无法协助",
    "无权",
)


@dataclass
class EvaluationCaseResult:
    case_id: str
    category: str
    question: str
    answer: str
    expected_route: list[str]
    actual_route: list[str]
    should_reject: bool
    is_reject: bool
    required_fact_groups: list[Any]
    matched_fact_groups: int
    forbidden_facts: list[str]
    matched_forbidden_facts: list[str]
    expected_sources: list[str]
    recall_at_10: bool | None
    recall_at_4: bool | None
    route_matched: bool | None
    is_correct: bool
    failure_reasons: list[str]
    retrieval_debug: dict[str, Any]
    execution_error: str | None
    elapsed_seconds: float


def load_cases(dataset_path: Path) -> list[dict[str, Any]]:
    if dataset_path.suffix.lower() == ".jsonl":
        cases = []
        with dataset_path.open("r", encoding="utf-8") as file_obj:
            for line_number, line in enumerate(file_obj, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    cases.append(json.loads(stripped))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid JSONL at {dataset_path}:{line_number}: {exc}"
                    ) from exc
        return cases

    with dataset_path.open("r", encoding="utf-8", newline="") as file_obj:
        return list(csv.DictReader(file_obj))


def evaluate_cases(
    cases: Iterable[dict[str, Any]],
    *,
    mode: str = "agent",
) -> tuple[list[EvaluationCaseResult], dict[str, float]]:
    agent = ReactAgent() if mode == "agent" else None
    rag = RagSummarizeService() if mode == "rag" else None
    router = (
        getattr(agent, "specialist_router", None)
        if agent is not None
        else MultiAgentRouter()
    )

    results: list[EvaluationCaseResult] = []
    elapsed_values: list[float] = []

    for index, case in enumerate(cases, start=1):
        question = str(case["question"]).strip()
        case_id = str(case.get("id") or f"case-{index:04d}")
        category = str(case.get("category") or "general")
        has_expected_route = "expected_route" in case
        expected_route = _as_string_list(case.get("expected_route")) if has_expected_route else []
        should_reject = _as_bool(case.get("should_reject"))
        fact_groups = _fact_groups(case)
        forbidden_facts = _as_string_list(case.get("forbidden_facts"))
        expected_sources = _as_string_list(case.get("expected_sources"))
        retrieval_debug: dict[str, Any] = {}
        execution_error = None
        recall_at_10: bool | None = None
        recall_at_4: bool | None = None

        started = time.perf_counter()
        try:
            if mode == "agent":
                answer = "".join(
                    chunk
                    for chunk in agent.execute_stream(
                        question,
                        session_id=str(uuid.uuid4()),
                        user_uuid=str(uuid.uuid4()),
                        request_id=uuid.uuid4().hex,
                    )
                )
            elif mode == "rag":
                allowed_domains = _as_string_list(case.get("allowed_domains")) or None
                retrieval = rag.retrieve(question, allowed_domains=allowed_domains)
                answer = rag.answer(question, retrieval)
                retrieval_debug = dict(retrieval.debug_scores)
                source_names_10 = [
                    str(candidate.metadata.get("source_name", ""))
                    for candidate in retrieval.candidates[:10]
                ]
                source_names_4 = [
                    str(candidate.metadata.get("source_name", ""))
                    for candidate in retrieval.selected[:4]
                ]
                if expected_sources:
                    recall_at_10 = _sources_match(expected_sources, source_names_10)
                    recall_at_4 = _sources_match(expected_sources, source_names_4)
            else:
                answer = ""
        except Exception as exc:
            answer = ""
            execution_error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started

        if mode == "router":
            routed = router._route_by_rules(
                query=question,
                task_route={},
                history_recall_context="",
                system_context="",
            )
        else:
            routed = router.route(
                query=question,
                task_route={},
                history_recall_context="",
                system_context="",
            )
        actual_route = [
            str(route.get("agent_name", ""))
            for route in routed.get("specialist_routes", [])
            if route.get("agent_name")
        ]

        is_reject = any(marker in answer for marker in REJECTION_MARKERS)
        matched_groups = sum(
            1 for group in fact_groups if _fact_group_matches(answer, group)
        )
        matched_forbidden = [term for term in forbidden_facts if term in answer]
        route_matched = (
            set(actual_route) == set(expected_route) if has_expected_route else None
        )

        failure_reasons = []
        if execution_error:
            failure_reasons.append("execution_error")
        if mode != "router":
            if is_reject != should_reject:
                failure_reasons.append("rejection_mismatch")
            if fact_groups and matched_groups != len(fact_groups):
                failure_reasons.append("required_facts_missing")
            if matched_forbidden:
                failure_reasons.append("forbidden_fact_present")
        if route_matched is False:
            failure_reasons.append("route_mismatch")
        if recall_at_10 is False:
            failure_reasons.append("recall_at_10_miss")
        if recall_at_4 is False:
            failure_reasons.append("recall_at_4_miss")

        is_correct = not failure_reasons
        elapsed_values.append(elapsed)
        results.append(
            EvaluationCaseResult(
                case_id=case_id,
                category=category,
                question=question,
                answer=answer,
                expected_route=expected_route,
                actual_route=actual_route,
                should_reject=should_reject,
                is_reject=is_reject,
                required_fact_groups=fact_groups,
                matched_fact_groups=matched_groups,
                forbidden_facts=forbidden_facts,
                matched_forbidden_facts=matched_forbidden,
                expected_sources=expected_sources,
                recall_at_10=recall_at_10,
                recall_at_4=recall_at_4,
                route_matched=route_matched,
                is_correct=is_correct,
                failure_reasons=failure_reasons,
                retrieval_debug=retrieval_debug,
                execution_error=execution_error,
                elapsed_seconds=round(elapsed, 4),
            )
        )

    metrics = _calculate_metrics(results, elapsed_values)
    return results, metrics


def _calculate_metrics(
    results: list[EvaluationCaseResult],
    elapsed_values: list[float],
) -> dict[str, Any]:
    total = len(results)
    denominator = total or 1
    reject_tp = sum(result.should_reject and result.is_reject for result in results)
    reject_fp = sum(not result.should_reject and result.is_reject for result in results)
    reject_fn = sum(result.should_reject and not result.is_reject for result in results)
    reject_precision = reject_tp / (reject_tp + reject_fp or 1)
    reject_recall = reject_tp / (reject_tp + reject_fn or 1)
    reject_f1 = (
        2 * reject_precision * reject_recall / (reject_precision + reject_recall)
        if reject_precision + reject_recall
        else 0.0
    )

    total_fact_groups = sum(len(result.required_fact_groups) for result in results)
    matched_fact_groups = sum(result.matched_fact_groups for result in results)
    route_results = [result for result in results if result.route_matched is not None]
    recall_10_results = [result for result in results if result.recall_at_10 is not None]
    recall_4_results = [result for result in results if result.recall_at_4 is not None]

    return {
        "total": total,
        "accuracy": sum(result.is_correct for result in results) / denominator,
        "fact_coverage": matched_fact_groups / (total_fact_groups or 1),
        "forbidden_fact_rate": sum(
            bool(result.matched_forbidden_facts) for result in results
        )
        / denominator,
        "reject_precision": reject_precision,
        "reject_recall": reject_recall,
        "reject_f1": reject_f1,
        "route_accuracy": (
            sum(result.route_matched is True for result in route_results)
            / len(route_results)
            if route_results
            else None
        ),
        "recall_at_10": (
            sum(result.recall_at_10 is True for result in recall_10_results)
            / len(recall_10_results)
            if recall_10_results
            else None
        ),
        "recall_at_4": (
            sum(result.recall_at_4 is True for result in recall_4_results)
            / len(recall_4_results)
            if recall_4_results
            else None
        ),
        "avg_latency_seconds": statistics.fmean(elapsed_values)
        if elapsed_values
        else 0.0,
        "p95_latency_seconds": _percentile(elapsed_values, 0.95),
    }


def write_report(
    results: list[EvaluationCaseResult],
    metrics: dict[str, Any],
    *,
    mode: str,
    suite: str,
    dataset_path: Path,
) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "suite": suite,
        "dataset": str(dataset_path),
        "metrics": metrics,
        "results": [asdict(result) for result in results],
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    LATEST_JSON.write_text(json_text, encoding="utf-8")
    mode_json = REPORT_DIR / f"{suite}-{mode}.json"
    mode_json.write_text(json_text, encoding="utf-8")

    lines = [
        f"# Evaluation Report ({suite})",
        "",
        f"- Mode: `{mode}`",
        f"- Dataset: `{dataset_path}`",
        f"- Total: `{int(metrics['total'])}`",
        f"- Accuracy: `{_format_percent(metrics['accuracy'])}`",
        f"- Fact coverage: `{_format_percent(metrics['fact_coverage'])}`",
        f"- Rejection F1: `{_format_percent(metrics['reject_f1'])}`",
        f"- Route accuracy: `{_format_percent(metrics['route_accuracy'])}`",
        f"- Recall@10: `{_format_percent(metrics['recall_at_10'])}`",
        f"- Recall@4: `{_format_percent(metrics['recall_at_4'])}`",
        f"- Avg latency: `{metrics['avg_latency_seconds']:.2f}s`",
        f"- P95 latency: `{metrics['p95_latency_seconds']:.2f}s`",
        "",
        "## Cases",
    ]
    for result in results:
        status = "PASS" if result.is_correct else "FAIL"
        suffix = (
            ""
            if result.is_correct
            else f" ({', '.join(result.failure_reasons)})"
        )
        lines.append(f"- [{status}] `{result.case_id}` {result.question}{suffix}")
    markdown_text = "\n".join(lines)
    LATEST_MD.write_text(markdown_text, encoding="utf-8")
    mode_md = REPORT_DIR / f"{suite}-{mode}.md"
    mode_md.write_text(markdown_text, encoding="utf-8")


def _fact_groups(case: dict[str, Any]) -> list[Any]:
    raw_groups = case.get("required_facts")
    if raw_groups:
        groups = []
        for group in raw_groups:
            if isinstance(group, dict) and "all_of" in group:
                concepts = [
                    _as_string_list(concept)
                    for concept in group.get("all_of") or []
                ]
                concepts = [concept for concept in concepts if concept]
                if concepts:
                    groups.append({"all_of": concepts})
                continue
            alternatives = _as_string_list(group)
            if alternatives:
                groups.append(alternatives)
        return groups

    legacy_keywords = _as_string_list(case.get("expected_keywords"))
    return [[keyword] for keyword in legacy_keywords]


def _fact_group_matches(answer: str, group: Any) -> bool:
    if isinstance(group, dict) and "all_of" in group:
        return all(
            any(term in answer for term in alternatives)
            for alternatives in group["all_of"]
        )
    return any(term in answer for term in _as_string_list(group))


def _as_string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                return _as_string_list(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in stripped.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _sources_match(expected: list[str], actual: list[str]) -> bool:
    return all(
        any(source in actual_source for actual_source in actual)
        for source in expected
    )


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = int(round((len(sorted_values) - 1) * ratio))
    return sorted_values[max(0, min(index, len(sorted_values) - 1))]


def _format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3%}"


def _resolve_dataset(args: argparse.Namespace) -> Path:
    if args.dataset:
        return Path(args.dataset)
    suite_dataset = DATASET_DIR / f"{args.suite}.jsonl"
    if suite_dataset.exists():
        return suite_dataset
    if DEFAULT_DATASET.exists():
        return DEFAULT_DATASET
    return LEGACY_CSV


def run_command(args: argparse.Namespace) -> int:
    dataset_path = _resolve_dataset(args)
    cases = load_cases(dataset_path)
    if args.mode == "rag":
        cases = [case for case in cases if case.get("category") == "rag"]
    results, metrics = evaluate_cases(cases, mode=args.mode)
    write_report(
        results,
        metrics,
        mode=args.mode,
        suite=args.suite,
        dataset_path=dataset_path,
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


def benchmark_command(args: argparse.Namespace) -> int:
    args.mode = "agent"
    args.suite = "benchmark"
    return run_command(args)


def report_command(_: argparse.Namespace) -> int:
    if not LATEST_JSON.exists():
        print("No report found. Run `python -m evaluation.cli run` first.")
        return 1
    payload = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    print(json.dumps(payload["metrics"], ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluation CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run evaluation suite")
    run_parser.add_argument("--suite", default="calibration")
    run_parser.add_argument(
        "--mode",
        choices=["agent", "rag", "router"],
        default="agent",
    )
    run_parser.add_argument("--dataset")
    run_parser.set_defaults(func=run_command)

    benchmark_parser = subparsers.add_parser("benchmark", help="Run benchmark")
    benchmark_parser.add_argument("--dataset")
    benchmark_parser.set_defaults(func=benchmark_command)

    report_parser = subparsers.add_parser("report", help="Print latest report")
    report_parser.set_defaults(func=report_command)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
