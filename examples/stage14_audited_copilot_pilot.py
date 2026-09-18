"""One bounded real-Gemini Copilot shadow session; solver execution is blocked."""

import os
from pathlib import Path

from mobilityops.config import Settings
from mobilityops.domain import ScenarioSpec
from mobilityops.services.copilot_pilot import CopilotShadowPilotService
from mobilityops.services.gemini_intake import GeminiBudgetConfig
from mobilityops.services.gurobi_inputs import strict_json


PILOT_ID = "stage14-audited-shadow-20260918-v1"
SESSION_ID = "stage14-shadow-20260918-v1"
AUDIT_ID = "stage14-shadow-audit-20260918-v1"
OBJECTIVE = (
    "以 baseline 为基准，比较 capacity30（容量30）；每个场景运行1次，使用HGS，"
    "内部5秒、外部10秒，最大2次 solver 调用，总等待预算20秒，失败继续。"
)
EVIDENCE_QUESTION = (
    "根据当前证据回答：计划多少次 solver 调用、等待预算多少秒？"
    "这些数字能证明 capacity30 更优吗？"
)


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY 未导出；未创建预算或发起请求。")
        return 2
    project = Path(__file__).resolve().parents[1]
    settings = Settings(
        hgs_repo_path=Path("/home/runqiu/BRPWR-HGSADC-SBC"),
        gurobi_repo_path=Path("/home/runqiu/BRPWR-Gurobi"),
        runs_dir=project / "runs",
    )
    scenario = ScenarioSpec.model_validate(
        strict_json((project / "examples/scenario_6_1.json").read_bytes())
    )
    report = CopilotShadowPilotService(settings).run(
        pilot_id=PILOT_ID,
        session_id=SESSION_ID,
        audit_id=AUDIT_ID,
        budget=GeminiBudgetConfig(budget_hkd=1, max_calls=4),
        context=scenario,
        objective=OBJECTIVE,
        evidence_question=EVIDENCE_QUESTION,
    )
    print(
        f"影子验收：{report.audit_status}；Gemini "
        f"{report.calls_succeeded}/{report.max_calls}；solver {report.solver_calls}。\n"
        f"报告：{settings.runs_dir / 'copilot-pilots' / PILOT_ID / 'report.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
