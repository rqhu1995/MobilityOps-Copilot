"""Deterministic explanations built only from freshly replayed experiment evidence."""

import hashlib
import json

from mobilityops.config import Settings
from mobilityops.domain import ScenarioSpec
from mobilityops.domain.decision_explanation import (
    DecisionExplanation, EvidenceClaim, NextExperimentProposal,
)
from mobilityops.domain.experiment import ExperimentPlan, ExperimentReport, GroupComparison
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.experiment_report import ExperimentReportService


METRIC_LABELS = {
    "objective_value": "目标值",
    "dissatisfaction": "不满意度",
    "emission": "排放",
}


def report_fingerprint(report: ExperimentReport) -> str:
    payload = json.dumps(
        report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class DecisionExplanationService:
    """Replay first, then state only what the replayed report supports."""

    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())
        self.reports = ExperimentReportService(self.settings)

    def explain(self, experiment_id: str, *, variant_case_id: str | None = None) -> DecisionExplanation:
        validate_run_id(experiment_id)
        report = self.reports.analyze(experiment_id)
        if variant_case_id is not None:
            validate_run_id(variant_case_id)
            valid = {case.case_id for case in report.batch.plan.variants}
            if variant_case_id not in valid:
                raise ValueError(f"Unknown variant case: {variant_case_id}")
        return explain_report(report, variant_case_id=variant_case_id)

    def next_plan(self, experiment_id: str) -> NextExperimentProposal:
        validate_run_id(experiment_id)
        report = self.reports.analyze(experiment_id)
        return propose_next_plan(report)


def explain_report(report: ExperimentReport, *, variant_case_id: str | None = None) -> DecisionExplanation:
    digest = report_fingerprint(report)
    plan = report.batch.plan
    experiment_evidence = ("experiments/%s/plan.json" % report.experiment_id,
                           "experiments/%s/batch.json" % report.experiment_id)
    facts = [EvidenceClaim(
        category="verified_fact", code="batch_execution",
        text=(f"计划 {plan.planned_calls} 次，实际调用 {report.batch.calls_invoked} 次；"
              f"批次状态为 {report.batch.status}。"), evidence=experiment_evidence,
    )]
    selected_cases = {plan.baseline.case_id}
    if variant_case_id is not None:
        selected_cases.add(variant_case_id)
    else:
        selected_cases.update(case.case_id for case in plan.variants)
    for summary in report.summaries:
        if summary.case_id not in selected_cases:
            continue
        facts.append(EvidenceClaim(
            category="verified_fact", code="verified_candidates", case_id=summary.case_id,
            text=(f"{summary.case_id}：{summary.invoked}/{summary.planned} 次被调用，"
                  f"{summary.verified_candidates} 个候选通过本次独立复核。"),
            evidence=tuple(f"runs/{run_id}" for run_id in _case_run_ids(report, summary.case_id)),
        ))

    observations: list[EvidenceClaim] = []
    for group in report.groups:
        if group.case_id not in selected_cases:
            continue
        values = "；".join(
            f"{METRIC_LABELS[item.metric]} n={item.n}，最小/中位/最大={item.minimum:g}/{item.median:g}/{item.maximum:g}"
            for item in group.metrics
        )
        observations.append(EvidenceClaim(
            category="descriptive_observation", code="case_distribution", case_id=group.case_id,
            text=f"{group.case_id} 本批描述统计：{values}。",
            evidence=tuple(f"runs/{run_id}" for run_id in group.run_ids),
        ))
    comparisons = [item for item in report.comparisons
                   if variant_case_id is None or item.variant_case_id == variant_case_id]
    for comparison in comparisons:
        if not comparison.median_differences:
            continue
        values = "；".join(
            f"{METRIC_LABELS[key]}中位数差={value:g}"
            for key, value in comparison.median_differences.items()
        )
        observations.append(EvidenceClaim(
            category="descriptive_observation", code="median_difference",
            case_id=comparison.variant_case_id,
            text=f"{comparison.variant_case_id} 减基准的本批观测：{values}。",
            evidence=_comparison_evidence(report, comparison),
        ))

    limits = [EvidenceClaim(
        category="cannot_conclude", code="no_causal_or_optimality_claim",
        text="这些结果不能证明统计显著性、因果效应、全局最优性，也不能据此自动选择或部署方案。",
        evidence=("fresh experiment replay",),
    )]
    if any(group.metrics and min(metric.n for metric in group.metrics) < 3
           for group in report.groups if group.case_id in selected_cases):
        limits.append(EvidenceClaim(
            category="cannot_conclude", code="small_sample", text="至少一个相关分组少于 3 个复核候选，样本只足以描述本批运行。",
            evidence=tuple(group.group_id for group in report.groups if group.case_id in selected_cases),
        ))
    for comparison in comparisons:
        if comparison.reasons:
            limits.append(EvidenceClaim(
                category="cannot_conclude", code=comparison.kind, case_id=comparison.variant_case_id,
                text=f"{comparison.variant_case_id} 不能计算方案效应差：{'；'.join(comparison.reasons)}",
                evidence=_comparison_evidence(report, comparison),
            ))
    return DecisionExplanation(
        experiment_id=report.experiment_id, source_report_sha256=digest,
        verified_facts=tuple(facts), descriptive_observations=tuple(observations),
        cannot_conclude=tuple(limits),
    )


