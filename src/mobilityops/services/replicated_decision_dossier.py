"""Build a fresh decision dossier from an audited replication campaign."""

from mobilityops.config import Settings
from mobilityops.domain.decision_dossier import DecisionGate
from mobilityops.domain.dossier_campaign_audit import DossierCampaignAuditReport
from mobilityops.domain.experiment import ExperimentReport
from mobilityops.domain.replicated_decision_dossier import (
    ReplicatedDecisionDossier,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.decision_explanation import (
    explain_report,
    propose_next_plan,
    report_fingerprint,
)
from mobilityops.services.dossier_campaign_audit import DossierCampaignAuditService
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_intake import fingerprint


class ReplicatedDecisionDossierService:
    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())

    def build(
        self,
        campaign_audit_id: str,
        *,
        dossier_id: str,
        variant_case_id: str | None = None,
    ) -> ReplicatedDecisionDossier:
        validate_run_id(campaign_audit_id)
        validate_run_id(dossier_id)
        if variant_case_id is not None:
            validate_run_id(variant_case_id)
        saved_audit = self._saved_audit(campaign_audit_id)
        fresh_audit = DossierCampaignAuditService(self.settings).analyze(
            saved_audit.session_id
        )
        if saved_audit != fresh_audit:
            raise ValueError("Saved campaign audit differs from deterministic replay")
        report = ExperimentReportService(self.settings).analyze(
            fresh_audit.experiment_id
        )
        report_sha256 = report_fingerprint(report)
        if report_sha256 != fresh_audit.report_sha256:
            raise ValueError("Campaign report differs from the campaign audit")
        variant_case_id = self._select_variant(report, variant_case_id)
        explanation = explain_report(report, variant_case_id=variant_case_id)
        proposal = propose_next_plan(report, variant_case_id=variant_case_id)
        gates = self._gates(
            fresh_audit,
            report,
            variant_case_id=variant_case_id,
            proposal_blocked=bool(proposal.blockers),
        )
        if any(item.status == "block" for item in gates):
            recommendation = "review_blockers"
        elif any(item.status == "caution" for item in gates):
            recommendation = "collect_replicates"
        else:
            recommendation = "human_decision_review"
        dossier = ReplicatedDecisionDossier(
            dossier_sha256="",
            dossier_id=dossier_id,
            source_campaign_audit_id=campaign_audit_id,
            source_campaign_audit_manifest_sha256=fresh_audit.manifest_sha256,
            parent_dossier_id=fresh_audit.source_dossier_id,
            parent_dossier_sha256=fresh_audit.source_dossier_sha256,
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
        campaign_audit_id: str,
        *,
        dossier_id: str,
        variant_case_id: str | None = None,
    ) -> ReplicatedDecisionDossier:
        dossier = self.build(
            campaign_audit_id,
            dossier_id=dossier_id,
            variant_case_id=variant_case_id,
        )
        root = checked_root(self.settings, "replicated-decision-dossiers")
        folder = root / dossier_id
        if folder.exists() or folder.is_symlink():
            raise ValueError("Replicated decision dossier exists; refusing overwrite")
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

    def _saved_audit(self, audit_id: str) -> DossierCampaignAuditReport:
        root = checked_root(self.settings, "dossier-campaign-audits") / audit_id
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Campaign audit must be an existing ordinary directory")
        return DossierCampaignAuditReport.model_validate(
            strict_json(read_local(root, root / "report.json"))
        )

    @staticmethod
    def _select_variant(
        report: ExperimentReport, variant_case_id: str | None
    ) -> str:
        variants = [case.case_id for case in report.batch.plan.variants]
        if variant_case_id is None:
            if len(variants) != 1:
                raise ValueError("Select one variant for the replicated dossier")
            return variants[0]
        if variant_case_id not in variants:
            raise ValueError(f"Unknown variant case: {variant_case_id}")
        return variant_case_id

    @staticmethod
    def _gates(
        audit: DossierCampaignAuditReport,
        report: ExperimentReport,
        *,
        variant_case_id: str,
        proposal_blocked: bool,
    ) -> tuple[DecisionGate, ...]:
        comparison = next(
            item for item in report.comparisons
            if item.variant_case_id == variant_case_id
        )
        relevant = {
            report.batch.plan.baseline.case_id,
            variant_case_id,
        }
        summaries = [item for item in report.summaries if item.case_id in relevant]
        all_verified = (
            report.batch.status == "completed"
            and report.batch.calls_invoked == report.batch.plan.planned_calls
            and all(item.verified_candidates == item.planned for item in summaries)
        )
        groups = [item for item in report.groups if item.case_id in relevant]
        minimum_n = min(
            metric.n for group in groups for metric in group.metrics
        ) if groups else 0
        comparable = (
            comparison.kind == "scenario_observations"
            and not comparison.reasons
            and bool(comparison.median_differences)
        )
        return (
            DecisionGate(
                gate="evidence_integrity",
                status="pass",
                rationale="重复实验审计已重放 Dossier、确认、run、报告和谱系。",
                evidence=(
                    f"dossier-campaign-audits/{audit.manifest_sha256}",
                    f"report-sha256/{audit.report_sha256}",
                ),
            ),
            DecisionGate(
                gate="execution_completion",
                status="pass" if all_verified else "block",
                rationale=(
                    "重复计划全部完成且候选通过独立复核。"
                    if all_verified else
                    "重复计划不完整或存在未通过独立复核的条目。"
                ),
                evidence=tuple(
                    f"runs/{item.run_id}" for item in report.batch.attempts
                    if item.case_id in relevant
                ),
            ),
            DecisionGate(
                gate="comparison_validity",
                status="pass" if comparable else "block",
                rationale=(
                    "基准与变体保持同模型、输入和搜索口径。"
                    if comparable else
                    "基准与变体不能形成同口径指标差。"
                ),
                evidence=tuple(
                    value for value in (
                        comparison.baseline_group_id,
                        comparison.variant_group_id,
                    ) if value is not None
                ),
            ),
            DecisionGate(
                gate="evidence_strength",
                status="pass" if minimum_n >= 3 else "caution",
                rationale=(
                    f"本次重复实验相关分组最小样本数为 {minimum_n}；"
                    + (
                        "达到当前描述性决策门槛。"
                        if minimum_n >= 3 else
                        "尚未达到当前描述性决策门槛。"
                    )
                ),
                evidence=tuple(group.group_id for group in groups),
            ),
            DecisionGate(
                gate="next_action",
                status="block" if proposal_blocked else "pass",
                rationale=(
                    "确定性规则拒绝生成后续候选。"
                    if proposal_blocked else
                    "证据已可交由人类审阅；任何后续实验仍须新确认。"
                ),
                evidence=(f"experiment-report/{audit.report_sha256}",),
            ),
        )


