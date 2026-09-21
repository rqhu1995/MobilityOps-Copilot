"""Read-only replay of an audit-bound execution and all child run evidence."""

import hashlib
import hmac
from pathlib import Path

from mobilityops.config import Settings
from mobilityops.domain.audited_execution import AuditedExecutionRequest
from mobilityops.domain.audited_execution_audit import (
    AuditedExecutionAuditFile,
    AuditedExecutionAuditReport,
)
from mobilityops.domain.copilot_audit import CopilotAuditReport
from mobilityops.domain.experiment import ExperimentReport
from mobilityops.domain.experiment_intake import (
    ExperimentPlanDraft,
    ExperimentPlanExecutionRequest,
)
from mobilityops.domain.solution import validate_run_id
from mobilityops.services.copilot_audit import CopilotAuditService
from mobilityops.services.decision_explanation import report_fingerprint
from mobilityops.services.experiment_report import ExperimentReportService
from mobilityops.services.experiment_service import checked_root, write_json
from mobilityops.services.gurobi_inputs import read_local, strict_json
from mobilityops.services.scenario_intake import fingerprint


MAX_JSON_BYTES = 2_000_000
MAX_FILES = 20_000
WARNINGS = (
    "审计重新验证本地请求、运行快照、独立结果和报告；散列不能证明证据在协调修改前的真实性。",
    "审计不调用 Gemini、solver、许可证环境或外部子进程。",
    "本批差异仍是小样本描述性观察，不构成因果效应、统计显著性或最优性证明。",
)