def propose_next_plan(report: ExperimentReport) -> NextExperimentProposal:
    digest = report_fingerprint(report)
    plan = report.batch.plan
    if not plan.variants:
        return _blocked(report, digest, "当前实验没有变体，无法构造基准与变体的后续比较。")
    comparison = next((item for item in report.comparisons if item.kind != "scenario_observations"
                       or item.reasons), report.comparisons[0])
    variant = next(case for case in plan.variants if case.case_id == comparison.variant_case_id)
    baseline = plan.baseline
    rationale: list[str] = []
    if comparison.kind == "not_comparable":
        return _blocked(report, digest,
                        f"{variant.case_id} 当前不可比：{'；'.join(comparison.reasons)}")
    if comparison.kind == "search_conditions_changed":
        base_values = baseline.scenario.model_dump()
        variant_values = variant.scenario.model_dump()
        for field in ("solver_runtime_limit_sec", "solution_requirement", "seed"):
            variant_values[field] = base_values[field]
        variant = variant.model_copy(update={
            "scenario": ScenarioSpec.model_validate(variant_values),
            "external_timeout_sec": baseline.external_timeout_sec,
            "backend": baseline.backend,
        })
        rationale.append("将变体的搜索条件、外部 timeout 和 backend 要求与基准对齐，以恢复同口径比较。")
    else:
        rationale.append("复用同一基准与变体做独立确认批次，检查本批中位数差是否稳定出现。")
    repetitions = 3
    baseline = baseline.model_copy(update={"repetitions": repetitions})
    variant = variant.model_copy(update={"repetitions": repetitions})
    calls = baseline.repetitions + variant.repetitions
    wait = baseline.external_timeout_sec * baseline.repetitions + variant.external_timeout_sec * variant.repetitions
    candidate = ExperimentPlan(
        baseline=baseline, variants=(variant,), max_calls=calls,
        external_wait_budget_sec=wait, failure_policy="continue",
    )
    rationale.append("候选计划固定为两个 case 各 3 次、串行、失败继续；所有预算由计划显式封顶。")
    return NextExperimentProposal(
        experiment_id=report.experiment_id, source_report_sha256=digest,
        plan=candidate, rationale=tuple(rationale),
    )


def _blocked(report: ExperimentReport, digest: str, reason: str) -> NextExperimentProposal:
    return NextExperimentProposal(
        experiment_id=report.experiment_id, source_report_sha256=digest,
        plan=None, rationale=("没有生成可执行候选；先补齐可比证据或明确新的实验问题。",),
        blockers=(reason,),
    )


def _case_run_ids(report: ExperimentReport, case_id: str) -> tuple[str, ...]:
    return tuple(attempt.run_id for attempt in report.batch.attempts if attempt.case_id == case_id)


def _comparison_evidence(report: ExperimentReport, comparison: GroupComparison) -> tuple[str, ...]:
    ids: list[str] = []
    for group_id in (comparison.baseline_group_id, comparison.variant_group_id):
        group = next((item for item in report.groups if item.group_id == group_id), None)
        if group is not None:
            ids.extend(f"runs/{run_id}" for run_id in group.run_ids)
    return tuple(ids) or (f"experiments/{report.experiment_id}/batch.json",)
