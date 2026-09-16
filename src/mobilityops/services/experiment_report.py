"""Read-only independent experiment replay and deterministic descriptive reports."""

from collections import Counter
import hashlib
import html
import json
import math
from pathlib import Path
from statistics import median
from urllib.parse import quote

from mobilityops.config import Settings
from mobilityops.domain import ResultStatus
from mobilityops.domain.analysis import RunObservation
from mobilityops.domain.experiment import (
    AttemptReview, CaseSummary, DescriptiveMetric, ExperimentGroup, ExperimentReport, GroupComparison,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.experiment_service import checked_root, experiment_path, load_batch, write_json
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_analysis import SEARCH_FIELDS, ScenarioAnalysisService, _changes, _compare


METRICS = ("objective_value", "dissatisfaction", "emission")
LIMITATIONS = (
    "统计仅包含本次独立复核通过的可行候选；失败和没有候选的结果不补零。",
    "按完整场景、模型/输入签名及搜索条件分组；外部 timeout 也是搜索条件的一部分。",
    "连续小样本仅用于描述本批观测，不默认相互独立，不作显著性、因果效应或最优性结论。",
    "重复编号只是实验索引；HGS 没有受支持的 seed，重复运行可能得到不同结果。",
    "HGS 六位有效数字的文本舍入范围不是统计置信区间；中位数及其差值使用打印值。",
    "每次调用完整预留外部 timeout，即使提前结束也不退回预算；不重试、不补跑。",
    "批次实际耗时包含预检查、输入快照、求解、验证和检查点写入；报告生成耗时另计。",
    "执行失败标签记录当时诊断；验证失败不证明不可行。Gurobi 的不可行结论只属于其周期模型。",
    "pending/running/interrupted 条目属于未完成记录，即使存在部分原始文件也不参与统计。",
    "Gurobi 的 bound/gap 只属于原始运行及其模型；不跨运行相减或转移，不用于证明 HGS 解质量。",
    "不同 backend 的 runtime 口径不同，本报告不比较求解速度。",
    "HGS 到站时原子库存更新与 Gurobi 周期语义均不保证真实交通中的服务完成时刻。",
    "散列用于核对本地证据一致性，不能认证全部产物被协调修改前的真实性。",
)


def group_observations(items: list[tuple[str, RunObservation]]) -> tuple[ExperimentGroup, ...]:
    grouped: dict[str, tuple[str, list[RunObservation]]] = {}
    for case_id, observation in items:
        if not observation.verified_candidate:
            continue
        identity = {"case_id": case_id, "scenario": observation.scenario.model_dump(mode="json", exclude={"scenario_name"}),
                    "model": observation.model_signature, "search": observation.search_context}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
        grouped.setdefault(key, (case_id, []))[1].append(observation)
    groups = []
    for key, (case_id, observations) in grouped.items():
        metrics = []
        for metric in METRICS:
            values = [getattr(o.result, metric) for o in observations]
            if any(v is None or not math.isfinite(v) for v in values):
                raise ValueError("Missing or nonfinite verified metric")
            metrics.append(DescriptiveMetric(metric=metric, n=len(values), minimum=min(values),
                                             median=median(sorted(values)), maximum=max(values)))
        first = observations[0]
        groups.append(ExperimentGroup(group_id=key, case_id=case_id, model_signature=first.model_signature,
                                      search_context=first.search_context, run_ids=tuple(o.result.run_id for o in observations),
                                      metrics=tuple(metrics)))
    return tuple(groups)


class ExperimentReportService:
    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())

    def analyze(self, experiment_id: str) -> ExperimentReport:
        """Never read old report metrics or validation flags as verification results."""
        batch, hashes = load_batch(self.settings, experiment_id)
        analysis = ScenarioAnalysisService(self.settings)
        cases = {c.case_id: c for c in batch.plan.cases}
        reviews, candidates = [], []
        for attempt in batch.attempts:
            case = cases[attempt.case_id]
            if attempt.status in {"pending", "running", "interrupted"} or not attempt.invoked:
                reviews.append(AttemptReview(run_id=attempt.run_id, status="not_completed", diagnostic=attempt.status))
                continue
            if attempt.result_status is None:
                reviews.append(AttemptReview(run_id=attempt.run_id, status="excluded", diagnostic="Invocation did not return a terminal result; retained artifacts are incomplete."))
                continue
            try:
                observation = analysis.observe(attempt.run_id)
                result = observation.result
                if observation.scenario != case.scenario or attempt.decision is None:
                    raise ValueError("Run scenario differs from the experiment plan or lacks selection evidence")
                if result.backend != attempt.decision.selected_backend or (case.backend is not None and result.backend != case.backend):
                    raise ValueError("Run backend differs from its saved selection")
                if (result.status, result.feasible) != (attempt.result_status, attempt.feasible):
                    raise ValueError("Run status differs from the execution checkpoint")
                run_dir = self.settings.runs_dir / attempt.run_id
                process = strict_json(read_local(run_dir, run_dir / "process.json"))
                if process.get("timeout_sec") != case.external_timeout_sec or result.solver_metadata.get("timeout_sec") != case.external_timeout_sec:
                    raise ValueError("Run external timeout differs from the explicit experiment plan")
                if observation.verified_candidate != (attempt.status in {"succeeded", "timeout_with_candidate"}):
                    raise ValueError("Verified candidate availability differs from the execution outcome")
                if result.status not in {ResultStatus.SUCCEEDED, ResultStatus.TIMEOUT, ResultStatus.FAILED, ResultStatus.INFEASIBLE}:
                    raise ValueError("Saved run is not terminal")
                observation = observation.model_copy(update={"search_context": {
                    **observation.search_context, "external_timeout_sec": case.external_timeout_sec,
                }})
                if observation.verified_candidate:
                    if any(getattr(result, metric) is None for metric in METRICS):
                        raise ValueError("Verified candidate lacks a report metric")
                    candidates.append((case.case_id, observation))
                reviews.append(AttemptReview(run_id=attempt.run_id,
                                             status="verified_candidate" if observation.verified_candidate else "no_candidate",
                                             observation=observation))
            except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
                reviews.append(AttemptReview(run_id=attempt.run_id, status="excluded", diagnostic=f"Evidence review failed: {type(exc).__name__}: {exc}"))
        groups = group_observations(candidates)
        summaries = []
        base = batch.plan.baseline
        for case in batch.plan.cases:
            attempts = [a for a in batch.attempts if a.case_id == case.case_id]
            changes = _changes(base.scenario.model_dump(mode="json"), case.scenario.model_dump(mode="json"))
            search = _changes({**{k: base.scenario.model_dump(mode="json")[k] for k in SEARCH_FIELDS},
                               "backend_requirement": base.backend, "external_timeout_sec": base.external_timeout_sec},
                              {**{k: case.scenario.model_dump(mode="json")[k] for k in SEARCH_FIELDS},
                               "backend_requirement": case.backend, "external_timeout_sec": case.external_timeout_sec})
            ids = {a.run_id for a in attempts}
            summaries.append(CaseSummary(case_id=case.case_id,
                                         scenario_changes=tuple(c for c in changes if c.field not in SEARCH_FIELDS | {"scenario_name"}),
                                         search_changes=search, planned=case.repetitions, invoked=sum(a.invoked for a in attempts),
                                         verified_candidates=sum(r.status == "verified_candidate" for r in reviews if r.run_id in ids),
                                         execution_status_counts=dict(sorted(Counter(a.status for a in attempts).items())),
                                         excluded_run_ids=tuple(r.run_id for r in reviews if r.run_id in ids and r.status != "verified_candidate")))
        comparisons = []
        observations = {o.result.run_id: o for _, o in candidates}
        baseline_groups = [g for g in groups if g.case_id == base.case_id]
        for case in batch.plan.variants:
            variant_groups = [g for g in groups if g.case_id == case.case_id]
            left = baseline_groups[0] if len(baseline_groups) == 1 else None
            right = variant_groups[0] if len(variant_groups) == 1 else None
            differences, search_changes = {}, ()
            if left is None or right is None:
                kind, reasons = "not_comparable", ("Each case must have exactly one verified group; a case has no candidates or multiple model/search signatures.",)
            else:
                comparison = _compare(observations[left.run_ids[0]], observations[right.run_ids[0]])
                search_changes = comparison.search_changes
                kind, reasons = comparison.kind, comparison.reasons
                if not reasons and search_changes:
                    kind, reasons = "search_conditions_changed", ("Search conditions differ; medians are displayed separately and no scenario-effect difference is computed.",)
                if not reasons:
                    differences = {l.metric: r.median - l.median for l, r in zip(left.metrics, right.metrics, strict=True)}
                    if any(not math.isfinite(v) for v in differences.values()):
                        kind, reasons, differences = "not_comparable", ("Median difference is not finite.",), {}
            comparisons.append(GroupComparison(baseline_group_id=left.group_id if left else None,
                                               variant_group_id=right.group_id if right else None,
                                               variant_case_id=case.case_id, kind=kind, reasons=reasons,
                                               median_differences=differences, search_changes=search_changes))
        return ExperimentReport(experiment_id=experiment_id, batch=batch, source_sha256=hashes,
                                summaries=tuple(summaries), reviews=tuple(reviews), groups=groups,
                                comparisons=tuple(comparisons), warnings=LIMITATIONS)

    def write_report(self, experiment_id: str, *, report_id: str) -> ExperimentReport:
        """Reverify first, then reserve a fresh report directory; never overwrite."""
        validate_run_id(report_id)
        root = checked_root(self.settings, "experiment-reports")
        folder = root / report_id
        if folder.exists() or folder.is_symlink():
            raise ValueError("Report already exists; refusing overwrite")
        report = self.analyze(experiment_id)
        markdown = render_markdown(report, self.settings)
        root.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
        write_json(folder / "report.json", report.model_dump(mode="json"))
        (folder / "report.md").write_text(markdown, encoding="utf-8")
        return report


