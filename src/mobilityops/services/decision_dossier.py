"""Build a decision dossier only from freshly replayed audited execution evidence."""

from pathlib import Path

from mobilityops.config import Settings
from mobilityops.domain.audited_execution_audit import AuditedExecutionAuditReport
from mobilityops.domain.decision_dossier import (
    AuditedDecisionDossier,
    DecisionGate,
)
from mobilityops.domain.experiment import ExperimentReport
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.audited_execution_audit import (
    AuditedExecutionAuditService,
)
from mobilityops.services.decision_explanation import (
    explain_report,
    propose_next_plan,
    report_fingerprint,
)
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_intake import fingerprint


class DecisionDossierService:
    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())

    def build(
        self,
        execution_audit_id: str,
        *,
        dossier_id: str,
        variant_case_id: str | None = None,
    ) -> AuditedDecisionDossier:
        validate_run_id(execution_audit_id)
        validate_run_id(dossier_id)
        if variant_case_id is not None:
            validate_run_id(variant_case_id)
        saved_audit = self._saved_audit(execution_audit_id)
        fresh_audit = AuditedExecutionAuditService(self.settings).analyze(
            saved_audit.session_id
        )
        if saved_audit != fresh_audit:
            raise ValueError(
                "Saved audited-execution audit differs from deterministic replay"
            )
        report = ExperimentReportService(self.settings).analyze(
            fresh_audit.experiment_id
        )
        report_sha256 = report_fingerprint(report)
        if report_sha256 != fresh_audit.report_sha256:
            raise ValueError("Experiment report differs from the execution audit")
        variant_case_id = self._select_variant(report, variant_case_id)
        explanation = explain_report(report, variant_case_id=variant_case_id)
        proposal = propose_next_plan(report, variant_case_id=variant_case_id)
        gates = self._gates(
            fresh_audit, report, variant_case_id=variant_case_id,
            proposal_blocked=bool(proposal.blockers),
        )
        if any(item.status == "block" for item in gates):
            recommendation = "review_blockers"
        elif any(item.status == "caution" for item in gates):
            recommendation = "collect_replicates"
        else:
            recommendation = "human_decision_review"
        dossier = AuditedDecisionDossier(
            dossier_sha256="",
            dossier_id=dossier_id,
            source_execution_audit_id=execution_audit_id,
            source_execution_audit_manifest_sha256=fresh_audit.manifest_sha256,
            source_session_id=fresh_audit.session_id,
            experiment_id=fresh_audit.experiment_id,
            source_report_sha256=report_sha256,
            selected_variant_case_id=variant_case_id,
            recommendation=recommendation,
            gates=gates,
            explanation=explanation,
            next_experiment=proposal,
        )
        return dossier.model_copy(update={
            "dossier_sha256": fingerprint(
                dossier.model_dump(mode="json", exclude={"dossier_sha256"})
            )
        })

    def write(
        self,
        execution_audit_id: str,
        *,
        dossier_id: str,
        variant_case_id: str | None = None,
    ) -> AuditedDecisionDossier:
        dossier = self.build(
            execution_audit_id,
            dossier_id=dossier_id,
            variant_case_id=variant_case_id,
        )
        root = checked_root(self.settings, "decision-dossiers")
        folder = root / dossier_id
        if folder.exists() or folder.is_symlink():
            raise ValueError("Decision dossier already exists; refusing overwrite")
        root.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
        write_json(folder / "dossier.json", dossier.model_dump(mode="json"))
        write_json(
            folder / "next-plan.json",
            dossier.next_experiment.model_dump(mode="json"),
        )
        (folder / "dossier.md").write_text(
            render_markdown(dossier), encoding="utf-8"
        )
        return dossier

    def _saved_audit(self, audit_id: str) -> AuditedExecutionAuditReport:
        root = checked_root(self.settings, "audited-execution-audits") / audit_id
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Execution audit must be an existing ordinary directory")
        return AuditedExecutionAuditReport.model_validate(
            strict_json(read_local(root, root / "report.json"))
        )

    @staticmethod
    def _select_variant(
        report: ExperimentReport, variant_case_id: str | None
    ) -> str:
        variants = [case.case_id for case in report.batch.plan.variants]
        if variant_case_id is None:
            if len(variants) != 1:
                raise ValueError(
                    "Select one variant explicitly when the experiment has zero or multiple variants"
                )
            return variants[0]
        if variant_case_id not in variants:
            raise ValueError(f"Unknown variant case: {variant_case_id}")
        return variant_case_id

    @staticmethod
    def _gates(
        audit: AuditedExecutionAuditReport,
        report: ExperimentReport,
        *,
        variant_case_id: str,
        proposal_blocked: bool,
    ) -> tuple[DecisionGate, ...]:
        comparison = next(
            item for item in report.comparisons
            if item.variant_case_id == variant_case_id
        )
        all_verified = (
            report.batch.status == "completed"
            and report.batch.calls_invoked == report.batch.plan.planned_calls
            and all(
                summary.verified_candidates == summary.planned
                for summary in report.summaries
                if summary.case_id in {
                    report.batch.plan.baseline.case_id, variant_case_id
                }
            )
        )
        relevant_groups = [
            group for group in report.groups
            if group.case_id in {
                report.batch.plan.baseline.case_id, variant_case_id
            }
        ]
        minimum_n = min(
            metric.n for group in relevant_groups for metric in group.metrics
        ) if relevant_groups else 0
        comparable = (
            comparison.kind == "scenario_observations"
            and not comparison.reasons
            and bool(comparison.median_differences)
        )
        return (
            DecisionGate(
                gate="evidence_integrity", status="pass",
                rationale="执行后审计已重新验证来源、确认、原始 run、报告和谱系。",
                evidence=(
                    f"audited-execution-audits/{audit.manifest_sha256}",
                    f"report-sha256/{audit.report_sha256}",
                ),
            ),
            DecisionGate(
                gate="execution_completion",
                status="pass" if all_verified else "block",
                rationale=(
                    "所选基准与变体按计划完成且候选全部通过独立复核。"
                    if all_verified else
                    "执行不完整或存在未通过独立复核的计划条目。"
                ),
                evidence=tuple(
                    f"runs/{attempt.run_id}" for attempt in report.batch.attempts
                    if attempt.case_id in {
                        report.batch.plan.baseline.case_id, variant_case_id
                    }
                ),
            ),
            DecisionGate(
                gate="comparison_validity",
                status="pass" if comparable else "block",
                rationale=(
                    "基准与变体具有可对齐的模型、输入和搜索口径。"
                    if comparable else
                    "基准与变体当前不能形成同口径指标差。"
                ),
                evidence=tuple(
                    item for item in (
                        comparison.baseline_group_id,
                        comparison.variant_group_id,
                    ) if item is not None
                ),
            ),
            DecisionGate(
                gate="evidence_strength",
                status="pass" if minimum_n >= 3 else "caution",
                rationale=(
                    f"相关分组最小样本数为 {minimum_n}；"
                    + (
                        "达到当前重复实验候选的最低描述性门槛。"
                        if minimum_n >= 3 else
                        "只足以描述本批运行，应先收集独立重复样本。"
                    )
                ),
                evidence=tuple(group.group_id for group in relevant_groups),
            ),
            DecisionGate(
                gate="next_action",
                status="block" if proposal_blocked else "pass",
                rationale=(
                    "下一轮候选被确定性 blocker 拒绝。"
                    if proposal_blocked else
                    "已生成两个 case 各 3 次的有限候选；不会自动执行。"
                ),
                evidence=(f"experiment-report/{audit.report_sha256}",),
            ),
        )


