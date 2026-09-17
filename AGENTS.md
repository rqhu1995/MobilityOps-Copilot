# MobilityOps-Copilot 协作说明

## 项目目标

本仓库负责交通运输优化工作流中的 orchestration、输入验证、solver adapter、确定性结果验证与后续 Agent workflow。长期数据流为：自然语言需求 → `ScenarioSpec` → 能力检查 → solver 选择与执行 → 标准化 `SolutionResult` → 解释与情景分析。

## 当前状态与工作范围

阶段 3（HGS 集成）、阶段 4A（Gurobi 集成前审计）、阶段 4B（Gurobi 集成）、阶段 5（显式能力选择与情景分析）、阶段 6（受控重复实验与可读报告）、阶段 7（自然语言需求到显式执行请求）、阶段 8（HGS/Gurobi 终端交互）、阶段 9（自然语言实验计划）及阶段 10（证据绑定的结果解释与下一轮实验建议）均已完成。最新完整开发验收为 2026-09-17：603 项测试通过；单场景两套 backend 和 HGS 自然语言重复实验的终端完整执行链路均已有真实验收证据。阶段 6 的 9 次 HGS 重复实验与 3 个历史 Gurobi 终态重放、阶段 5 的 3 次真实求解和 12 个历史重放继续作为历史基线。

当前已实现 `SolverService.assess()`、`select()`、`preflight()`、`solve_selected()`、`ScenarioAnalysisService.observe()` / `compare()`，以及 `ExperimentService.precheck()` / `execute()` 和 `ExperimentReportService.analyze()` / `write_report()`。阶段 6 调用约定、统计分母和验收见 [重复实验文档](docs/experiments.md)。接口及验收范围见 [阶段 5 文档](docs/scenario-analysis.md)，Gurobi 模型差异和接入约定见 [集成审计](docs/gurobi-integration-audit.md)及 [adapter 文档](docs/gurobi-adapter.md)。本地产物在被忽略的 `runs/` 下，不作为普通测试的必需依赖。

2026-09-16 按用户要求启动阶段 9：自然语言需求 → 可追溯的有限 `ExperimentPlan` 草稿 → 缺失、冲突、未支持要求与伪造引用澄清 → 全场景能力和运行预检查 → 绑定完整计划、预算、标识、运行配置及预检查依据的明确确认 → 复用现有串行执行与确定性报告。计划修改、预检查变化或 backend 运行配置变化均使旧请求和旧确认失效；自然语言中的“执行”不启动 solver。终端新增显式实验计划模式，原有单场景流程继续保持兼容。

本轮阶段 9 仅授权离线开发与 fake 端到端验收。可以使用 fake HTTP、fake executable 和 fake Python 子进程覆盖 3 个 HGS 场景 × 每场景 3 次、内部 5 秒、外部 10 秒、最多 9 次、总等待预算 90 秒的流程，但不得调用真实 Gemini、真实 HGS 或 Gurobi，不得探测许可证，不得新增外部依赖或修改 solver core。不得把阶段 6 历史运行或普通测试解释为新的真实验收；离线完成后只生成一份具体、可审阅的真实验收计划，等待新的明确授权。

阶段 9 离线开发验收已完成：592 项普通测试通过，本次新增 21 项；fake HTTP 与可快速返回的 fake HGS 完成 3 个场景 × 3 次的终端闭环，9 个候选均通过独立复核，保存报告已只读重放一致。缺失/冲突/引用/能力/预检查/旧确认/超时/部分失败/中断分支均由离线测试覆盖。离线开发验收阶段真实 Gemini、HGS、Gurobi 调用和许可证探测均为 0；两个 solver 仓库保持不变。

2026-09-16 用户随后明确确认 [阶段 9 真实验收计划](docs/natural-language-experiments.md#真实验收计划与完成记录)。会话 `stage9-real-acceptance-20260916-v1` 调用 Gemini `gemini-3.8-flash` 1 次，预留 0.21888 HKD、usage 估算 0.119862 HKD；草稿准确重建 baseline、capacity30、two_trucks 三个 HGS 场景。全计划预检查通过后串行执行 9 次 HGS，内部/外部时限 5/10 秒、等待预算 90 秒、无重试或补跑；9 次均 `succeeded`，9 个候选全部重新复核通过。批次实际耗时约 12.374 秒，三个 case 各 `n=3`；容量 30 和两车方案相对基准的 objective 中位数观测差均为 0.002，不满意度差为 0，排放差为 0.03884。这些仅是本批描述性观测。保存报告只读重放完全一致，296 个证据文件的聚合散列未变化，两个 solver 仓库保持不变；本次 Gurobi 调用与许可证探测均为 0，不授权自动重跑或额外批次。

2026-09-17 用户要求启动阶段 10：每次从原始实验及运行证据重新复核后，将结果分成已验证事实、本批描述性观察和不能得出的结论；终端提供 `/explain`、`/compare <variant> baseline` 与 `/next-plan [variant]`。下一轮建议只生成绑定当前重放报告及最近比较或显式指定变体的有限 `ExperimentPlan` 候选，`auto_execute=false`，仍需新的全计划预检查、预算审阅和明确确认；没有选择变体时拒绝生成。跨 backend、无候选、多模型/输入签名或损坏证据不生成统一差值或候选计划。本阶段完成 11 项新增离线测试，完整 603 项测试通过；analysis 模式从仓库根目录默认读取 `./runs`，不要求 solver 仓库环境变量。真实 Gemini、HGS/Gurobi 调用和许可证探测均为 0，两个 solver 仓库未修改。范围与使用见 [阶段 10 文档](docs/evidence-based-explanations.md)。

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

阶段 7 已允许上述受限的真实 LLM REST 接入，阶段 8 已允许上述终端 CLI，阶段 9 允许上述自然语言实验计划服务和终端计划模式的离线实现。当前仍不引入 LLM/Agent SDK、RAG、多 Agent、HTTP API、Web UI、数据库、消息队列、云部署或新框架，不实现并行调度、自动恢复、自动重试、自动补跑、自动续费、自动调参或跨模型统一评分。这是工作范围限制，不是项目的永久架构结论；后续用户明确要求相应阶段时，先同步本文件与具体任务范围，再开展实现。阶段计划本身不构成真实 LLM 调用、许可证探测或真实 solver 批次的授权。

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