def _text(value: object) -> str:
    return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ").replace("`", "&#96;").replace("[", "&#91;").replace("]", "&#93;")


def _number(value: float) -> str:
    return format(value, ".12g")


def _link(label: str, path: Path) -> str:
    return f"[{_text(label)}]({quote(str(path), safe='/._-')})"


def render_markdown(report: ExperimentReport, settings: Settings) -> str:
    """A deterministic template using only the freshly verified report model."""
    batch, plan = report.batch, report.batch.plan
    folder = experiment_path(settings, report.experiment_id)
    lines = [f"# 重复实验报告：{_text(report.experiment_id)}", "",
             f"批次状态：**{batch.status}**。开始时间：{_text(batch.started_at)}。", "",
             f"计划 {plan.planned_calls} 次；实际调用 {batch.calls_invoked} 次；复核通过的候选 {sum(s.verified_candidates for s in report.summaries)} 个。",
             f"调用上限 {plan.max_calls}；外部等待预算 {_number(plan.external_wait_budget_sec)} 秒；已预留 {_number(batch.reserved_external_wait_sec)} 秒；批次实际耗时 {_number(batch.elapsed_sec)} 秒。",
             f"失败策略：`{plan.failure_policy}`；按场景与重复编号串行执行，跳过条目不补跑。", "",
             "证据：" + " · ".join(_link(name, folder / name) for name in ("plan.json", "precheck.json", "batch.json")), "",
             "## 实验计划与场景变化", "",
             "| 场景 | backend 要求 | 重复次数 | 内部上限 / 外部 timeout（秒） | 相对基准的场景变化 | 搜索/执行要求变化 |",
             "| --- | --- | ---: | --- | --- | --- |"]
    for case, summary in zip(plan.cases, report.summaries, strict=True):
        def changes(values: tuple) -> str:
            return "; ".join(f"{_text(c.field)}: {_text(c.baseline)} → {_text(c.variant)}" for c in values) or "无"
        lines.append(f"| {_text(case.case_id)}（{_text(case.scenario.scenario_name)}） | {case.backend or '唯一兼容 backend'} | {case.repetitions} | {_number(case.scenario.solver_runtime_limit_sec)} / {_number(case.external_timeout_sec)} | {changes(summary.scenario_changes)} | {changes(summary.search_changes)} |")
    lines += ["", "全部完整 ScenarioSpec 见 plan.json。预检查不会启动进程或探测许可证；它不保证之后的执行环境不发生变化。", ""]
    for check in batch.precheck.cases:
        lines.append(f"- {_text(check.case_id)}：{'通过' if not check.errors else '拒绝'}；选择 `{check.decision.selected_backend or '无'}`；本地文件与时限检查{'通过' if not check.errors else '未全部通过'}；许可证未探测。")
        for error in check.errors:
            lines.append(f"  - {_text(error)}")
    lines += ["", "## 样本数与执行状态", "", "| 场景 | 计划 | 实际调用 | 复核候选 | 执行状态计数 |", "| --- | ---: | ---: | ---: | --- |"]
    for summary in report.summaries:
        counts = "; ".join(f"{key}: {value}" for key, value in summary.execution_status_counts.items())
        lines.append(f"| {_text(summary.case_id)} | {summary.planned} | {summary.invoked} | {summary.verified_candidates} | {_text(counts)} |")
    lines += ["", "## 同口径描述统计", "", "每组使用同一场景、模型/输入签名和搜索条件。样本数仅包含本次重放通过独立复核的候选。", ""]
    if not report.groups:
        lines += ["没有复核通过的候选，指标统计为空。", ""]
    for group in report.groups:
        lines += [f"### {_text(group.case_id)} / {group.group_id[:12]}", "",
                  f"模型：`{_text(group.model_signature.get('backend'))}` / `{_text(group.model_signature.get('objective_profile'))}`；语义：`{_text(group.model_signature.get('semantics'))}`。", "",
                  "完整模型、输入和搜索签名及每次重新计算的验证结果保存在 report.json。", "",
                  "| 指标 | n | 最小值 | 中位数 | 最大值 |", "| --- | ---: | ---: | ---: | ---: |"]
        for metric in group.metrics:
            lines.append(f"| {metric.metric} | {metric.n} | {_number(metric.minimum)} | {_number(metric.median)} | {_number(metric.maximum)} |")
        lines += ["", "运行：" + ", ".join(f"`{run}`" for run in group.run_ids), ""]
    lines += ["## 相对基准的观测", "", "差值为变体中位数减去基准中位数；不代表统计显著性、因果效应或最优性。", ""]
    for comparison in report.comparisons:
        lines.append(f"- **{_text(comparison.variant_case_id)}**：`{comparison.kind}`。")
        if comparison.reasons:
            lines.extend(f"  - {_text(reason)}" for reason in comparison.reasons)
        else:
            lines.extend(f"  - {key} 中位数差：{_number(value)}" for key, value in comparison.median_differences.items())
    lines += ["", "## 每次运行与复核证据", "", "执行状态是当时的记录；复核列是重新读取原始结果和输入后的结论。", "",
              "| 场景 / 重复 | run ID | 执行状态 | 本次复核 | 目标值 | 证据 |", "| --- | --- | --- | --- | ---: | --- |"]
    for index, (attempt, review) in enumerate(zip(batch.attempts, report.reviews, strict=True), 1):
        run = settings.runs_dir / attempt.run_id
        links = [_link("选择/状态", folder / "attempts" / f"{index:05d}.json")] if attempt.status != "pending" else []
        if review.observation:
            links += [_link(name, run / name) for name in ("solution.json", "solver-result.txt", "worker-result.json", "process.json")
                      if name in review.observation.evidence_sha256]
            links.append(_link("完整运行与输入快照", run))
            if "validation" in review.observation.result.raw_artifact_paths:
                links.append(_link("原执行验证记录", review.observation.result.raw_artifact_paths["validation"]))
        elif attempt.invoked:
            links.append(_link("保留的运行目录", run))
        objective = _number(review.observation.result.objective_value) if review.status == "verified_candidate" else "—"
        lines.append(f"| {_text(attempt.case_id)} / {attempt.repetition} | `{attempt.run_id}` | {attempt.status} | {review.status} | {objective} | {' · '.join(links)} |")
    failures = [(a, r) for a, r in zip(batch.attempts, report.reviews, strict=True) if r.status != "verified_candidate"]
    if failures:
        lines += ["", "### 未纳入统计的条目", ""]
        for attempt, review in failures:
            lines.append(f"- `{attempt.run_id}`：{_text(review.diagnostic or attempt.diagnostic or attempt.status)}")
    lines += ["", "## 限制", ""]
    lines.extend(f"- {_text(warning)}" for warning in report.warnings)
    return "\n".join(lines) + "\n"