def render_markdown(dossier: ReplicatedDecisionDossier) -> str:
    lines = [
        f"# 重复实验决策证据包：{dossier.dossier_id}",
        "",
        f"建议：**{dossier.recommendation}**；变体："
        f"`{dossier.selected_variant_case_id}`。",
        f"指纹：`{dossier.dossier_sha256}`。",
        f"来源 campaign audit：`{dossier.source_campaign_audit_id}` / "
        f"`{dossier.source_campaign_audit_manifest_sha256}`。",
        f"父 Dossier：`{dossier.parent_dossier_id}` / "
        f"`{dossier.parent_dossier_sha256}`。",
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
    for title, claims in (
        ("已复核事实", dossier.explanation.verified_facts),
        ("本批描述性观察", dossier.explanation.descriptive_observations),
        ("不能得出的结论", dossier.explanation.cannot_conclude),
    ):
        lines += ["", f"## {title}", ""]
        lines.extend(f"- [{claim.code}] {claim.text}" for claim in claims)
    lines += ["", "## 后续候选", ""]
    proposal = dossier.next_experiment
    if proposal.plan is None:
        lines.append("未生成：" + "；".join(proposal.blockers))
    else:
        lines.append(
            f"计划 {proposal.plan.planned_calls} 次；"
            "`auto_execute=false`，不属于当前授权。"
        )
    return "\n".join(lines) + "\n"