def render_markdown(dossier: AuditedDecisionDossier) -> str:
    lines = [
        f"# 决策证据包：{dossier.dossier_id}",
        "",
        f"建议状态：**{dossier.recommendation}**。所选变体："
        f"`{dossier.selected_variant_case_id}`。",
        "",
        f"证据包指纹：`{dossier.dossier_sha256}`。",
        f"来源执行审计：`{dossier.source_execution_audit_id}` / "
        f"`{dossier.source_execution_audit_manifest_sha256}`。",
        f"来源实验报告：`{dossier.experiment_id}` / "
        f"`{dossier.source_report_sha256}`。",
        "",
        "## 决策门",
        "",
        "| 门 | 状态 | 依据 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {item.gate} | {item.status} | {item.rationale} |"
        for item in dossier.gates
    )
    sections = (
        ("已复核事实", dossier.explanation.verified_facts),
        ("本批描述性观察", dossier.explanation.descriptive_observations),
        ("不能得出的结论", dossier.explanation.cannot_conclude),
    )
    for title, claims in sections:
        lines += ["", f"## {title}", ""]
        lines.extend(f"- [{claim.code}] {claim.text}" for claim in claims)
    proposal = dossier.next_experiment
    lines += ["", "## 下一轮有限候选", ""]
    if proposal.plan is None:
        lines.append("未生成计划：" + "；".join(proposal.blockers))
    else:
        lines.append(
            f"计划 {proposal.plan.planned_calls} 次 / 上限 {proposal.plan.max_calls} 次；"
            f"等待预算 {proposal.plan.external_wait_budget_sec:g} 秒；"
            "`auto_execute=false`，仍需新的预检查、审阅与明确确认。"
        )
        lines.extend(f"- {item}" for item in proposal.rationale)
    return "\n".join(lines) + "\n"
