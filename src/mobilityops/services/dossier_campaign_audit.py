"""Read-only replay of a completed dossier-bound replication campaign."""

import hashlib
import hmac
from pathlib import Path

from mobilityops.config import Settings
from mobilityops.domain.audited_execution_audit import AuditedExecutionAuditReport
from mobilityops.domain.decision_dossier import AuditedDecisionDossier
from mobilityops.domain.dossier_campaign import DossierCampaignRequest
from mobilityops.domain.dossier_campaign_audit import (
    DossierCampaignAuditFile,
    DossierCampaignAuditReport,
)
from mobilityops.domain.experiment import ExperimentReport
from mobilityops.domain.next_experiment import NextExperimentExecutionRequest
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.decision_dossier import DecisionDossierService
from mobilityops.services.decision_explanation import report_fingerprint
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_intake import fingerprint


MAX_JSON_BYTES = 2_000_000
MAX_FILES = 20_000
WARNINGS = (
    "审计重新验证 Dossier、来源审计、确认、原始 run、独立结果、报告和谱系。",
    "审计不调用 Gemini、solver、许可证环境或外部子进程。",
    "重复实验只提供描述性观测，不构成因果效应、统计显著性或最优性证明。",
)


class DossierCampaignAuditService:
    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())

    def analyze(self, session_id: str) -> DossierCampaignAuditReport:
        validate_run_id(session_id)
        session_root = (
            checked_root(self.settings, "dossier-campaign-sessions") / session_id
        )
        if not session_root.is_dir() or session_root.is_symlink():
            raise ValueError("Dossier campaign session must be an ordinary directory")
        session = self._object(session_root, session_root / "session.json")
        state = self._object(session_root, session_root / "state.json")
        experiment_id = f"campaign-{session_id}"
        if (
            session.get("format") != "dossier_campaign_terminal_session_v1"
            or session.get("mode") != "dossier-campaign"
            or session.get("session_id") != session_id
            or session.get("experiment_id") != experiment_id
            or session.get("report_id") != experiment_id
        ):
            raise ValueError("Dossier campaign session identity is invalid")
        if (
            state.get("format") != session.get("format")
            or state.get("mode") != "dossier-campaign"
            or state.get("session_id") != session_id
            or state.get("status") != "reported"
            or state.get("phase") != "reporting"
        ):
            raise ValueError("Dossier campaign final state is incomplete")
        self._verify_events(session_root, state)

        request = DossierCampaignRequest.model_validate(
            self._object(session_root, session_root / "request.json")
        )
        child = request.execution_request
        if (
            request.request_sha256
            != fingerprint(request.model_dump(mode="json", exclude={"request_sha256"}))
            or child.request_sha256
            != fingerprint(child.model_dump(mode="json", exclude={"request_sha256"}))
            or child.experiment_id != experiment_id
            or child.report_id != experiment_id
        ):
            raise ValueError("Dossier campaign request identity or fingerprint is invalid")
        review = self._object(session_root, session_root / "review.json")
        review_sha256 = review.get("review_sha256")
        review_body = {
            key: value for key, value in review.items() if key != "review_sha256"
        }
        if (
            review.get("format") != "dossier_campaign_execution_review_v1"
            or review.get("request") != request.model_dump(mode="json")
            or not isinstance(review_sha256, str)
            or not hmac.compare_digest(review_sha256, fingerprint(review_body))
        ):
            raise ValueError("Dossier campaign terminal review is invalid")
        source_dossier_root = (
            checked_root(self.settings, "decision-dossiers")
            / request.source_dossier_id
        )
        source_dossier = AuditedDecisionDossier.model_validate(
            self._object(source_dossier_root, source_dossier_root / "dossier.json")
        )
        fresh_dossier = DecisionDossierService(self.settings).build(
            source_dossier.source_execution_audit_id,
            dossier_id=source_dossier.dossier_id,
            variant_case_id=source_dossier.selected_variant_case_id,
        )
        if (
            source_dossier != fresh_dossier
            or source_dossier != request.dossier
            or source_dossier.dossier_sha256 != request.source_dossier_sha256
            or source_dossier.recommendation != "collect_replicates"
            or self._object(source_dossier_root, source_dossier_root / "next-plan.json")
            != source_dossier.next_experiment.model_dump(mode="json")
        ):
            raise ValueError("Source decision dossier differs from deterministic replay")

        request_root = (
            checked_root(self.settings, "dossier-campaign-requests") / experiment_id
        )
        saved_request = DossierCampaignRequest.model_validate(
            self._object(request_root, request_root / "request.json")
        )
        saved_dossier = AuditedDecisionDossier.model_validate(
            self._object(request_root, request_root / "dossier.json")
        )
        request_state = self._object(request_root, request_root / "state.json")
        if saved_request != request or saved_dossier != source_dossier:
            raise ValueError("Confirmed dossier campaign request differs from review")

        child_root = checked_root(self.settings, "next-experiment-requests") / experiment_id
        saved_child = NextExperimentExecutionRequest.model_validate(
            self._object(child_root, child_root / "request.json")
        )
        child_state = self._object(child_root, child_root / "state.json")
        if saved_child != child:
            raise ValueError("Child request differs from dossier campaign request")
        for label, value, sha256 in (
            ("campaign", request_state, request.request_sha256),
            ("child", child_state, child.request_sha256),
        ):
            if value.get("status") != "reported" or value.get("request_sha256") != sha256:
                raise ValueError(f"Confirmed {label} request did not reach reported state")

        fresh_report = ExperimentReportService(self.settings).analyze(experiment_id)
        report_sha256 = report_fingerprint(fresh_report)
        report_root = checked_root(self.settings, "experiment-reports") / experiment_id
        saved_report = ExperimentReport.model_validate(
            self._object(report_root, report_root / "report.json")
        )
        if saved_report != fresh_report:
            raise ValueError("Saved campaign report differs from fresh evidence replay")
        if (
            fresh_report.batch.plan != child.plan
            or fresh_report.batch.precheck != child.precheck
        ):
            raise ValueError("Campaign batch differs from confirmed plan or precheck")
        verified = sum(item.verified_candidates for item in fresh_report.summaries)
        if (
            fresh_report.batch.status != "completed"
            or fresh_report.batch.calls_invoked != child.plan.planned_calls
            or verified != child.plan.planned_calls
        ):
            raise ValueError("Dossier campaign is not a verified complete batch")

        lineage = self._object(request_root, request_root / "lineage.json")
        lineage_sha256 = lineage.get("lineage_sha256")
        lineage_body = {
            key: value for key, value in lineage.items() if key != "lineage_sha256"
        }
        expected_lineage = {
            "format": "dossier_campaign_lineage_v1",
            "source_dossier_id": request.source_dossier_id,
            "source_dossier_sha256": request.source_dossier_sha256,
            "source_execution_audit_id": request.source_execution_audit_id,
            "source_execution_audit_manifest_sha256": (
                request.source_execution_audit_manifest_sha256
            ),
            "request_sha256": request.request_sha256,
            "child_request_sha256": child.request_sha256,
            "child_experiment_id": experiment_id,
            "child_report_sha256": report_sha256,
        }
        if (
            lineage_body != expected_lineage
            or not isinstance(lineage_sha256, str)
            or not hmac.compare_digest(lineage_sha256, fingerprint(lineage_body))
        ):
            raise ValueError("Dossier campaign lineage is invalid")
        if (
            request_state.get("lineage_sha256") != lineage_sha256
            or state.get("lineage_sha256") != lineage_sha256
            or state.get("request_sha256") != request.request_sha256
            or state.get("review_sha256") != review_sha256
            or state.get("confirmed_request_sha256") != request.request_sha256
            or state.get("confirmed_review_sha256") != review_sha256
            or state.get("batch_status") != fresh_report.batch.status
            or state.get("calls_invoked") != fresh_report.batch.calls_invoked
            or state.get("verified_candidates") != verified
        ):
            raise ValueError("Campaign terminal state differs from replayed execution")

        source_audit_root = (
            checked_root(self.settings, "audited-execution-audits")
            / request.source_execution_audit_id
        )
        source_audit = AuditedExecutionAuditReport.model_validate(
            self._object(source_audit_root, source_audit_root / "report.json")
        )
        if (
            source_audit.manifest_sha256
            != request.source_execution_audit_manifest_sha256
        ):
            raise ValueError("Source execution audit manifest differs from request")
        roots: list[tuple[str, Path]] = [
            ("session", session_root),
            ("source_dossier", source_dossier_root),
            ("source_audit", source_audit_root),
            ("request", request_root),
            ("request", child_root),
            ("experiment", checked_root(self.settings, "experiments") / experiment_id),
            ("report", report_root),
        ]
        for attempt in fresh_report.batch.attempts:
            run_root = self.settings.runs_dir / attempt.run_id
            if run_root.exists() or run_root.is_symlink():
                roots.append(("run", run_root))
        files = self._manifest(roots)
        manifest_sha256 = fingerprint(
            [item.model_dump(mode="json") for item in files]
        )
        return DossierCampaignAuditReport(
            session_id=session_id,
            source_dossier_id=request.source_dossier_id,
            source_dossier_sha256=request.source_dossier_sha256,
            source_execution_audit_id=request.source_execution_audit_id,
            source_execution_audit_manifest_sha256=(
                request.source_execution_audit_manifest_sha256
            ),
            experiment_id=experiment_id,
            request_sha256=request.request_sha256,
            child_request_sha256=child.request_sha256,
            review_sha256=review_sha256,
            lineage_sha256=lineage_sha256,
            report_sha256=report_sha256,
            batch_status=fresh_report.batch.status,
            calls_invoked=fresh_report.batch.calls_invoked,
            verified_candidates=verified,
            manifest_sha256=manifest_sha256,
            files=files,
            warnings=WARNINGS,
        )

    def write_report(
        self, session_id: str, *, audit_id: str
    ) -> DossierCampaignAuditReport:
        validate_run_id(audit_id)
        report = self.analyze(session_id)
        root = checked_root(self.settings, "dossier-campaign-audits")
        folder = root / audit_id
        if folder.exists() or folder.is_symlink():
            raise ValueError("Dossier campaign audit already exists; refusing overwrite")
        root.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
        write_json(folder / "report.json", report.model_dump(mode="json"))
        (folder / "report.md").write_text(render_markdown(report), encoding="utf-8")
        return report

    def _verify_events(self, root: Path, state: dict) -> None:
        data = read_local(root, root / "events.jsonl")
        if len(data) > MAX_JSON_BYTES:
            raise ValueError("Dossier campaign event log exceeds size limit")
        events = []
        for line in data.splitlines():
            value = strict_json(line)
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("at"), str)
                or value.get("format") != "dossier_campaign_terminal_session_v1"
                or value.get("mode") != "dossier-campaign"
            ):
                raise ValueError("Dossier campaign event log is invalid")
            events.append(value)
        if not events:
            raise ValueError("Dossier campaign event log is empty")
        final = {key: value for key, value in events[-1].items() if key != "at"}
        if final != state:
            raise ValueError("Dossier campaign final event differs from state")

    def _object(self, root: Path, path: Path) -> dict:
        data = read_local(root, path)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError(f"Campaign audit evidence exceeds size limit: {path.name}")
        value = strict_json(data)
        if not isinstance(value, dict):
            raise ValueError(f"Campaign audit evidence must be an object: {path.name}")
        return value

    def _manifest(
        self, roots: list[tuple[str, Path]]
    ) -> tuple[DossierCampaignAuditFile, ...]:
        files = []
        seen = set()
        runs = self.settings.runs_dir.resolve()
        for scope, root in roots:
            if (
                not root.is_dir()
                or root.is_symlink()
                or not root.resolve().is_relative_to(runs)
            ):
                raise ValueError("Dossier campaign evidence root is invalid")
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Dossier campaign evidence cannot use symlinks")
                if not path.is_file():
                    continue
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Dossier campaign evidence escapes its root")
                relative = str(path.relative_to(self.settings.runs_dir))
                if relative in seen:
                    continue
                seen.add(relative)
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                files.append(DossierCampaignAuditFile(
                    scope=scope,
                    path=relative,
                    sha256=digest.hexdigest(),
                    size_bytes=size,
                ))
                if len(files) > MAX_FILES:
                    raise ValueError("Dossier campaign audit contains too many files")
        return tuple(sorted(files, key=lambda item: item.path))


def render_markdown(report: DossierCampaignAuditReport) -> str:
    lines = [
        f"# Dossier 重复实验审计：{report.session_id}",
        "",
        f"状态：**{report.audit_status}**；实验：`{report.experiment_id}`。",
        f"来源 Dossier：`{report.source_dossier_id}` / "
        f"`{report.source_dossier_sha256}`。",
        f"请求 `{report.request_sha256}`；审阅 `{report.review_sha256}`；"
        f"谱系 `{report.lineage_sha256}`。",
        "",
        f"调用 {report.calls_invoked} 次；独立复核候选 "
        f"{report.verified_candidates} 个；报告 `{report.report_sha256}`。",
        f"清单文件 {len(report.files)} 个；指纹 `{report.manifest_sha256}`。",
        "",
        "| 范围 | 路径 | 字节 | SHA-256 |",
        "| --- | --- | ---: | --- |",
    ]
    lines.extend(
        f"| {item.scope} | `{item.path}` | {item.size_bytes} | `{item.sha256}` |"
        for item in report.files
    )
    lines += ["", "## 限制", ""]
    lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines) + "\n"
