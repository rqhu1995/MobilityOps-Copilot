# MobilityOps-Copilot 协作说明

## 项目目标

本仓库负责交通运输优化工作流中的 orchestration、输入验证、solver adapter、确定性结果验证与后续 Agent workflow。长期数据流为：自然语言需求 → `ScenarioSpec` → 能力检查 → solver 选择与执行 → 标准化 `SolutionResult` → 解释与情景分析。

## 当前状态与工作范围

阶段 3（HGS 集成）、阶段 4A（Gurobi 集成前审计）、阶段 4B（Gurobi 集成）、阶段 5（显式能力选择与情景分析）、阶段 6（受控重复实验与可读报告）、阶段 7（自然语言需求到显式执行请求）、阶段 8（HGS/Gurobi 终端交互）、阶段 9（自然语言实验计划）、阶段 10（证据绑定的结果解释与下一轮实验建议）、阶段 11（下一轮实验候选的离线接管、预检查与明确执行请求）、阶段 12（Gemini Decision Copilot v1）、阶段 13（Copilot 会话离线审计）、阶段 14（可审计的真实 Copilot 影子会话）、阶段 15（审计绑定的执行接管）、阶段 16（审计绑定的真实 HGS 小批验收）、阶段 17（审计证据绑定的 Decision Dossier v1）、阶段 18（Dossier 绑定的有限重复实验接管）、阶段 19（Replicated Decision Gate）及阶段 20（Gemini Decision Copilot v2）均已完成离线开发。最新完整开发验收为 2026-09-21：677 项测试通过。阶段 16 已按新确认执行 baseline/capacity30 两次真实 HGS 并通过闭环审计；阶段 17 已生成 `collect_replicates` dossier，阶段 18 已用 fake HGS 验证 6 次重复实验闭环。阶段 19 已用 fake 证据验证 campaign audit 和 replicated dossier，阶段 20 已用 fake Gemini 验证两遍式简报与复核。真实阶段 18 的 6 次 HGS 尚未确认，因此真实阶段 19 Dossier 和阶段 20 Gemini 调用均不存在；阶段 19/20 开发没有新增真实 Gemini/solver 调用或许可证探测。阶段 6 的 9 次 HGS 重复实验与 3 个历史 Gurobi 终态重放、阶段 5 的 3 次真实求解和 12 个历史重放继续作为历史基线。

当前已实现 `SolverService.assess()`、`select()`、`preflight()`、`solve_selected()`、`ScenarioAnalysisService.observe()` / `compare()`，以及 `ExperimentService.precheck()` / `execute()` 和 `ExperimentReportService.analyze()` / `write_report()`。阶段 6 调用约定、统计分母和验收见 [重复实验文档](docs/experiments.md)。接口及验收范围见 [阶段 5 文档](docs/scenario-analysis.md)，Gurobi 模型差异和接入约定见 [集成审计](docs/gurobi-integration-audit.md)及 [adapter 文档](docs/gurobi-adapter.md)。本地产物在被忽略的 `runs/` 下，不作为普通测试的必需依赖。

2026-09-16 按用户要求启动阶段 9：自然语言需求 → 可追溯的有限 `ExperimentPlan` 草稿 → 缺失、冲突、未支持要求与伪造引用澄清 → 全场景能力和运行预检查 → 绑定完整计划、预算、标识、运行配置及预检查依据的明确确认 → 复用现有串行执行与确定性报告。计划修改、预检查变化或 backend 运行配置变化均使旧请求和旧确认失效；自然语言中的“执行”不启动 solver。终端新增显式实验计划模式，原有单场景流程继续保持兼容。

本轮阶段 9 仅授权离线开发与 fake 端到端验收。可以使用 fake HTTP、fake executable 和 fake Python 子进程覆盖 3 个 HGS 场景 × 每场景 3 次、内部 5 秒、外部 10 秒、最多 9 次、总等待预算 90 秒的流程，但不得调用真实 Gemini、真实 HGS 或 Gurobi，不得探测许可证，不得新增外部依赖或修改 solver core。不得把阶段 6 历史运行或普通测试解释为新的真实验收；离线完成后只生成一份具体、可审阅的真实验收计划，等待新的明确授权。