class AuditedExecutionAuditService:
    def __init__(self, settings: Settings) -> None:
        self.settings = Settings.model_validate(settings.model_dump())

    def analyze(self, session_id: str) -> AuditedExecutionAuditReport:
        validate_run_id(session_id)
        session_root = (
            checked_root(self.settings, "audited-execution-sessions") / session_id
        )
        if not session_root.is_dir() or session_root.is_symlink():
            raise ValueError(
                "Audited-execution session must be an existing ordinary directory"
            )
        session = self._object(session_root, session_root / "session.json")
        state = self._object(session_root, session_root / "state.json")
        experiment_id = f"audited-{session_id}"
        if (
            session.get("format") != "audited_execution_terminal_session_v1"
            or session.get("mode") != "audited-execution"
            or session.get("session_id") != session_id
            or session.get("experiment_id") != experiment_id
            or session.get("report_id") != experiment_id
        ):
            raise ValueError("Audited-execution session identity is invalid")
        if (
            state.get("format") != session.get("format")
            or state.get("session_id") != session_id
            or state.get("mode") != "audited-execution"
            or state.get("status") != "reported"
            or state.get("phase") != "reporting"
        ):
            raise ValueError("Audited-execution final state is incomplete")
        self._verify_events(session_root, state)

        request = AuditedExecutionRequest.model_validate(
            self._object(session_root, session_root / "request.json")
        )
        expected_request_sha = fingerprint(
            request.model_dump(mode="json", exclude={"request_sha256"})
        )
        if not hmac.compare_digest(request.request_sha256, expected_request_sha):
            raise ValueError("Audit-bound request fingerprint is invalid")
        child_request = request.execution_request
        expected_child_sha = fingerprint(
            child_request.model_dump(mode="json", exclude={"request_sha256"})
        )
        if (
            child_request.request_sha256 != expected_child_sha
            or child_request.experiment_id != experiment_id
            or child_request.report_id != experiment_id
        ):
            raise ValueError("Child execution request identity or fingerprint is invalid")
        review = self._object(session_root, session_root / "review.json")
        review_sha256 = review.get("review_sha256")
        review_body = {
            key: value for key, value in review.items() if key != "review_sha256"
        }
        if (
            review.get("format") != "audited_execution_review_v1"
            or review.get("request") != request.model_dump(mode="json")
            or not isinstance(review_sha256, str)
            or not hmac.compare_digest(review_sha256, fingerprint(review_body))
        ):
            raise ValueError("Audit-bound terminal review is invalid")
        self._verify_source(request)

        request_root = (
            checked_root(self.settings, "audited-execution-requests")
            / experiment_id
        )
        saved_request = AuditedExecutionRequest.model_validate(
            self._object(request_root, request_root / "request.json")
        )
        saved_draft = ExperimentPlanDraft.model_validate(
            self._object(request_root, request_root / "draft.json")
        )
        request_state = self._object(request_root, request_root / "state.json")
        if saved_request != request or saved_draft.draft_sha256 != request.source_draft_sha256:
            raise ValueError("Confirmed audit-bound request differs from its review")

        language_root = (
            checked_root(self.settings, "language-experiment-requests")
            / experiment_id
        )
        language_request = ExperimentPlanExecutionRequest.model_validate(
            self._object(language_root, language_root / "request.json")
        )
        language_draft = ExperimentPlanDraft.model_validate(
            self._object(language_root, language_root / "draft.json")
        )
        language_state = self._object(language_root, language_root / "state.json")
        if language_request != child_request or language_draft != saved_draft:
            raise ValueError("Child language request differs from the audit-bound request")
        for label, value, sha256 in (
            ("audit-bound", request_state, request.request_sha256),
            ("language", language_state, child_request.request_sha256),
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
            raise ValueError("Saved child report differs from fresh evidence replay")
        if (
            fresh_report.batch.plan != child_request.plan
            or fresh_report.batch.precheck != child_request.precheck
        ):
            raise ValueError("Child batch differs from the confirmed plan or precheck")
        verified = sum(item.verified_candidates for item in fresh_report.summaries)

        lineage = self._object(request_root, request_root / "lineage.json")
        lineage_sha256 = lineage.get("lineage_sha256")
        lineage_body = {
            key: value for key, value in lineage.items() if key != "lineage_sha256"
        }
        expected_lineage = {
            "format": "audited_execution_lineage_v1",
            "source_session_id": request.source_session_id,
            "source_audit_id": request.source_audit_id,
            "source_audit_manifest_sha256": request.source_audit_manifest_sha256,
            "source_review_sha256": request.source_review_sha256,
            "source_execution_request_sha256": (
                request.source_execution_request_sha256
            ),
            "request_sha256": request.request_sha256,
            "child_experiment_id": experiment_id,
            "child_report_sha256": report_sha256,
        }
        if (
            lineage_body != expected_lineage
            or not isinstance(lineage_sha256, str)
            or not hmac.compare_digest(lineage_sha256, fingerprint(lineage_body))
        ):
            raise ValueError("Audit-bound lineage is invalid")
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
            raise ValueError("Terminal state differs from the replayed execution")

        experiment_root = checked_root(self.settings, "experiments") / experiment_id
        source_audit_root = (
            checked_root(self.settings, "copilot-audits") / request.source_audit_id
        )
        roots: list[tuple[str, Path]] = [
            ("session", session_root),
            ("source_audit", source_audit_root),
            ("request", request_root),
            ("request", language_root),
            ("experiment", experiment_root),
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
        return AuditedExecutionAuditReport(
            session_id=session_id,
            source_session_id=request.source_session_id,
            source_audit_id=request.source_audit_id,
            experiment_id=experiment_id,
            request_sha256=request.request_sha256,
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
    ) -> AuditedExecutionAuditReport:
        validate_run_id(audit_id)
        report = self.analyze(session_id)
        root = checked_root(self.settings, "audited-execution-audits")
        folder = root / audit_id
        if folder.exists() or folder.is_symlink():
            raise ValueError("Audited-execution audit already exists; refusing overwrite")
        root.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
        write_json(folder / "report.json", report.model_dump(mode="json"))
        (folder / "report.md").write_text(
            render_markdown(report), encoding="utf-8"
        )
        return report

    def _verify_source(self, request: AuditedExecutionRequest) -> None:
        audit = CopilotAuditService(self.settings).analyze(request.source_session_id)
        if (
            audit.audit_status != "verified_no_execution"
            or audit.session_status != "completed"
            or audit.execution.executed
            or audit.manifest_sha256 != request.source_audit_manifest_sha256
        ):
            raise ValueError("Source Copilot audit no longer matches the request")
        audit_root = (
            checked_root(self.settings, "copilot-audits") / request.source_audit_id
        )
        saved_audit = CopilotAuditReport.model_validate(
            self._object(audit_root, audit_root / "report.json")
        )
        if saved_audit != audit or saved_audit.session_id != request.source_session_id:
            raise ValueError("Saved source audit differs from deterministic replay")
        source_root = (
            checked_root(self.settings, "copilot-sessions")
            / request.source_session_id
        )
        reviews = sorted(source_root.glob("turn-*-execution-review.json"))
        if len(reviews) != 1:
            raise ValueError("Source session must contain exactly one execution review")
        review = self._object(source_root, reviews[0])
        review_sha256 = review.get("review_sha256")
        review_body = {
            key: value for key, value in review.items() if key != "review_sha256"
        }
        source_request = ExperimentPlanExecutionRequest.model_validate(
            review.get("request")
        )
        if (
            review.get("format") != "copilot_execution_review_v1"
            or review.get("source") != "draft"
            or review_sha256 != request.source_review_sha256
            or review_sha256 != fingerprint(review_body)
            or source_request.request_sha256
            != request.source_execution_request_sha256
        ):
            raise ValueError("Source execution review differs from the request")
        drafts = [
            ExperimentPlanDraft.model_validate(self._object(source_root, path))
            for path in sorted(source_root.glob("turn-*-plan-draft.json"))
        ]
        matching = [
            draft for draft in drafts
            if draft.draft_sha256 == request.source_draft_sha256
        ]
        if len(matching) != 1:
            raise ValueError("Source request does not bind exactly one plan draft")
        child = request.execution_request
        if (
            source_request.draft_sha256 != request.source_draft_sha256
            or child.draft_sha256 != request.source_draft_sha256
            or child.plan != source_request.plan
            or child.precheck != source_request.precheck
            or child.execution_configurations
            != source_request.execution_configurations
        ):
            raise ValueError("Child plan, precheck or configuration differs from source")

    def _verify_events(self, root: Path, state: dict) -> None:
        data = read_local(root, root / "events.jsonl")
        if len(data) > MAX_JSON_BYTES:
            raise ValueError("Audited-execution event log exceeds size limit")
        events = []
        for line in data.splitlines():
            value = strict_json(line)
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("at"), str)
                or value.get("format")
                != "audited_execution_terminal_session_v1"
                or value.get("mode") != "audited-execution"
            ):
                raise ValueError("Audited-execution event log is invalid")
            events.append(value)
        if not events:
            raise ValueError("Audited-execution event log is empty")
        final = {key: value for key, value in events[-1].items() if key != "at"}
        if final != state:
            raise ValueError("Audited-execution final event differs from state")

    def _object(self, root: Path, path: Path) -> dict:
        data = read_local(root, path)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError(f"Audit evidence exceeds size limit: {path.name}")
        value = strict_json(data)
        if not isinstance(value, dict):
            raise ValueError(f"Audit evidence must be a JSON object: {path.name}")
        return value

    def _manifest(
        self, roots: list[tuple[str, Path]]
    ) -> tuple[AuditedExecutionAuditFile, ...]:
        files = []
        seen = set()
        runs = self.settings.runs_dir.resolve()
        for scope, root in roots:
            if (
                not root.is_dir()
                or root.is_symlink()
                or not root.resolve().is_relative_to(runs)
            ):
                raise ValueError("Audited-execution evidence root is invalid")
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Audited-execution evidence cannot use symlinks")
                if not path.is_file():
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(root.resolve()):
                    raise ValueError("Audited-execution evidence escapes its root")
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
                files.append(AuditedExecutionAuditFile(
                    scope=scope,
                    path=relative,
                    sha256=digest.hexdigest(),
                    size_bytes=size,
                ))
                if len(files) > MAX_FILES:
                    raise ValueError(
                        "Audited-execution audit contains too many files"
                    )
        return tuple(sorted(files, key=lambda item: item.path))


def render_markdown(report: AuditedExecutionAuditReport) -> str:
    lines = [
        f"# 审计绑定执行复核：{report.session_id}",
        "",
        f"审计状态：**{report.audit_status}**；子实验：`{report.experiment_id}`。",
        "",
        "## 来源与确认链",
        "",
        f"来源 Copilot 会话：`{report.source_session_id}`；来源 audit："
        f"`{report.source_audit_id}`。",
        f"请求 `{report.request_sha256}`；审阅 `{report.review_sha256}`；"
        f"谱系 `{report.lineage_sha256}`。",
        "",
        "## 执行与重新复核",
        "",
        f"批次状态 `{report.batch_status}`；实际调用 {report.calls_invoked} 次；"
        f"独立复核候选 {report.verified_candidates} 个。",
        f"重新生成的报告指纹：`{report.report_sha256}`。",
        "",
        "## 证据清单",
        "",
        f"文件 {len(report.files)} 个；规范清单指纹 `{report.manifest_sha256}`。",
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