阶段 9 离线开发验收已完成：592 项普通测试通过，本次新增 21 项；fake HTTP 与可快速返回的 fake HGS 完成 3 个场景 × 3 次的终端闭环，9 个候选均通过独立复核，保存报告已只读重放一致。缺失/冲突/引用/能力/预检查/旧确认/超时/部分失败/中断分支均由离线测试覆盖。离线开发验收阶段真实 Gemini、HGS、Gurobi 调用和许可证探测均为 0；两个 solver 仓库保持不变。

2026-09-16 用户随后明确确认 [阶段 9 真实验收计划](docs/natural-language-experiments.md#真实验收计划与完成记录)。会话 `stage9-real-acceptance-20260916-v1` 调用 Gemini `gemini-3.8-flash` 1 次，预留 0.21888 HKD、usage 估算 0.119862 HKD；草稿准确重建 baseline、capacity30、two_trucks 三个 HGS 场景。全计划预检查通过后串行执行 9 次 HGS，内部/外部时限 5/10 秒、等待预算 90 秒、无重试或补跑；9 次均 `succeeded`，9 个候选全部重新复核通过。批次实际耗时约 12.374 秒，三个 case 各 `n=3`；容量 30 和两车方案相对基准的 objective 中位数观测差均为 0.002，不满意度差为 0，排放差为 0.03884。这些仅是本批描述性观测。保存报告只读重放完全一致，296 个证据文件的聚合散列未变化，两个 solver 仓库保持不变；本次 Gurobi 调用与许可证探测均为 0，不授权自动重跑或额外批次。

2026-09-17 用户要求启动阶段 10：每次从原始实验及运行证据重新复核后，将结果分成已验证事实、本批描述性观察和不能得出的结论；终端提供 `/explain`、`/compare <variant> baseline` 与 `/next-plan [variant]`。下一轮建议只生成绑定当前重放报告及最近比较或显式指定变体的有限 `ExperimentPlan` 候选，`auto_execute=false`，仍需新的全计划预检查、预算审阅和明确确认；没有选择变体时拒绝生成。跨 backend、无候选、多模型/输入签名或损坏证据不生成统一差值或候选计划。本阶段完成 11 项新增离线测试，完整 603 项测试通过；analysis 模式从仓库根目录默认读取 `./runs`，不要求 solver 仓库环境变量。真实 Gemini、HGS/Gurobi 调用和许可证探测均为 0，两个 solver 仓库未修改。范围与使用见 [阶段 10 文档](docs/evidence-based-explanations.md)。

2026-09-17 用户要求启动阶段 11：新增 `--mode next-experiment --proposal ...`，将阶段 10 的 `NextExperimentProposal` 接入现有安全执行链路。入口每次重新复核来源实验并核对 `source_report_sha256`，只接受无 blocker、`auto_execute=false`、含单一明确变体的完整候选；随后执行全场景能力/运行预检查，展示调用数、等待预算和 backend 配置，生成同时绑定候选文件、规范候选、来源证据、完整计划、预检查与运行配置的请求。候选文件、来源证据、计划、预检查或运行配置在确认前发生变化均使旧确认失效；文件或自然语言中的“执行”不会代替一次性终端确认。阶段 11 新增 13 项离线测试，完整 616 项测试通过；fake HGS 完成候选两个 case 各 3 次的确认后闭环并只读重放一致。开发验收期间真实 Gemini、HGS/Gurobi 调用和许可证探测均为 0，当前真实 6 次 HGS 候选未执行，两个 solver 仓库未修改。范围与使用见 [阶段 11 文档](docs/confirmed-next-experiments.md)。

2026-09-17 用户要求启动阶段 12：交付统一的 `Gemini Decision Copilot v1`，把自然语言目标、现有证据审阅、实验计划、预检查、明确确认、结果解释和下一轮建议编排在同一受控会话中。Gemini 只负责理解、有限工具选择和证据绑定的表述；所有计划、能力、预检查、执行请求、结果复核和确认仍由现有确定性服务负责。不得向模型提供任意 shell/Python 工具，不得让模型生成或接受执行确认。首轮真实模型固定为 `gemini-3.8-flash`，最多 6 次 interactions 调用、总预算上限 1.5 HKD，凭据仅从 `GEMINI_API_KEY` 读取；失败不自动重试或续费。先完成 fake Gemini/fake solver 离线闭环，再做最多 6 次、真实 solver 调用为 0 的真实 Gemini 协议与工具选择验收。本轮不得调用真实 HGS/Gurobi、不得探测许可证、不得修改 solver core；任何真实 solver 请求仍需后续针对完整计划的单独明确授权。

阶段 12 已完成：新增 `--mode copilot`、受限 `gemini_copilot_decision_v1` 动作协议、证据 ID/数字约束、统一计划/解释/下一轮/执行审阅会话，以及确认后父子实验谱系。新增 15 项测试，完整 631 项通过；fake Gemini/fake HGS 覆盖两场景计划、全计划预检查、精确确认、2 次执行、独立解释和阶段 11 候选接管，并在剩余模型额度不足 2 次时隐藏需两次调用的计划起草动作。真实验收固定调用 `gemini-3.8-flash` 6 次，预留 1.31328 HKD、usage 估算合计 0.085188 HKD；前 5 个案例直接通过，第 6 个正确回答最初因本地校验器把列表序号及 `evidence-001` 误判为无来源数字而被拒绝。修复仅忽略已验证证据 ID 和行首列表标记，保存的第 6 次响应随后离线重放通过，没有第 7 次调用或重试。真实 solver 调用和许可证探测均为 0，两个 solver 仓库保持不变。范围与使用见 [阶段 12 文档](docs/gemini-decision-copilot.md)。

2026-09-18 用户授权创建并完成下一阶段，阶段 13 新增 `--mode audit --copilot-session-id ... --audit-id ...`，对阶段 12 保存会话做非交互、只读的完整证据重放。它核对 session/state/event、Gemini 预算与逐次调用、受限动作、模型可见 evidence 与本地工具产物、执行请求与确认、父子实验谱系、原始 solver run、独立验证和保存报告，并输出逐文件 SHA-256 的 JSON/Markdown 清单。损坏、缺失、陈旧、符号链接或跨层不一致证据使审计失败；已有 audit ID 拒绝覆盖。新增 9 项测试，完整 640 项通过；fake Gemini/fake HGS 覆盖确认后闭环及所有篡改分支。本阶段真实 Gemini、HGS/Gurobi 调用和许可证探测均为 0，两个 solver 仓库和 solver core 未修改。范围与使用见 [阶段 13 文档](docs/copilot-audit.md)。

2026-09-18 用户授权阶段 14：运行一次可审计的真实 Copilot 影子会话，固定 `gemini-3.8-flash`、最多 4 次 interactions、预算上限 1 HKD，不调用真实 solver。固定路径为业务目标路由、实验字段提取、全计划 HGS 只读预检查、执行审阅、明确取消、证据回答及阶段 13 审计；专用 HGS adapter 的 `solve()` 硬阻断，不注册 Gurobi、不探测许可证、不重试或续费。离线开发新增 5 项测试，完整 645 项通过。真实会话 `stage14-shadow-20260918-v1` 已完成：4 次调用全部成功，预留 0.87552 HKD、usage 估算 0.187116 HKD；动作依次为 `draft_experiment`、`prepare_execution_review`、`answer`。计划包含 baseline/capacity30 各 1 次、等待预算 20 秒；执行确认固定取消，状态 `execution_completed=false`，未创建 solver run。审计 `stage14-shadow-audit-20260918-v1` 为 `verified_no_execution`，清单含 39 个文件；真实 solver 调用和许可证探测均为 0，两个 solver 仓库保持不变。范围和证据见 [阶段 14 文档](docs/audited-copilot-pilot.md)。

2026-09-18 用户要求创建并进入下一步，阶段 15 新增 `--mode audited-execution --copilot-session-id ... --audit-id ...`。入口重新运行阶段 13 审计、核对保存 audit、唯一执行审阅及来源草稿，再以当前 backend 配置重建全计划预检查和新请求；新请求绑定来源 audit 清单、审阅、请求、草稿以及当前计划、预检查和配置。终端只接受绑定当次审阅散列的一次性确认，任何来源或环境漂移都会拒绝；确认后的实验保存到来源会话/audit/审阅/请求和子报告的谱系。新增 5 项离线测试，完整 650 项通过；fake HGS 完成两个 case 共 2 次的确认后执行、独立验证及报告，取消、错确认、audit 篡改和 CLI 路由均有覆盖。本阶段真实 Gemini、HGS/Gurobi 调用和许可证探测均为 0，solver core 未修改；阶段 14 的真实两次 HGS 计划未获本次请求授权，仍未执行。范围与使用见 [阶段 15 文档](docs/audit-bound-execution.md)。

2026-09-18 用户要求进入阶段 16：新增 `--mode audited-execution-audit --audited-execution-session-id ... --audit-id ...`，将阶段 14 来源 audit、阶段 15 双层请求和确认、父子谱系、实验批次、原始 run、独立验证及保存报告做无交互确定性重放，并输出逐文件 SHA-256 清单。入口不需要 TTY、Gemini 凭据或 solver 仓库，不调用网络、solver、许可证环境或外部子进程；缺失、篡改、符号链接、状态矛盾或跨层散列不一致均拒绝。新增 4 项离线测试，完整 654 项通过；fake HGS 完成两个 case 共 2 次执行及闭环审计，谱系和原始 run 篡改均被检出。本轮真实 Gemini、HGS/Gurobi 调用和许可证探测为 0，solver core 未修改。真实验收候选固定为阶段 14 的 baseline/capacity30 各 1 次 HGS、内部/外部 5/10 秒、最多 2 次、总等待 20 秒；只读生产预检查已通过，当前请求指纹为 `4bb228109e65707d9608b984907b09f2a546f227735dfa60af5bb919820cf017`，但没有创建 session/run 或启动 solver。进入阶段 16 和该指纹均不构成执行授权，仍需新的明确确认。范围见 [阶段 16 文档](docs/audit-bound-real-pilot.md)。

2026-09-21 用户原样确认阶段 16 审阅短语 `执行审计计划 a51d346ef1f8`。确认前来源 audit、请求、计划、预检查和 backend 配置重新核对未变化；会话 `stage16-stage14-hgs-20260918-v1` 随后串行执行 baseline/capacity30 各 1 次 HGS，两次均 `succeeded`，两个候选均通过独立复核。批次状态 `completed`，调用 2/2，预留等待 20/20 秒，实际耗时约 3.16711 秒，无重试或补跑。baseline 的 objective/dissatisfaction/emission 为 104.042/51.7808/7.99806，capacity30 为 104.038/51.7808/7.93847；单 case `n=1`，差异只作描述。闭环审计 `stage16-stage14-hgs-audit-20260918-v1` 为 `verified_complete`，80 个文件进入清单，保存审计再次只读重放一致。本次新增 Gemini/Gurobi 调用和许可证探测为 0，两个 solver 仓库保持干净。证据见 [阶段 16 完成记录](docs/audit-bound-real-pilot.md#真实小批完成记录)。

2026-09-21 用户要求进入阶段 17：新增 `--mode dossier --execution-audit-id ... --dossier-id ... [--variant-case-id ...]`，从阶段 16 保存审计开始重新验证完整执行证据和实验报告，输出确定性事实、描述性观察、不能得出的结论、五个决策门及 `auto_execute=false` 的有限下一轮候选。零个或多个变体时必须显式选择，audit/报告漂移、未知变体或输出覆盖均拒绝。新增 5 项离线测试，完整 659 项通过；真实 dossier `stage17-stage16-decision-20260921-v1` 与再次只读重放一致，指纹 `65b5ed5235de27aaa476726392fd0bee52695c9a62ea0d8d4d968f91b249b179`，五门状态为 pass/pass/pass/caution/pass，建议 `collect_replicates`。候选为 baseline/capacity30 各 3 次 HGS、内部/外部 5/10 秒、最大 6 次、等待预算 60 秒；未做运行预检查、未授权且未执行。本阶段真实 Gemini、HGS/Gurobi 调用和许可证探测为 0，solver core 未修改。范围见 [阶段 17 文档](docs/audited-decision-dossier.md)。

2026-09-21 用户要求开始阶段 18：新增 `--mode dossier-campaign --source-dossier-id ...`，每次重新重放阶段 16 执行审计和阶段 17 Dossier，核对 Dossier 指纹、决策门及保存候选，再导入完整计划、完成全场景预检查并展示调用数、等待预算和 backend 配置。外层请求绑定 Dossier、audit 清单、阶段 11 子请求、预检查和配置；终端只接受绑定当次审阅散列的一次性确认。Dossier、audit、候选、预检查或配置漂移均使旧确认失效；确认后保存 Dossier→请求→实验→报告谱系。新增 7 项离线测试，完整 666 项通过；fake HGS 完成 baseline/capacity30 各 3 次，6 个候选均通过独立复核，取消、错误确认、来源及候选篡改、CLI 路由均有覆盖。本阶段真实 Gemini、HGS/Gurobi 调用和许可证探测为 0，solver core 未修改；阶段 17 的真实 6 次 HGS 候选仍未授权、未执行。范围见 [阶段 18 文档](docs/dossier-replication-campaign.md)。

阶段 18 随后对真实 dossier `stage17-stage16-decision-20260921-v1` 完成无写入预检查：baseline/capacity30 各 3 次、内部/外部 5/10 秒、调用 6/6、等待 60/60 秒且全部场景允许；backend 仅为 HGS，外层候选请求指纹为 `e408055eaae28ed4653691f1ae6993a32519b4ff19c0cb178d131f4bf26309b9`。检查前后 `runs/` 均为 1589 个文件，两个 solver 仓库干净，没有创建会话或运行。该指纹不构成终端确认；真实批次仍须新交互会话及其当次一次性确认短语。

2026-09-21 用户要求连续启动阶段 19 和 20，并在阶段 20 完成后停止。阶段 19 新增 `--mode campaign-decision --dossier-campaign-session-id ... --audit-id ... --dossier-id ...`：无交互重放阶段 18 session、父 Dossier、来源 audit、确认、双层请求、原始 run、独立报告和谱系，生成逐文件 campaign audit，再仅以本次 campaign 分组生成 replicated dossier。新增 5 项测试；fake HGS 的 baseline/capacity30 各 `n=3`，6 个候选全部复核通过，刷新后五门全为 pass，建议进入 `human_decision_review`。真实阶段 18 campaign 尚未确认，故没有真实阶段 19 audit 或 Dossier。范围见 [阶段 19 文档](docs/replicated-decision-gate.md)。

阶段 20 新增交互式 `--mode decision-copilot-v2 --replicated-dossier-id ...`。入口重放 replicated dossier，构造编号 lineage/gate/fact/observation/limitation evidence，固定 `gemini-3.8-flash`、最多 4 次、预算不超过 1 HKD，并等待绑定请求指纹的启用短语。正常工作流恰好两次调用：生成 `DecisionBrief` 和独立 `DecisionBriefReview`；未知/重复 evidence、无来源数字、越过确定性 gate 或复核矛盾均失败关闭，不重试。输出始终 `human_approval_required=true`、`auto_execute=false`，没有 solver 工具。新增 6 项测试；fake Gemini 两次协议通过，取消不创建预算。本阶段真实 Gemini、solver 和许可证调用均为 0。阶段 19–20 合计新增 11 项，完整 677 项通过。范围见 [阶段 20 文档](docs/gemini-decision-copilot-v2.md)。

继续允许维护和修复现有 adapter、输入验证、能力评估、选择、受控执行、证据复核和情景分析。保持简单的 Python application service；可按实际职责拆分模块，不以“最小”为由省略必要的错误处理、证据保存或验证。

阶段 6 已按用户要求完成：显式实验计划、有界串行执行、描述统计与 JSON/Markdown 可读报告，具体范围见 [推进路线](docs/development-roadmap.md)。阶段 6 的真实验收规模为 HGS `6_1` 基准（1 车、1 维修人员、容量 25）、容量 30、2 辆车三个场景各 3 次，共 9 次；每次内部上限 5 秒、外部 timeout 10 秒，外部等待预算合计 90 秒。这是已完成的验收，不授权新一批重复实验。

2026-09-16 按用户要求启动阶段 7：自然语言需求 → 候选 `ScenarioSpec` → 缺失字段澄清 → 能力评估 → 明确执行请求。实现可替换的解析入口、字段来源与澄清状态、现有确定性校验/能力接口衔接，以及绑定具体场景版本的执行请求。生成或补全候选不自动启动 solver；含歧义、缺失字段、未支持要求或不兼容 backend 时不得执行。用户已指定 Google Gemini `gemini-3.8-flash`，本轮外部 LLM 费用上限 10 HKD，凭据仅从 `GEMINI_API_KEY` 环境变量用于认证，不打印或写入产物。此前 generateContent 路径共预留 8 次、1.75104 HKD，失败证据和账本保持不变，旧预算转为只读。2026-09-16 用户提供同一模型的成功 interactions 调用，凭据与账号可用已确认；本轮切换到 `/v1beta/interactions`，已完成 3 次有界协议/参数对照：最简请求可用，加入本项目 response_format 后返回 400。最终采用提示中要求 JSON、Pydantic 本地严格验证的已验证请求格式；另设完整六案例验收最多 6 次。包含旧失败、3 次对照、六案例和用户手动验证 0.02 HKD 预留，累计最多预留 3.74096 HKD，低于既有 10 HKD 授权。每次最多 16000 输入 token、4096 输出 token（含 thinking），计数使用完整文本/schema 的 countTokens 投影加 1024 token 余量，实际 usage 超限立即停止；失败保留预留，不自动重试。真实验收前先完成相应离线检查；六案例使用原定场景，不启动真实 solver。本阶段不预先安装框架，不能以普通测试或历史求解验收替代远端验收。最终验收：498 项测试通过，六个真实 Gemini 案例全部通过，原始响应与执行请求已离线重放复核；本批 usage 估算 0.146778 HKD，真实 solver 调用 0 次。六案例验收完成时已到达“明确执行请求”边界；容量 30 请求随后获用户确认并完成真实执行，见下一段。不再启动额外 LLM 验收。接口与证据见 [阶段 7 文档](docs/natural-language-intake.md)。

2026-09-16 用户随后明确要求执行已展示的容量 30 请求：`stage7-gemini-interactions-final-20260916-reviewed`，HGS `6_1`、1 车、1 维修人员、内部上限 5 秒、外部 timeout 10 秒，仅 1 次、无重试。此授权仅执行保存的具体请求，沿用独立复核与报告；不新增 LLM 调用或 Gurobi 求解。 本次已完成：`succeeded / feasible=True`，1 个候选通过独立复核；objective 104.039、dissatisfaction 51.7808、emission 7.94776，求解器报告耗时 1.27922 秒。13 项指标比较通过，166 个受保护文件和两个 solver 仓库状态保持不变；证据见 [执行验收](runs/stage7-confirmed-execution-20260916/report.md)。此前六案例的 solver 调用 0 次仍是该阶段的历史事实。

2026-09-16 用户要求先做交互入口，阶段 8 允许标准库终端 CLI：自然语言输入、澄清、候选与预算审阅、明确确认、调用现有服务执行及展示报告。每个会话独立保存输入、草稿版本、请求和状态；修订使旧确认失效，退出或中断不自动恢复执行。LLM 预算在终端明确展示并由用户确认；每会话最多 6 次提取、最多 1 次 solver 执行，不自动重试或续费。首版执行入口注册 HGS，明确展示 Gurobi 未注册的原因，不静默切换 backend。本轮实现验收使用 fake HTTP 和 fake executable，不新增远端 LLM 或真实求解批次。

阶段 8 开发验收：534 项普通测试通过，其中终端新增 36 项；fake HTTP/fake executable 覆盖确认后执行与独立报告、缺失字段、撤回、旧确认失效、预算耗尽、失败和中断。实际 PTY 启动及取消通过，该开发验收阶段新增真实 LLM/solver 调用均为 0。使用与证据见 [终端文档](docs/terminal.md)。

2026-09-16 用户随后在 fish 中完成真实终端会话 `20260916T090049Z-c25da014`：确认本会话预算并调用 Gemini 1 次，再确认容量 30 的单次 HGS 请求，内部 5 秒、外部 10 秒，无重试。结果 `succeeded / feasible=True`，目标值 104.04，候选通过独立复核；LLM usage 估算 0.013878 HKD，预留 0.21888 HKD。原始响应、草稿、请求指纹及报告已只读重放复核，报告完全一致，55 个证据文件及两个 solver 仓库状态在复核前后保持不变。终端成功路径已完成真实验收；本次复核没有新增 LLM 或 solver 调用，不授权自动重放求解或额外批次。

2026-09-16 用户要求接入 Gurobi 终端交互，当前允许扩展阶段 8：通过显式 Python 路径注册现有 Gurobi adapter，展示时间段长度、线程数、能力与模型边界，沿用单次确认、受控执行、独立复核和报告。执行审阅须绑定所选 backend 的运行配置，配置变化使旧确认失效；不得自动将 HGS 目标标识改写为 Gurobi 目标标识。开发验收使用 fake HTTP/fake Python 子进程，不新增远端 LLM 或真实求解批次、不重新探测许可证。HGS 原有入口继续可用，既有真实产物保持只读。

Gurobi 终端扩展开发验收：571 项普通测试通过，本次新增 37 项；fake HTTP/fake Python 子进程覆盖确认后执行、独立复核报告、成功/两种超时/不可行/失败、无许可证探测的拒绝与取消，以及运行配置变化使旧确认失效。本机既有 Python、已审计源码及 `6_1` 输入只读预检查通过，内部/外部时限 5/10 秒匹配；55 个既有终端证据文件及两个 solver 仓库状态保持不变。本轮真实 LLM/solver 调用均为 0，许可证仍未探测。详情见 [终端扩展验收](docs/terminal.md#gurobi-终端扩展验收)。

2026-09-16 用户随后完成真实 Gurobi 终端会话 `20260916T092552Z-12512c5a`：Gemini 提取 1 次，确认容量 30、600 秒时间段、1 线程、内部 5 秒／外部 10 秒的请求后执行 Gurobi 1 次，无重试。结果 `timeout_with_candidate / feasible=True`，8657 项独立检查通过；目标值 104.163310039134，求解器报告 best bound 103.794436993005、gap 约 0.354130%，没有证明最优。子进程正常退出，未触发外部终止。原始响应、草稿、完整请求、含配置的审阅指纹和实际 options 已核对，独立重算报告与保存报告一致；本次 47 个及历史 HGS 55 个证据文件和两个 solver 仓库状态在只读复核前后保持不变。两套 backend 的终端完整执行链路已有真实验收；本次复核未新增 LLM/solver 调用，不授权自动重跑或额外批次。证据见 [Gurobi 真实终端验收](docs/terminal.md#用户完成的真实-gurobi-终端验收)。

## 选择与分析约定

不得静默改写目标标识、输入、时限或资源要求。能力兼容不代表 executable、数据或许可证已经可用；`assess()` / `select()` 保持无执行副作用，运行环境由执行路径检查。显式指定的 backend 不兼容时拒绝，省略时仅选择唯一兼容 backend；不得根据历史目标值自动切换模型。两个 solution policy 均保留用户提供的求解时限，`best_available` 不保证最优。

比较必须重新核对保存的场景、原始结果和输入快照，重新执行独立验证，不能信任旧的 `passed` 标记。历史分析保持只读，新报告使用独立且不覆盖已有内容的路径。区分同模型情景观测、搜索条件变化和跨模型不可比；不可比或没有通过复核的可行候选时不得生成指标差值。HGS 的打印舍入范围不是统计置信区间，单次或小批次结果差异不能直接解释为因果效应或最优性证明。

## Solver 执行与模型边界

继续允许维护 Gurobi adapter、受控 Python 子进程桥接、独立目标标识、能力检查、原始结果保存、状态映射、周期模型验证与 SolverService 注册；允许合成小实例及真实 `6_1` 的有限时限验收。只导入原有建模函数，不复制或改写 solver core。

允许调用已有 HGS executable、使用 `6_1` 进行受控的 5 秒 smoke test，并更新文档和测试。运行与分析产物必须隔离保存在被 Git 忽略的 `runs/` 下，不得写入 solver 仓库。导入外部 Python solver 时禁用 bytecode 写入，避免在 solver 仓库产生 `__pycache__`。能力评估、只读结果分析和纯文档修改不应触发真实求解或许可证探测。

允许通过现有 Python 环境调用 Gurobi，进行受控的许可证、可用性和小型求解验证；用户已明确授权这类调用，无需重复确认。检查应使用最小模型、有限求解时限和外部 timeout，工作目录及产物仍放在被忽略的 `runs/` 下。许可证由 Gurobi 自身按已有配置加载；不得在检查脚本中读取、提取或回显许可证文件、`.env` 或凭据值，启动许可证环境前关闭日志，仅输出不含敏感信息的检查结果。

已有 HGS executable 不可用时应先报告；如必须编译，仅允许不在 HGS 仓库留下构建产物的 out-of-source build。

不得仅因两个 solver 目标权重相同就将其视为同一数学模型。Gurobi 的时间离散、库存更新和不满意度表示已完成首次审计；若建模源码、参数映射或语义发生变化，应先检查审计与验证器是否仍适用，不得直接绕过源码散列检查。其 bound/gap 仅属于本次 Gurobi 模型，不能直接作为 HGS 解的质量证明。Gurobi 使用独立的 `gurobi_linear_default` 目标标识，保留时间索引原始变量。所有含可行候选的结果（包括有解超时）必须经过独立周期模型复核；无候选时不得读取 incumbent 属性。当前 adapter 拒绝非空 seed 和损坏车比例，Gurobi 本身具有某项能力不代表 adapter 已支持。

HGS 不支持 seed，指定 seed 必须在启动前拒绝；不提供数学 best bound 或 optimality gap，两者必须为 `None`。确定性指输入、映射、解析和验证行为，不保证 HGS 算法结果可复现。run ID 必须安全且不得覆盖已有运行，外部 timeout 为内部 runtime 加有限 grace period。

成功的 HGS 结果必须通过基于本次 run 输入快照的独立复核：校验输入散列、站点库存时序、逐路线行驶与服务时限，以及不满意度、排放和目标值。复核采用已审计的 HGS 到站时原子更新库存语义，不得将其描述为维修完成时刻或真实交通过程的保证；无法确定的同时到达顺序不得擅自假定可行。保存 `validation.json` 和明确诊断，复核失败不代表问题已被证明不可行。

Python 代码使用 `src` layout、类型标注、Pydantic v2 和 pytest。避免为了未来需求引入复杂框架。

## 暂不纳入的范围

阶段 7 已允许上述受限的真实 LLM REST 接入，阶段 8 已允许上述终端 CLI，阶段 9 允许上述自然语言实验计划服务和终端计划模式的离线实现，阶段 11 允许阶段 10 候选的离线接管和 fake 确认后闭环，阶段 12 允许受限 Gemini 决策会话，阶段 13 允许只读本地审计和证据清单，阶段 14 允许上述固定预算的真实 Gemini 影子会话但不允许 solver 执行，阶段 15 允许审计绑定接管的离线实现和 fake solver 闭环，阶段 16 允许执行后闭环审计的离线实现和 fake solver 验收，阶段 17 允许从闭环审计生成确定性 dossier 和非执行候选，阶段 18 允许 Dossier 绑定接管的离线实现和 fake solver 闭环，阶段 19 允许 campaign 闭环审计和 replicated dossier 的离线实现，阶段 20 允许两遍式 Gemini 决策协议的 fake 验收。当前仍不引入 LLM/Agent SDK、RAG、多 Agent、HTTP API、Web UI、数据库、消息队列、云部署或新框架，不实现并行调度、自动恢复、自动重试、自动补跑、自动续费、自动调参或跨模型统一评分。阶段 19/20 的真实 HGS 或 Gemini 仍需相应完整证据、预算审阅和当次明确确认；阶段名、计划、候选文件、dossier 或审计报告本身不构成调用授权。

## 三个仓库的关系

- 本仓库：`/home/runqiu/MobilityOps-Copilot`，只负责 orchestration、adapter 和 validation。
- HGS solver：`/home/runqiu/BRPWR-HGSADC-SBC`，独立 C++/CMake 仓库，通过 `HGS_REPO_PATH` 引用。
- Gurobi solver：`/home/runqiu/BRPWR-Gurobi`，独立 Python/Gurobi 仓库，通过 `GUROBI_REPO_PATH` 引用。

不得复制 solver 源码到本仓库。未经用户明确授权，不得修改 solver core；如果确实必须修改，应先停止并说明原因、最小修改范围及不修改的影响。

## 开发与测试

从仓库根目录运行：

```bash
.venv/bin/python -m pytest
.venv/bin/python -c "import mobilityops; print(mobilityops.__version__)"
git diff --check
```

代码变更运行相应测试及以上完整检查；纯文档修改检查事实、链接和差异即可，不必重复运行 solver 或完整 pytest。普通测试使用 fixture、mock 或 fake executable，不依赖本机许可证、外部 solver 的实时运行或被忽略的历史产物。新增未跟踪文件也要检查差异格式，不能只依赖 `git diff --check`。

真实验收应按本次变更需要设置有限实例、运行次数和总时限；重复实验需要另有明确的批次规模与预算，单次 smoke 授权不解释为无界批量授权。开始和结束时核对相关 solver 仓库状态；需要保留的输入、源码与旧证据使用散列核对。记录实际执行的检查，将历史验收与本次结果分开报告。

外部进程必须使用参数列表而不是 `shell=True`，并显式设置 timeout、working directory、stdout/stderr 捕获和错误状态。保留 solver 原始输出，再生成标准化结果。

## 安全与版本控制

- 不读取、打印或提交 WLS 密钥、Token、`.env` 内容或其他敏感信息。
- `.env` 只能保留在本地；仓库只提交无敏感值的 `.env.example`。
- 不覆盖已有修改，不执行破坏性 Git 命令，不自动 commit 或 push。
