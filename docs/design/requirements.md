# Requirements Document

## Introduction

本文档是 `multiphase-buck-power-agent` 的需求规格，由已完成的 `design.md` 反推、并回溯至两份上游资料撰写：

| 上游资料 | 角色 | 本文档的使用方式 |
| --- | --- | --- |
| `需求分析/项目需求_AI辅助仿真设计寻优.docx`（V1） | 原始需求文档（需求源头） | User Story 的「role / benefit」尽量回溯该文档的第 1 节目标、第 3 节主要任务、第 4 节交付物、第 5 节待确认事项 |
| `设计文档/AI芯片多相Buck供电模块_阶段1正式设计规格书_V2.0.md` | 唯一设计基线（实施基线） | 需求不得违反其任何决策；引用记为「V2.0 §x.y」 |
| `.kiro/specs/multiphase-buck-power-agent/design.md` | 工程化实现设计 | 需求与其模块、里程碑、正确性属性一一对应；引用记为「design.md §x」 |

原始需求文档已成功提取，未使用 V2.0 §1.2 的回退依据。

**系统定位**：面向多相 Buck 电源设计的单机 Engineering Agent。通过受控 Harness 连接 Simulink，形成「需求 → 候选生成 → 自动仿真 → 确定性评价 → 反馈搜索 → 工程推荐」的闭环，验证其能否减少工程师重复调参工作。阶段1 不建设产品平台，不承诺仿真结果等同实板结果。

**需求边界（由 V2.0 §1.4 需求裁剪与 §2.2 不做什么承载，Requirement 1 显式落成）**：设计变量固定为 `rcomp` / `ccomp` 二维；不做多目标 Pareto、不用可调权重；效率指标在二维阶段默认不激活；不做跨范式对照与统计推断；不建 Web/API、消息队列、Agent 编排框架、向量库与抽象层。design.md §0.2 的负向清单组件不出现在本文档的任何需求中。

**追溯约定**：每条需求末尾给出「追溯」行，标注对应的 design.md 章节、V2.0 章节，以及（若有）原始需求文档的对应条目。

---

## Glossary

术语控制在 V2.0 §16 的 8 条之内。EARS 句式中的系统名直接取 design.md §2.1 / §2.3 的模块与函数标识符（`cli`、`config`、`controller`、`run_task()`、`preflight()`、`sim`、`eval`、`agent.propose`、`agent.validate`、`retrieval`、`store`、`report`、`calib`、`reference`、`robustness`、`BudgetLedger`、`MatlabSession`、MATLAB `+pa` 包），不重复定义；`PowerAgent 系统` 指全体模块的合体，`PowerAgent 项目` 指需人工执行的过程性义务（探针、门禁审批、任务集冻结、总结文档）。

- **Probe（P-1 / P-2 / P-3）**：前置实测探针。把单次仿真耗时、稳定性裕量提取路径与成本、实测数据可得性三个物理量从待定值变为实测值。
- **Baseline Gate**：Baseline 可行性门禁。M1 出口要求 Engineering Baseline（项目组批准的当前工程参考设计）在冻结模型与冻结约束下于完整 Evaluation 场景集上判定为可行。
- **Averaged_Model**：条件建设的平均模型，用于线性分析或 Screening，须与开关模型通过一致性验证。
- **Optimize / Apply**：Optimize 期只做运行时参数注入不落盘；Apply 期在人工批准后才生成新模型版本文件。
- **`simulation_key` / `evaluation_key`**：两级缓存键。前者含模型包哈希、模型变体、执行环境、候选与场景，复用波形；后者叠加 `metrics_hash` 与 `constraints_hash`，复用指标与约束结果。预算计数只挂前者。
- **`simulation_only`**：验收轨道标记，`task.yaml` 的布尔字段。为真时（仿真寻优 PoC 轨）结论限定于冻结仿真参考模型；为假时（验证工程轨）方可输出实物相关结论。
- **Checkpoint 1 / 2 / 3**：三个且仅三个人工介入点——任务确认、异常与扩展、最终推荐。
- **Dense Grid**：二维批准档位的一次性 12×12 参考扫描。用于响应面可视化与「Agent 是否具备搜索价值」的直接答案，不作为搜索效率 Oracle。

---

## Requirements

### Requirement 1: 阶段1 能力范围与双轨验收边界

**User Story:** 作为需求方，我希望阶段1 的设计变量、目标口径与结论边界被显式限定，以便我清楚系统能证明什么、不得声称什么，避免把仿真域结论误读为实板结论。

#### Acceptance Criteria

1. THE PowerAgent 系统 SHALL 把设计变量限定为 `rcomp`（单位 ohm）与 `ccomp`（单位 F）两项，即候选 `parameters_si` 的键集合、`constraints.yaml` 的 `design_space.variables` 键集合与 `model.yaml` 的 `io_contract.injectable_params` 键集合三者均恰为 `{rcomp, ccomp}`；相位数、相电流均衡增益与开关频率 Fsw 在阶段1 不作为设计变量。
2. THE `eval.aggregate` SHALL 以「硬约束可行性 → 主目标 → 字典序次目标」为唯一候选比较口径：判定为不可行的候选与未跑完 Evaluation 集的候选不参与主目标排序；主目标取 `metrics.yaml` 的 `objective.primary.metric_id` 指定指标在 Evaluation 集上按 `objective.primary.aggregation` 聚合所得的值，并按 `objective.primary.direction` 比较；两个候选的主目标聚合值之差的绝对值不大于 `objective.tie_tolerance` 时判为并列，并按 `objective.secondary_lexicographic` 中已激活项依序比较，其中该序列的每一项为含 `metric_id` 与 `direction` 两个字段的结构（`phase_margin` 取 `maximize`、`efficiency` 取 `maximize`、`sensitivity` 取 `minimize`），比较时按该项自身声明的 `direction` 定序而不沿用 `objective.primary.direction`。
3. WHERE `metrics.yaml` 的 `active_metrics` 不含 `efficiency`，THE `eval` SHALL 跳过效率指标的计算，且 THE `eval.aggregate` SHALL 在字典序比较中跳过 `secondary_lexicographic` 中 `metric_id` 为 `efficiency` 的项。
4. WHERE `metrics.yaml` 的 `active_metrics` 不含 `sensitivity`，THE `eval.aggregate` SHALL 在字典序比较中跳过 `secondary_lexicographic` 中 `metric_id` 为 `sensitivity` 的项。
5. WHEN `run_task()` 开始执行时，THE `config` SHALL 从 `task.yaml` 的 `simulation_only` 布尔字段读取验收轨道，并在本任务首次仿真启动之前把该值写入 `tasks` 行的 `simulation_only` 列，该列在任务生命周期内不再改写，且为 `report` 与 `robustness` 判定验收轨道的唯一依据。
6. WHERE `simulation_only` 为 `true`，THE `report` SHALL 使报告第一节渲染的结论边界声明逐字等于 `templates/report.md.j2` 已定义的 PoC 轨结论表述集合中的一条、不由自由文本拼接产生，且 THE `templates/report.md.j2` SHALL 不含断言实物可行性的条目或渲染分支（含「可打板」「实物可行」「实物预测准确」「打板建议」四类表述）。
7. WHERE `simulation_only` 为 `true`，THE `robustness` SHALL 输出按 `eval.aggregate` 排序的前 3 名仿真域 Top 候选；可行候选少于 3 个时输出全部可行候选；不存在可行候选时输出「无可行候选」的可审计记录；每个输出候选标记为仿真域结果且不标记为工程推荐。
8. WHERE `simulation_only` 为 `true`，THE PowerAgent 项目 SHALL 在交付物6 项目总结文档中记录「使仿真结果尽可能逼近实际测试结果」这一需求首要目标在阶段1 为零进展。
9. THE PowerAgent 系统 SHALL 以单机 CLI 进程形态运行，不监听任何网络端口、不提供网络服务，其唯一出网连接为 LLM 推理 API 的出站调用，与 MATLAB 的交互限于本机 `MatlabSession` 会话。
10. THE PowerAgent 系统 SHALL 不实现 PCB Layout 寄生参数提取与反标注、自动拓扑搜索与自动器件选型，其代码中不存在产生这三项结果的路径，且不以等效寄生敏感性扫描结果替代 Layout 工具链与实板校准。
11. WHERE `simulation_only` 为 `true`，THE `report` SHALL 标注本任务未输出工程推荐，其原因为校准参数置信区间为空、Robustness 第三维未执行、结论限定于冻结仿真参考模型。
12. WHERE 工程师已显式批准等效寄生范围，WHILE 当前里程碑处于 M6 出口之后，WHEN 剩余 engine 启动额度不小于该扫描所需启动数时，THE PowerAgent 系统 SHALL 在该批准范围内执行寄生敏感性扫描，并把扫描结果标记为仿真域结果。

**追溯**：design.md Overview / §4.3 / §4.4 / §14 / §17.1 Q1、Q12 / §17.2 L9-2；V2.0 §1.3、§1.4、§2.1、§2.2、§6.3、§15；原始需求文档 §1 目标、§2 项目范围、§5 待确认事项第 2 项。

---

### Requirement 2: 前置探针与 M0 启动门禁

**User Story:** 作为技术负责人，我希望在写生产代码之前把仿真耗时、裕量提取成本与实测数据可得性测出来并作为启动门禁，以便预算、硬约束可信度与验收轨道基于实测值而非估计值决策。

#### Acceptance Criteria

1. THE PowerAgent 项目 SHALL 在 `probe_report.md` 中记录下列探针产出：P-1 不少于 3 次手动运行的逐次单次仿真墙钟值（单位 s，逐次列出）、P-1 `fast_restart` 与 `parsim` 相对 `serial` 的加速比（无量纲比值）、P-1 推荐的 `model.yaml` 的 `runtime.max_wallclock_per_run_s` 取值（单位 s，取 `ceil(3 × single_run_s)`，即超过探针实测单次墙钟 3 倍即视为卡死而非慢）、P-2 裕量提取方法标识、P-2 双方法交叉核对偏差（`phase_delta_deg` 单位 deg 与 `gain_delta_db` 单位 dB）、P-2 每候选额外仿真启动数（单位 次，为 `metrics.yaml` 的 `margin_extraction.extra_engine_starts_per_candidate` 的来源值）、P-2 判定的 `averaged_model_required` 布尔值、P-3 数据清单与数据缺口、P-3 预判的验收轨道（`task.yaml` 的 `simulation_only` 建议取值）。
2. THE `controller.preflight` SHALL 取 P-1 逐次墙钟值中的最大值作为 `ProbeRecord.single_run_s` 消费（保守取值，与 worst-case 口径一致），并按 `estimated_wallclock = single_run_s × task.yaml 的 budget.max_engine_starts` 计算墙钟估算值。
3. IF `estimated_wallclock` 大于 `task.yaml` 的 `budget.max_wallclock_hours × 3600`，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('budget_wallclock_infeasible')`。
4. IF 四个配置文件中任一由 schema 标记为必填的字段为空、为 `null`、或其值仍为模板中的 `<...>` 占位形式，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('config_incomplete: <字段路径>')`，且 THE `config` SHALL 不推断该字段的默认物理值。
5. IF `metrics.yaml` 的 `active_metrics` 不含 `phase_margin`，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('phase_margin_must_be_active')`。
6. IF `metrics.yaml` 的 `margin_extraction.cross_check_record` 为空，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('margin_cross_check_missing')`。
7. THE `controller.preflight` SHALL 对配置不齐备、冻结哈希不一致、裕量提取未就绪、预算墙钟不可行与 Baseline 不可行五类情形各抛出对应的 `PreflightError`，且对这五类情形不提供以告警、降级或跳过方式继续执行的分支。
8. THE PowerAgent 项目 SHALL 在 M0 出口以四个配置文件中对应字段的具体值落定验收轨道、模型包、硬约束、合法域与档位、指标定义、显式场景行、Engineering Baseline 与预算八项（`metrics.yaml` 的 `compare_tolerance` 除外，其补齐时点见 Requirement 3），其中模型包与安全边界另以 `freezes` 表的 `kind='model_package'` 与 `kind='safety'` 两行承载，并在 `probe_report.md` 中具名记录模型 Owner、数据 Owner、指标 Owner 与技术负责人。
9. IF `estimated_wallclock` 大于 `task.yaml` 的 `budget.max_wallclock_hours × 3600`，THEN THE PowerAgent 项目 SHALL 在「缩减档位或总预算」「启用经确定性验证的 `parsim`」「Screening 层采用平均模型」「Screening 层评估改用 SIMPLIS」四项中恰好冻结一项决策，并把该决策记录在 `probe_report.md` 中。
10. IF `probe_report.md` 的 P-1、P-2 与 P-3 记录项中任一项为空或仍为占位形式，THEN THE PowerAgent 项目 SHALL 判定 M0 门禁未通过并阻断进入 M1，且不以「M1 会测」为由放行。
11. IF `metrics.yaml` 的 `active_metrics` 含在 `eval.metrics` 中无对应具名计算函数的 `metric_id`，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('metric_not_implemented: <metric_id>')`，且不以静默跳过该指标的方式继续执行。
12. IF `constraints.yaml` 的 `hard_constraints` 中任一条约束的 `observable` 既不属于 `metrics.yaml` 的 `constraint_observables` 键集合、也不属于 `active_metrics`，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('observable_unbound: <约束名>')`，且不进入寻优循环。
13. IF 同一物理量在 `metrics.yaml` 与 `constraints.yaml` 中声明的单位字符串不逐字符相同，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('unit_mismatch: <约束名>')`，且不进入寻优循环。
14. IF `metrics.yaml` 的 `margin_extraction.cross_check_record.path` 所指文件的内容 sha256 与该节的 `cross_check_record.sha256` 不相等，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('margin_cross_check_mismatch')`，且不进入寻优循环。
15. IF Baseline Gate 自身按 `probe.single_run_s × Baseline Gate 所需 engine 启动数` 计算的墙钟估算值大于为该门禁独立审批的墙钟上限，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('baseline_gate_wallclock_infeasible')`，且不启动该门禁的任何仿真；该断言与 AC3 的 `estimated_wallclock` 断言相互独立，Baseline Gate 的墙钟不计入 `estimated_wallclock`。

**追溯**：design.md §4.2 / §6.2.2 / §8.2 / §13 / §16 T1 / §17.2 L5-1、L7-2、L8-1、L9-1、L10-5、L18-4；V2.0 §3.1、§3.3、§6.2、§12.2 验收 1；原始需求文档 §3 任务1、§5 待确认事项第 1、3 项。

---

### Requirement 3: 配置文件校验、规范化序列化与三个冻结点

**User Story:** 作为指标 Owner 与电源设计工程师，我希望四个配置文件被强类型校验、并以确定性方式哈希与冻结，以便任何口径变更都可被检测且不会静默影响历史结果的可比性。

#### Acceptance Criteria

1. THE `config.loader` SHALL 把 `task.yaml`、`model.yaml`、`metrics.yaml`、`constraints.yaml` 四个文件解析为 pydantic v2 强类型模型，并校验每个字段的 `unit` 标识与 schema 声明的单位字符串逐字符相同、其数值落在 schema 声明的闭区间内。
2. IF 任一配置文件存在结构不合规、字段类型不合规、`unit` 标识与 schema 声明不一致或数值越界四类情形之一，THEN THE `config.loader` SHALL 抛出校验错误、不返回配置模型、逐条列出全部不合规字段的完整路径（文件名加嵌套键序列）与不合规类别，且不为任何不合规或缺失字段填入默认值。
3. THE `config.hashing` SHALL 按 design.md §5.3.1 登记的 `canonical_json` 规则序列化后取 sha256、以不截断的小写十六进制表示，计算 `metrics_hash`（输入为 `metrics.yaml` 解析后的全部内容）、`constraints_hash`（输入为 `constraints.yaml` 解析后的全部内容）与 `scenario_set_hash`（输入为写入 `scenario_set` 表的全部冻结显式场景行）；`canonical_json`、各类哈希算法与浮点格式化规则以 design.md §5.3.1 为唯一登记处，本文档不另行定义。
4. WHEN 一个合法的 `parameters_si` 映射先经 `canonical_json` 序列化再被解析时，THE `config.hashing` SHALL 产出键集合与原映射相同、且每个浮点值按 design.md §5.3.1 的浮点格式化规则所得文本与原映射逐字符相同的映射（往返属性）。
5. WHEN 相同的配置内容在不同操作系统、不同进程、不同当前工作目录、不同配置文件路径、不同文件行尾形式与不同键书写顺序下被哈希时，THE `config.hashing` SHALL 产出相同的 `metrics_hash`、`constraints_hash` 与 `scenario_set_hash`，其中文本类输入的行尾规范化按 design.md §5.3.1 执行。
6. THE `store` SHALL 在 `freezes` 表中以 `kind='model_package'`、`kind='safety'`、`kind='task_set'` 三行承载三个冻结点，每个 `kind` 至多一行且每行记录 `frozen_at`，其中 `kind='safety'` 的 `hash` 为把 `constraints_hash`、`metrics_hash` 与 `scenario_set_hash` 三个具名值按 `canonical_json` 序列化后取 sha256 所得的值。
7. WHEN `controller.preflight` 执行时，THE `controller.preflight` SHALL 比对 `freezes` 表中 `kind='model_package'` 与 `kind='safety'` 两行的 `hash` 与当前配置计算值，且不改写这两行的 `hash` 与 `frozen_at`。
8. THE `config` SHALL 允许 `metrics_hash` 在 M1 出口前更新一次（用于补齐 `compare_tolerance`），该次更新已发生以 `freezes` 表 `kind='safety'` 行的 `detail` 列记录，M1 出口以 Requirement 10 的寻优前值基线 `frozen_result_hash` 是否已记录界定。
9. THE `constraints.yaml` SHALL 分为 `hard_constraints` 节与 `design_space` 节两节，且 `hard_constraints` 节顶部载明「本节变更需重新审批」。
10. WHERE 当前里程碑为 M5 或之后，THE `controller.preflight` SHALL 比对 `task_set.md` 的 sha256 与 `freezes` 表中 `kind='task_set'` 行的 `hash`。
11. IF `freezes` 表中 `kind='model_package'` 或 `kind='safety'` 的行缺失、或其 `hash` 与当前配置计算值不一致，THEN THE `controller.preflight` SHALL 抛出 `PreflightError`、在其消息中标识该 `kind`，且不执行后续 preflight 步骤。
12. IF `metrics_hash` 的更新请求发生在 M1 出口之后、或 `freezes` 表 `kind='safety'` 行的 `detail` 已记录过一次更新、或该次变更涉及 `compare_tolerance` 以外的字段，THEN THE `config` SHALL 拒绝该更新、保持该行的 `hash`、`detail` 与 `frozen_at` 三列不变，并抛出校验错误。
13. IF `task_set.md` 的 sha256 与 `freezes` 表中 `kind='task_set'` 行的 `hash` 不一致，THEN THE `controller.preflight` SHALL 输出告警、继续执行后续步骤、不抛出 `PreflightError`，且不改写该行。
14. IF `model.yaml` 的 `io_contract.divergence_guard.vout_abs_max` 小于 `constraints.yaml` 的 `hard_constraints.vout_max.value`、或 `io_contract.divergence_guard.iphase_abs_max` 小于 `hard_constraints.peak_current_max.value`，THEN THE `config` SHALL 抛出跨文件一致性校验错误、不返回配置模型，并在错误消息中给出两个被比较字段的完整路径；该断言的必要性在于安全界低于评价阈值会使合法候选被误判为发散。
15. THE `config.schema` SHALL 断言 `constraints.yaml` 的 `design_space.variables.<name>.ticks` 为等比档位序列，即显式列表形式下全部相邻档位比在 `1e-6` 相对容差内一致（不等比即报错、不做归一化修正），并断言 `design_space.tick_match_rel_tol` 严格小于 `0.01 × (tick_ratio − 1)`（`tick_ratio` 为该变量的相邻档位比）。
16. WHEN `config` 展开 `design_space.variables.<name>.ticks` 时，THE `config` SHALL 使每个档位值一律经 design.md §5.3.1 登记的档位字面量格式化规则（`format(v, ".12g")`）规范化后再供 `agent.prompt.build_prompt` 写入与 `agent.validate` 判定命中使用，使写入 prompt 的档位字面量与校验时的档位值为同一组十进制文本。

**追溯**：design.md §4 / §4.2 / §4.4 / §4.5 / §5.3.1 / §6.2.2 / §17.2 L5-2、L12-1、L12-3、L14-2；V2.0 §3.4、§12.1 可复现性定义；原始需求文档 §5 待确认事项第 2 项。

---

### Requirement 4: 模型包完整性与 Optimize 期只读

**User Story:** 作为模型 Owner，我希望模型包以依赖闭包为单位被哈希并在寻优期保持字节不变，以便「历史仿真结果 ↔ 模型版本」的对应关系不会被静默破坏。

#### Acceptance Criteria

1. THE `model.yaml` SHALL 为每个模型变体列出 `.slx` 入口、引用模型、数据字典、MATLAB 函数目录、初始化脚本、被 `.slx` 入口或初始化脚本读取的全部 MAT 输入与自定义库，构成依赖闭包清单，该清单不以主观重要性筛选条目。
2. THE `sim.hashing` SHALL 对依赖闭包全体文件内容与 `model.yaml` 的规范化内容计算 `model_package_hash`，并在 `averaged_model_required=true` 时覆盖 switching 与 averaged 的合并闭包。
3. WHEN 依赖闭包中任一文件内容、闭包成员集合（含 MATLAB 函数目录下文件的新增、删除与重命名）或 `model.yaml` 的规范化内容三者之一发生变更时，THE `sim.hashing` SHALL 产出与变更前不同的 `model_package_hash`。
4. WHILE 任务处于 Optimize 期，THE `sim` SHALL 以只读方式打开模型文件、通过 `SimulationInput` 或模型工作区注入运行时参数，并使依赖闭包内每个文件在 `sim.simulate()` 返回后的内容与该次调用前逐字节相同。
5. WHILE 任务处于 Optimize 期（自 `run_task()` 入口起至 `run_task()` 返回止，`cli.cmd_apply` 的执行不属于该期），THE `sim` SHALL 使磁盘上模型包的 `model_package_hash` 保持与 `run_task()` 入口时相同。
6. WHEN 每次调用 `sim.simulate()` 之前，THE `sim` SHALL 按与 `model_package_hash` 相同的闭包条目顺序计算依赖闭包的 `fast_fingerprint`（`(size, mtime_ns)` 序列的哈希），并与 `run_task()` 入口时对同一闭包计算并冻结的值比对。
7. IF `fast_fingerprint` 与冻结值不一致且全量 `model_package_hash` 重算后仍不一致，THEN THE `run_task()` SHALL 在不发起本次 `sim.simulate()` 的 Simulink 调用的前提下以 `stop_and_ask_human(cause='model_mutated_during_optimize')` 停止任务，且不为本次比对写入任何仿真结果行。
8. THE `sim` 包中除 `sim.apply_model` 之外的模块 SHALL 通过 `pa.*` 白名单函数访问模型，其静态代码中不存在对依赖闭包内任一文件执行写入、覆盖、另存、重命名或删除的调用（含 `save_system` 及等价调用）。
9. IF `fast_fingerprint` 与冻结值不一致而全量 `model_package_hash` 重算后与冻结值一致，THEN THE `run_task()` SHALL 继续执行本次 `sim.simulate()`，且不触发 `stop_and_ask_human`。
10. WHEN 依赖闭包内文件的时间戳发生变化而其内容与闭包成员集合均未变时，THE `sim.hashing` SHALL 产出与变化前相同的 `model_package_hash`。

**追溯**：design.md §4.2 / §6.3.3 / §10 CP-3 / §16 T4；V2.0 §3.2、§6.5；原始需求文档 §4 交付物2、§5 待确认事项第 1 项。

---

### Requirement 5: 确定性仿真执行与 MATLAB 接口封装

**User Story:** 作为软件工程师，我希望 MATLAB/Simulink 的全部细节封装在一个函数边界内并只暴露白名单调用，以便参数注入、仿真触发与结果解析可自动化，同时 Agent 无法执行任意 MATLAB 命令。

#### Acceptance Criteria

1. THE `sim` SHALL 是 PowerAgent 系统中唯一 import MATLAB Engine 的包，并以 `MatlabSession` 进程内单例承载 Engine 生命周期。
2. THE `MatlabSession.call` SHALL 只接受与 `ALLOWED_MATLAB_FUNCTIONS` 白名单（`pa.inspect_model`、`pa.simulate_once`、`pa.simulate_batch`、`pa.export_observables`、`pa.run_linear_analysis`）某一项逐字符完全相等（区分大小写、无前后空白）的函数名，不做前缀匹配或模糊匹配，且不接受附带参数或语句的字符串。
3. IF `MatlabSession.call` 收到白名单之外的函数名或任意 MATLAB 语句字符串，THEN THE `MatlabSession.call` SHALL 在向 MATLAB Engine 发出该次调用之前拒绝该调用并抛出异常，不发生 Simulink 启动、不增加 `engine_starts`，且同一 `MatlabSession` 实例对后续白名单调用保持可用。
4. THE MATLAB `+pa` 包 SHALL 把 Block Path、`SimulationInput`、`logsout`、求解器设置、模型工作区、线性化调用与 MAT 文件操作封装在 MATLAB 侧，并向 Python 侧只返回结果引用与标量字段（`status`、`elapsed_ms`、`engine_starts`），其返回值不含波形采样点本体。
5. THE MATLAB `+pa/private/apply_params.m` SHALL 是 PowerAgent 系统唯一的参数注入实现，并按 `model.yaml` 的 `io_contract.injectable_params` 白名单为每个键声明的 `block_path`、`param` 与 SI 单位注入该白名单内的键。
6. WHEN `sim.simulate()` 返回 `status='ok'` 时，THE `sim.simulate` SHALL 提供指向已 commit 产物的非空 `waveform_ref`，在非缓存路径上使 `engine_starts` 不小于 1，并在 `scenario.require_margin` 为真时提供非空 `observable_ref` 且使 `engine_starts` 不小于 `1 + metrics.yaml 的 margin_extraction.extra_engine_starts_per_candidate`。
7. THE MATLAB `+pa/private/map_error.m` SHALL 把求解器不收敛或步长触及下限映射为 `solver_error`、单次运行墙钟超过 `model.yaml` 的 `runtime.max_wallclock_per_run_s` 映射为 `timeout`、Vout 或 Iphase 出现 Inf/NaN 或其绝对值越过 `model.yaml` 的 `io_contract.divergence_guard.vout_abs_max` 与 `io_contract.divergence_guard.iphase_abs_max` 映射为 `diverged`、Engine 连接中断或许可证瞬时不可用映射为 `engine_transient`，每次失败只输出一个取自 `{ok, diverged, solver_error, timeout, engine_transient}` 的枚举值而非自由文本，且 `timeout` 与 `diverged` 两项的判据来源恰为上述两个 `model.yaml` 字段而不取自 `io_contract.solver.stop_time` 与 `constraints.yaml` 的 `hard_constraints`。
8. IF MATLAB 侧检测到 Block Path 或输出信号名不存在，THEN THE MATLAB `+pa` 包 SHALL 抛出 `ContractError`，且 THE `run_task()` SHALL 以 `stop_and_ask_human(cause='model_systemic_error')` 停止任务。
9. THE `sim.simulate_batch` SHALL 按 `model.yaml` 的 `runtime.execution_mode`（`serial` | `fast_restart` | `parsim`）统一决定映射方式，调用方不逐次指定，并为请求序列中每个请求返回恰好一条 `SimulationResult`，使返回序列长度等于请求序列长度且顺序与请求序列一致。
10. IF `apply_params.m` 收到 `model.yaml` 的 `io_contract.injectable_params` 之外的键，THEN THE MATLAB `+pa/private/apply_params.m` SHALL 不对任何键执行注入、不启动仿真，并抛出 `ContractError`。

**追溯**：design.md §4.2 / §6.3.1 / §6.3.2 / §6.4 / §11.3 / §16 T2、T5 / §17.2 L5-1、L5-2；V2.0 §10.2、§11.4；原始需求文档 §3 任务2、§4 交付物3。

---

### Requirement 6: 分层场景调度与 Screening 提前拒绝

**User Story:** 作为使用者，我希望场景以显式行给出并分 Screening / Evaluation / Robustness 三层执行，以便不可行候选在低成本层被提前拒绝，且角点覆盖由我批准而非由程序组合生成。

#### Acceptance Criteria

1. THE `controller.scenario` SHALL 只从 `task.yaml` 的 `scenarios` 显式行读取场景，每行包含在同一 `task_id` 内唯一的 `scenario_id`、取值属于 `{screening, evaluation, robustness}` 的 `tier`、取值属于 `{switching, averaged}` 的 `model_variant`、`vin_v`（V）、`temp_c`（degC）、`load_start_a`（A）、`load_end_a`（A）、`slew_a_per_us`（A/us）与布尔型 `require_margin`，且不从其他来源补充或推断任何场景行。
2. THE `controller.scenario` SHALL 使冻结的 `scenario_set` 行与 `task.yaml` 的 `scenarios` 行一一对应（行数相等、`scenario_id` 集合相等），且其静态代码中不存在对 `vin_v`、`temp_c`、`load_start_a`、`load_end_a` 与 `slew_a_per_us` 取值集合做组合展开的路径。
3. WHEN 任务启动时，THE `controller.scenario.freeze_scenario_set` SHALL 按 `task.yaml` 中的行序把全部场景行写入 `scenario_set` 表、返回 `scenario_set_hash`，并使 `tasks` 行的 `scenario_set_hash` 列等于该返回值。
4. THE `task.yaml` 中 `tier='evaluation'` 的场景行 SHALL 显式覆盖五类批准的电气角点，即 `vin_v` 最小、`vin_v` 最大、`abs(load_end_a - load_start_a)` 最大、`temp_c` 最小与 `temp_c` 最大，每类至少由一个显式行承担且同一行可承担多类，其比对来源为 M0 出口批准记录。
5. WHEN 一个候选进入仿真时，THE `run_task()` SHALL 先在 `tier='screening'` 场景行上执行，仅当该层每一行均以 `runs.status='done'` 结束且其 `constraint_results.feasible` 为 true 时才执行 `tier='evaluation'` 场景行，且在寻优循环内不执行任何 `tier='robustness'` 行。
6. IF Screening 层任一场景的 `ConstraintResult.feasible` 为 `false`，THEN THE `run_task()` SHALL 结束该候选的执行、不再执行该候选的 evaluation 与 robustness 行、以 `failure_class='candidate_rejected'` 记录触发拒绝的 `scenario_id` 与未通过的约束名，并保留该候选已写入的 `runs`、`metric_results` 与 `constraint_results` 行。
7. WHILE 候选处于 Evaluation 层执行中，IF 单个场景以 `runs.status='done'` 结束且其 `ConstraintResult.feasible` 为 `false`，THEN THE `run_task()` SHALL 记录该结果并继续执行该层其余场景行。
8. THE `robustness` SHALL 只对 `eval.aggregate.rank`（`top_n` 默认 3）输出的 Top 候选执行 `tier='robustness'` 场景行，不对其他候选执行该层任何场景行，且 Robustness 层不进入寻优循环。
9. IF Evaluation 层某场景的 `SimulationResult.status` 为 `diverged`、`solver_error` 或 `timeout`，THEN THE `run_task()` SHALL 以 `failure_class='candidate_rejected'` 结束该候选在该层的执行，并不再执行该层其余场景行。
10. IF 冻结的 `scenario_set` 不含 `tier='screening'` 行，THEN THE `run_task()` SHALL 把 Screening 层判为通过并直接执行 `tier='evaluation'` 场景行。
11. IF `task.yaml` 的 `scenarios` 不含 `tier='evaluation'` 行，THEN THE `controller.preflight` SHALL 抛出配置不齐备类的 `PreflightError` 并终止任务，且不进入寻优循环。
12. IF 某场景的裕量提取失败（`phase_margin` 的 `MetricResult.valid` 为 `false`）且该场景的 `tier` 为 `evaluation`，THEN THE `run_task()` SHALL 以 `failure_class='candidate_rejected'` 与 `cause='metric_invalid:phase_margin'` 关闭该 `runs` 行后继续执行该层其余场景行；IF 该场景的 `tier` 为 `screening`，THEN THE `run_task()` SHALL 结束该候选的执行并不再执行该候选的后续场景行。
13. WHEN 同一任务内连续第 3 次发生裕量提取失败时（连续计数在任一次裕量提取成功时复位，阈值为 `controller/stop.py` 的模块常量 `MARGIN_FAILURE_ESCALATION_THRESHOLD = 3`、不落配置项），THE `run_task()` SHALL 以 `stop_and_ask_human(cause='metric_pipeline_error')` 停止任务。

**追溯**：design.md §4.1 / §6.2.4 / §8.4 / §17.2 L8-2；V2.0 §6.3；原始需求文档 §3 任务3 约束条件。

---

### Requirement 7: 时域指标计算与无效处理

**User Story:** 作为指标 Owner，我希望五项时域指标的窗口、带宽、单位、无效条件与容差全部来自配置且无效时不填默认值，以便 `settling_time` 一类定义分歧不会以静默方式污染结论。

#### Acceptance Criteria

1. THE `eval.metrics` SHALL 为 `metrics.yaml` 的 `active_metrics` 中出现的每个时域指标（`output_ripple`（V）、`overshoot`（V）、`undershoot`（V）、`settling_time`（us）、`phase_peak_current`（A））计算其值，每项的 `signal`、计算窗口、误差带、滤波或带宽、聚合方式、单位、`invalid_if`、比较容差与阈值来源取自 `metrics.yaml` 的 `metrics.<metric_id>` 节，其中 `phase_peak_current` 按 `aggregation: max_over_phases` 在全部相之间聚合为单一值，`invalid_if` 为 AC9 所定固定枚举的子列表。
2. THE `eval.metrics` 的每个指标函数 SHALL 只从传入的 `MetricSpec` 读取 `signal`、`window`、`band`、`filter`、`aggregation` 与 `invalid_if` 参数，其函数体内不含硬编码的窗口、误差带或带宽字面量，不从模块级常量或环境变量读取窗口、误差带与带宽三类值，且对 `invalid_if` 只按枚举值查表调用 `INVALID_PREDICATES` 中的具名谓词函数、不对该字段做表达式解析或求值。
3. WHEN `eval.metrics.compute_metrics` 被调用时，THE `eval.metrics` SHALL 为 `active_metrics` 中的每个时域指标产出恰好一条以 `(run_id, metric_id)` 唯一标识的 `MetricResult`、为 `metrics.yaml` 的 `constraint_observables` 中的每个观测量产出恰好一条 `metric_id` 带 `obs.` 前缀的 `MetricResult`，且不为这两个集合之外的指标产出 `MetricResult`。
4. IF 指标的 `invalid_if` 中任一枚举值对应的谓词函数返回真，THEN THE `eval.metrics` SHALL 返回 `valid=False`、`value=None` 与等于该命中枚举值的非空 `invalid_reason`、不写入任何默认数值，且不阻断本次调用中其余激活指标与观测量的计算。
5. THE `eval` SHALL 只接受产物引用与已校验配置作为输入，其代码不引用 `.slx` 文件路径、`SimulationInput` 与 `logsout` 三项标识。
6. WHEN `eval.metrics.compute_metrics` 以相同的 `waveform_ref` 与相同的 `evaluation_key` 被调用两次时，THE `eval.metrics` SHALL 产出 `metric_id` 顺序相同、且逐条 `value`、`valid` 与 `invalid_reason` 相同的 `MetricResult` 序列（该比较不含 `run_id`）。
7. THE `eval` SHALL 不调用 LLM，也不发起仿真。
8. IF 波形产物不可读或其中缺少 `MetricSpec.signal` 指定的信号，THEN THE `eval.metrics` SHALL 为受影响指标返回 `valid=False`、`value=None` 与非空 `invalid_reason`、不填入默认数值，并继续计算其余不受影响的激活指标。
9. THE `metrics.yaml` 的 `invalid_if` SHALL 取值于恰好 8 项的固定枚举（`waveform_unreadable`、`signal_missing`、`window_empty`、`waveform_not_converged`、`vout_out_of_guard`、`no_step_detected`、`not_settled_within_window`、`extraction_failed`），每项对应 `eval/metrics.py` 中一个具名谓词函数并由 `INVALID_PREDICATES` 映射承载，且 THE `config.schema` SHALL 断言每个指标的 `invalid_if` 只含适用于该指标的谓词（`no_step_detected` 仅限 `settling_time` / `overshoot` / `undershoot`，`not_settled_within_window` 仅限 `settling_time`，`vout_out_of_guard` 仅限 Vout 类指标，`extraction_failed` 仅限 `phase_margin` / `gain_margin`），该字段不是表达式语法、`config.schema` 不接受枚举外的字符串。
10. THE `eval.metrics` SHALL 在同一次波形遍历中恒定计算 `metrics.yaml` 的 `constraint_observables` 中的每个观测量（`obs.vout_min` 与 `obs.vout_max`，各按其 `window` 与 `aggregation` 取值、不做滤波与阶跃识别），使该计算不受 `active_metrics` 开关影响，并把结果写入同一张 `metric_results` 表；硬约束判定依赖这些观测量，因此不提供把它们关闭的配置路径。
11. IF `metrics.yaml` 的 `active_metrics` 含在 `eval.metrics` 中无对应具名计算函数的 `metric_id`，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('metric_not_implemented: <metric_id>')`，且 THE `eval.metrics` SHALL 不提供静默跳过该指标的代码路径。

**追溯**：design.md §4.3 / §6.2.2 / §6.5.1 / §7.4 / §16 T8 / §17.2 L7-1、L7-2、L7-3、L9-1；V2.0 §6.1、§12.1 Metric Fixture 层；原始需求文档 §3 任务3 优化目标函数。

---

### Requirement 8: 稳定性裕量提取、交叉核对与成本计入

**User Story:** 作为电源设计工程师，我希望相位裕量作为硬约束依据具备独立交叉核对手段、且其额外仿真成本计入预算，以便硬约束判定可信而不是看起来可信。

#### Acceptance Criteria

1. THE `metrics.yaml` 的 `margin_extraction` 节 SHALL 记录取值属于 `{linear_analysis_on_averaged, freq_response_estimator_on_switching}` 的 `primary_method`、取值属于 `{manual_bode_reference, the_other_method}` 的 `cross_check_method`、`cross_check_tolerance.phase_deg`（单位 deg，正实数）、`cross_check_tolerance.gain_db`（单位 dB，正实数）、`extra_engine_starts_per_candidate`（单位 次，整数，来源为 P-2 探针实测，`config.schema` 约束 `0 <= v <= 4`）与 `cross_check_record` 六项，六项均为必填且不留占位形式；其中 `cross_check_record` 为含 `path` 与 `sha256` 两个字段的结构，`path` 固定指向 `artifacts/margin_cross_check.json`（该记录是模型与提取方法的开发期属性，因此不按 `task_id` 分目录、同一模型包下的全部任务共用一份）。
2. WHILE 任务处于 `run_task()` 执行期，THE `sim.run_linear_analysis` SHALL 对每个 `(候选, 场景)` 对的每次非缓存执行只发起一次由 `margin_extraction.primary_method` 指定的采集调用，且系统中不存在在该期调用 `cross_check_method` 或 `eval.margin.cross_check_margin` 的代码路径。
3. WHERE `primary_method` 为 `linear_analysis_on_averaged`，THE `sim.run_linear_analysis` SHALL 在平均模型上执行 `linearize` 与 `allmargin`。
4. WHEN 场景的 `require_margin` 为真时，THE `sim.simulate` SHALL 返回非空 `observable_ref` 与不小于 `1 + extra_engine_starts_per_candidate` 的 `SimulationResult.engine_starts`，且该采集产物随 `simulation_key` 缓存。
5. THE `eval.margin.extract_margin` SHALL 从已采集的频响引用计算 `phase_margin`（deg）与 `gain_margin`（dB）两条 `MetricResult`（每次调用恰好两条），使对应 `runs` 行的 `budget_units` 增量为 0、不增加 `engine_starts`，且其结果归属 `evaluation_key`。
6. IF 频响采集或裕量提取失败，THEN THE `eval.margin` SHALL 为 `phase_margin` 与 `gain_margin` 两条 `MetricResult` 均返回 `valid=False`、`value=None` 与 `invalid_reason='extraction_failed'`、不填入默认数值，且 THE `run_task()` SHALL 以 `failure_class='candidate_rejected'` 与 `cause='metric_invalid:phase_margin'` 关闭该 `runs` 行，随后按 Requirement 6 的 AC12 与 AC13 处理后续控制流（Evaluation 层跑完该层其余场景行、Screening 层提前拒绝、连续 3 次失败触发 `stop_and_ask_human(cause='metric_pipeline_error')`）。
7. THE PowerAgent 项目 SHALL 在首次 `run_task()` 执行之前执行一次双方法交叉核对，其记录含两个方法标识、两组 `phase_margin` 与 `gain_margin` 数值、`phase_delta_deg`、`gain_delta_db`、所用候选参数与 `scenario_id`，并持久化为 `margin_extraction.cross_check_record` 所引用的记录，运行期不重复执行该核对。
8. IF `|phase_delta_deg|` 大于 `cross_check_tolerance.phase_deg` 或 `|gain_delta_db|` 大于 `cross_check_tolerance.gain_db`（即 `CrossCheckVerdict.ok` 为 `false`），THEN THE PowerAgent 系统 SHALL 抛出 `stop_and_ask_human(cause='margin_extraction_unreliable')`，且不提供自行选择其中一个结果的代码路径。
9. WHERE `primary_method` 为 `freq_response_estimator_on_switching`，THE `sim.run_linear_analysis` SHALL 在开关模型上执行 `frestimate`。
10. IF `primary_method` 为 `linear_analysis_on_averaged` 而 `model.yaml` 的 `averaged_model_required` 不为 `true`，THEN THE `controller.preflight` SHALL 抛出 `PreflightError`、在其消息中标识 `margin_extraction.primary_method` 与 `averaged_model_required` 两个字段路径，且不进入寻优循环。
11. WHERE 场景的 `require_margin` 为真，THE `store.cache` SHALL 使 `margin_primary_method` 参与该 `(候选, 场景)` 的 `simulation_key` 计算；WHERE 场景的 `require_margin` 为假，THE `store.cache` SHALL 不使该字段参与计算（传入空值），使裕量提取方法变更只令 `require_margin` 为真的场景的波形缓存失效、不影响其余场景，且 `simulation_key` 中的 `model_variant` 仍记该场景行声明的变体、不因采集实际发生在另一变体上而改写。

**追溯**：design.md §4.3 / §5.3 / §6.5.2 / §8.5 / §10 CP-11 / §16 T9 / §17.1 Q3 / §17.2 L8-1、L8-2、L8-3、L15-4；V2.0 §6.2、§12.2 验收 2、§15；原始需求文档 §2 阶段一稳定性裕度反馈信号。

---

### Requirement 9: 硬约束判定、worst-case 聚合与排序

**User Story:** 作为电源设计工程师，我希望硬约束优先于性能目标、且任一 Evaluation 场景缺失即判候选不可行，以便不会出现「忽略缺失值后聚合」得出的乐观结论。

#### Acceptance Criteria

1. THE `constraints.yaml` 的 `hard_constraints` 节 SHALL 恰含 `vout_min`（V）、`vout_max`（V）、`peak_current_max`（A）、`phase_margin_min`（deg）四条约束且不含第五条，各条恰含 `value`、`observable`、取值属于 `{lower, upper}` 的 `sense` 与 `applies_to_tier` 四个字段且不含 `model_variant` 字段，其绑定关系为 `vout_min` → `obs.vout_min` / `lower`、`vout_max` → `obs.vout_max` / `upper`、`peak_current_max` → `phase_peak_current` / `upper`、`phase_margin_min` → `phase_margin` / `lower`，其中前三条的 `applies_to_tier` 为 `[screening, evaluation]`、`phase_margin_min` 的 `applies_to_tier` 为 `[evaluation]`。
2. THE `eval.constraints.judge` SHALL 只判定该 run 所属场景的 `tier` 出现在该约束 `applies_to_tier` 中的约束条目，对每个参与判定的条目按其 `observable` 从本次 `MetricResult` 序列取值、按其 `sense` 比较，其余条目不参与本次判定、不写入 `violations`，也不影响本次 `feasible`；THE `eval.constraints.judge` SHALL 不读取 `model_variant`，判定所使用的模型变体由该 run 所在场景行声明的 `model_variant` 与该 `observable` 的可得性共同决定。
3. THE `eval.constraints.judge` SHALL 只读 `MetricResult` 做判定，不重新计算物理量。
4. IF 任一参与本次判定的硬约束其支撑 `MetricResult` 的 `valid` 为 `false`，THEN THE `eval.constraints.judge` SHALL 返回 `feasible=false` 并在 `violations` 中给出对应条目，该条目的 `constraint`、`limit` 与 `unit` 取自 `constraints.yaml`、`actual` 为空值，且不以 0、边界值或任何替代数值填充 `actual`。
5. THE `eval.aggregate.worst_case` SHALL 由 SQL 判定候选是否跑完，仅当该候选同时满足 `runs.status='done'`、`constraint_results.feasible=1` 与主目标 `metric_results.valid=1` 的相异 `scenario_id` 计数等于该任务 `tier='evaluation'` 场景行总数时，该候选才出现在返回结果中。
6. WHEN 候选满足上述完备条件时，THE `eval.aggregate.worst_case` SHALL 取其 Evaluation 集内有效主目标值的最大值作为 `worst_case_settling_time`（单位随 `metrics.yaml` 的 `settling_time.unit`）。
7. THE `eval.aggregate` SHALL 不提供在 Python 侧补全缺失场景值后再聚合的代码路径。
8. WHEN 两个候选的主目标聚合值之差的绝对值不大于 `metrics.yaml` 的 `objective.tie_tolerance` 时，THE `eval.aggregate.sort_key` SHALL 判为并列，并按 `objective.secondary_lexicographic` 中 `metric_id` 出现在 `active_metrics` 内的项依次比较、每项按其自身声明的 `direction`（`maximize` 时取值大者优先、`minimize` 时取值小者优先）定序，跳过 `metric_id` 未出现在 `active_metrics` 内的项。
9. THE `eval.aggregate` SHALL 不提供以加权得分抵消硬约束的比较路径。
10. WHEN 参与本次判定的硬约束其支撑 `MetricResult` 的 `valid` 均为 `true` 时，THE `eval.constraints.judge` SHALL 对 `sense='lower'` 的条目在 `observable` 取值不小于其 `value` 时判为满足、对 `sense='upper'` 的条目在 `observable` 取值不大于其 `value` 时判为满足（两者均取等判为满足），并对未满足的条目返回 `feasible=false` 且在 `violations` 中写入 `actual` 为该 `observable` 取值的条目。
11. THE `eval.aggregate.sort_key` SHALL 按「主目标按 `objective.primary.direction` 定序（`minimize` 时升序）→ `secondary_lexicographic` 已激活项按各项自身的 `direction` 依序 → `candidate_id` 字典序」三段定序，使 `eval.aggregate.rank` 的输出与输入候选顺序无关。
12. WHEN 没有任何候选满足 Evaluation 集完备条件时，THE `eval.aggregate.worst_case` SHALL 返回空映射、不抛出异常，且不返回任何填充值。
13. THE `controller.preflight` SHALL 断言 `hard_constraints` 中每条约束的 `observable` 或属于 `metrics.yaml` 的 `constraint_observables` 键集合、或属于 `active_metrics`，并在不满足时按 Requirement 2 的 AC12 抛出 `PreflightError('observable_unbound: <约束名>')`，使 `judge()` 在进入寻优循环前即具备取数依据。

**追溯**：design.md §4.3 / §4.4 / §5.4 / §6.2.2 / §6.5.3 / §7.5 / §10 CP-6 / §16 T10 / §17.1 Q1、Q2 / §17.2 L9-1、L9-2、L24-3；V2.0 §2.3、§6.3、§11.2、§12.2 验收 3；原始需求文档 §3 任务3 约束条件、§5 待确认事项第 2 项。

---

### Requirement 10: Engineering Baseline 可行性门禁与前值基线冻结

**User Story:** 作为技术负责人，我希望在烧掉寻优预算之前先用 Baseline 参数跑完整 Evaluation 集，以便约束阈值、模型与指标三类缺陷在 M1 出口被定位而不是在 M3 之后。

#### Acceptance Criteria

1. THE `model.yaml` 的 `baseline` 节 SHALL 给出键集合与 `design_space.variables` 相同、各键单位取自 `design_space.variables.<name>.unit` 的 `parameters_si`，以及 `measured_evidence_ref`（实板验证记录引用或 `null`），其中 `measured_evidence_ref` 取 `null` 为合法取值且不构成 Requirement 2 的 `config_incomplete`。
2. WHERE `task_kind='optimize'`，THE `controller.preflight` SHALL 执行 Baseline Gate，即以 `baseline.parameters_si` 作为 `origin='baseline'` 候选、在 `task.yaml` 中全部 `tier='evaluation'` 显式场景行上执行，并按 Requirement 9 的判定口径判定可行性；该次执行以独立的 `task_kind='baseline_gate'` 的 `tasks` 行记录，其 `budget_max_starts` 取自为该门禁单独审批的额度，其产生的全部 `budget_units` 计入该 `baseline_gate` 任务、不计入任何 `optimize` 任务的 `budget_max_starts`。
3. IF Baseline 候选在 `task.yaml` 的 `tier='evaluation'` 场景行中存在任一行不满足 `runs.status='done'`、或存在任一行 `constraint_results.feasible=0`、或存在任一激活指标 `metric_results.valid=0`，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('baseline_infeasible: <diagnosis_ref>')`、在该 `baseline_gate` 的 `tasks` 行写入 `stop_reason='stop_and_ask_human'` 与 `cause='baseline_infeasible'`、写入 `baselines` 行（`passed=0` 且 `diagnosis_ref` 非空）、不写入寻优前值基线，且不以部分场景结果或告警方式放行；此时寻优任务的 `tasks` 行尚未创建，`baseline_infeasible` 不写入任何 `task_kind='optimize'` 的行。
4. WHERE `model.yaml` 的 `measured_evidence_ref` 非空，THE PowerAgent 项目 SHALL 使 `diagnosis_ref` 指向文件中的排查结论表述为指向配置或模型缺陷；WHERE `measured_evidence_ref` 为 `null`，THE PowerAgent 项目 SHALL 使该文件中的排查结论同时列出参考值本身需要复核的可能。
5. WHEN Baseline Gate 判定为可行时，THE `store.freeze_baseline` SHALL 把该 Baseline 候选在全部 `tier='evaluation'` 场景行上的激活指标结果、硬约束判定结果与 worst-case 聚合值冻结为交付物5 的寻优前值基线，写入 `baselines` 支撑表一行（`gate_key` 为主键，另含 `task_id`、`passed=1`、非空 `frozen_result_hash`、`diagnosis_ref` 与 `frozen_at`），且不覆盖同一 `gate_key` 下已存在的冻结记录、不新增 `freezes.kind` 取值。
6. THE `controller.preflight` SHALL 以 `gate_key = H(model_package_hash, constraints_hash, metrics_hash, scenario_set_hash)` 为键在 `baselines` 表中缓存与查询 Baseline Gate 结论，该键命中时不启动任何 Simulink 仿真，四者任一变更时该结论失效并须重跑，且不按场景部分沿用旧结论。
7. THE `report` SHALL 只从 `gate_key` 与该 `tasks` 行的 `model_package_hash`、`constraints_hash`、`metrics_hash` 与 `scenario_set_hash` 四者算出的键相同的那条 `baselines` 行取数渲染寻优前后对比表，不为该对比另行重跑仿真。
8. THE PowerAgent 项目 SHALL 按「`constraints.yaml` 阈值或单位量纲 → 模型结构或参数（含 M-1 迁移偏差与双模型不一致）→ `metrics.yaml` 窗口、带宽、无效条件或提取实现」三类固定顺序排查 Baseline Gate 不可行原因，并在 `diagnosis_ref` 指向的文件中为三类各写入一条「命中」或「已排除」的结论及其依据引用。
9. IF `baselines` 表中不存在 `gate_key` 与该 `tasks` 行四个哈希算出的键相同的行，THEN THE `report` SHALL 把寻优前后对比表标记为不可用并给出缺失原因、不启动仿真，且不取用其他 `gate_key` 下的冻结基线。
10. THE `config.schema` SHALL 只校验 `model.yaml` 的 `baseline.parameters_si` 每个取值落在 `design_space.variables.<name>.domain` 闭区间内，不要求其命中 `ticks` 展开所得的档位集合；THE Baseline 候选 SHALL 以 `origin='baseline'` 落库且不经 `agent.validate` 校验，该例外在 CP-2 的量化范围中显式限定。
11. WHERE `task_kind='optimize'`，THE `controller.preflight` SHALL 对 Baseline Gate 自身的墙钟按 Requirement 2 的 AC15 做一次独立断言，且 THE `controller.preflight` SHALL 使 `estimated_wallclock` 的计算式保持只估寻优任务（`probe.single_run_s × task.yaml 的 budget.max_engine_starts`）、不把 Baseline Gate 的启动数计入该式。

**追溯**：design.md §5.1 / §6.2.2 / §6.9 / §8.2 / §10 CP-2 / §13 / §16 T12 / §17.1 Q8 / §17.2 L10-1 ~ L10-5；V2.0 §6.4、§9.1、§12.2 验收 3；原始需求文档 §4 交付物5。

---

### Requirement 11: ProposalAgent 候选生成、确定性 prompt 与权限边界

**User Story:** 作为技术负责人，我希望 LLM 只在「提出候选与搜索假设」这一个位置进入关键路径且输入可复现，以便我能区分模型随机性与输入漂移，并确保安全判定始终由确定性程序执行。

#### Acceptance Criteria

1. WHEN 每轮寻优开始时，THE `agent.propose` SHALL 依据 `SearchState`（`legal_domain`、`hard_constraints`、`current_best`、`tested_candidates`、`failed_regions`、`remaining_budget`、`evidence`）输出数量落在闭区间 [3, 6] 内且未经 `agent.validate` 校验的原始候选取值、`search_hypothesis`、`target_bottleneck`、`expected_tradeoff`、`evidence_ids`、`stop_recommendation` 与 `llm_call_id`，其中 `stop_recommendation` 只作为停止判定的输入之一、不直接终止任务。
2. THE `agent.prompt.build_prompt` SHALL 按 `legal_domain`、`hard_constraints`、`current_best`、`tested_candidates`、`failed_regions`、`remaining_budget`、`evidence` 的固定 section 顺序拼装 payload（该顺序不由字典迭代顺序决定），使 payload 的键集合与 `SearchState` 的七个字段一一对应，并以 `canonical_json` 规则序列化后计算 `context_hash = sha256(canonical_json(payload))`。
3. WHEN `agent.prompt.build_prompt` 以相同的 `SearchState` 被调用两次（含在不同进程、不同操作系统中调用）时，THE `agent.prompt.build_prompt` SHALL 产出逐字节相同的 `prompt_text` 与相同的 `context_hash`，其中 `prompt_text` 在 Jinja2 渲染后按 design.md §5.3.1 登记的换行规范化规则统一为 `\n`，使 `prompt_hash` 跨平台一致。
4. THE `agent.prompt.compress_waveform` SHALL 把波形压缩为超调（单位取 `metrics.yaml` 的 `overshoot.unit`）、下冲（`undershoot.unit`）、恢复时间（`settling_time.unit`）、纹波幅值（`output_ripple.unit`）、振荡频次（无量纲非负整数）与发散标志（布尔）六个特征，对相同输入产出相同的六元组，且使 `prompt_text` 只包含这些特征、不包含波形采样点。
5. THE `agent.prompt.build_prompt` SHALL 产出不含绝对路径与时间戳、且行尾已按 design.md §5.3.1 统一为 `\n` 的 `prompt_text`。
6. THE `agent.propose` SHALL 以 `agent/schema.py` 定义的输出 schema 经 function calling 或 JSON mode 约束输出结构，该 schema 约束候选数量落在闭区间 [3, 6] 内并要求 `search_hypothesis`、`target_bottleneck`、`expected_tradeoff`、`evidence_ids` 与 `stop_recommendation` 五项必填，不以提示词祈使替代 schema 约束。
7. IF LLM 输出解析或 schema 校验失败，THEN THE `agent.propose` SHALL 以原 `prompt_text` 加本次校验失败项清单为输入回灌重试（不引入 `SearchState` 之外的新输入），重试次数上限为 `task.yaml` 的 `budget.max_llm_repair_rounds`（整数，默认 2，`config.schema` 约束 `0 <= v <= 5`，取 0 时不重试），且使修复轮不新增 `runs` 行、不使 `BudgetLedger.used()` 增加。
8. IF 达到 `max_llm_repair_rounds` 后仍校验失败，THEN THE `agent.propose` SHALL 返回空候选集，且 THE `run_task()` SHALL 把该轮计入轮次计数与连续无改善轮数、按 `controller.stop.should_stop` 判定是否停止、在停止时把 `cause` 记为 `agent_returned_no_candidate`，并使该轮不新增 `runs` 行。
9. THE `SearchState.failed_regions` SHALL 以结构化形式表示，每项含参数边界、`failure_type`、样本数与 `run_id` 列表，其中 `failure_type` 的取值域直接复用 `runs.cause` 的 `Literal` 枚举取值、不新增枚举。
10. THE `agent` 包的导入闭包 SHALL 不含 `sim`、`store.db` 的写接口与 `config.loader` 的写接口；THE `agent.propose` SHALL 不提供运行仿真、修改硬约束或合法域、选择场景、修改预算、判定可行性、触发 Apply 或签发审批的代码路径。
11. THE `agent.tools` SHALL 只暴露 `search_knowledge`、`query_constraints`、`query_experiments` 三个只读工具。
12. WHEN 每次 LLM 调用完成时，THE `store` SHALL 写入一条 `llm_calls` 行，含 `role='proposal'`（阶段1 唯一 LLM 角色）、`model_id`、`prompt_hash = sha256(prompt_text)`、取自 `agent.prompt.build_prompt` 返回值的 `context_hash`、`tokens`、取值属于 `{ok, schema_invalid, repaired, empty}` 的 `outcome` 六字段，把 prompt 与输出原文落到 `artifacts/<task_id>/llm/`，并使每个修复轮的调用逐次成行；THE `store` SHALL 使 `tokens` 保持单列、其口径固定为 prompt 与 completion token 数之和，且不提供把 prompt 与 completion 分列存储的表结构。
13. IF `agent.prompt.build_prompt` 拼装出的文本含波形采样序列、绝对路径或随时刻变化的时间戳，THEN THE `agent.prompt.build_prompt` SHALL 抛出错误且不返回 `prompt_text`，并且 THE `agent.propose` SHALL 不发起本轮 LLM 调用、不写入 `llm_calls` 行。
14. IF 模型输出请求 `agent.tools` 白名单之外的工具名，THEN THE `agent.propose` SHALL 不执行该请求、不产生任何写副作用，并按 schema 校验失败处理（`outcome='schema_invalid'`，计入 `max_llm_repair_rounds`）。
15. IF LLM 调用发生网络类失败或超过 `task.yaml` 的 `llm.timeout_s`（默认 120 s），THEN THE `agent.propose` SHALL 以指数退避重试至 `task.yaml` 的 `llm.max_network_retries`（默认 2）、不把该类失败计入 `budget.max_llm_repair_rounds`、不使 `BudgetLedger.used()` 增加；IF 网络重试次数耗尽仍失败，THEN THE `store` SHALL 写入一条 `outcome='empty'` 的 `llm_calls` 行，且 THE `run_task()` SHALL 按 AC8 的空候选集分支处理本轮，`outcome` 的四值枚举不因该情形扩张。

**追溯**：design.md §4.1 / §5.1 / §5.3.1 / §6.6 / §7.7 / §8.9 / §10 CP-8、CP-14 / §16 T17 / §17.1 Q10、Q11 / §17.2 L11-1 ~ L11-5；V2.0 §8.1、§8.2、§8.3、§9.4、§12.2 验收 6；原始需求文档 §3 任务3、§4 交付物4。

---

### Requirement 12: 确定性候选校验器 `validate()`

**User Story:** 作为电源设计工程师，我希望 LLM 输出的每个候选在进入仿真前必过一道确定性校验，以便越界或重复取值绝不消耗仿真预算。

#### Acceptance Criteria

1. WHEN `agent.propose` 返回候选时，THE `run_task()` SHALL 以本轮原始候选序列、来自已冻结 `constraints.yaml` 的 `design_space`、`SearchState.tested_candidates` 与 `mode` 四个入参先调用 `agent.validate`，并仅把 `accepted` 集合中的候选送入 `sim.simulate`。
2. FOR ALL 进入 `sim.simulate` 的候选，THE PowerAgent 系统 SHALL 按 `origin` 分两支且两支均不存在无校验路径：`origin` 属于 `{agent, dense_grid}` 的候选，其 `candidate_id` 属于同一任务内某次 `agent.validate` 返回的 `accepted` 集合的 `candidate_id` 集合（`dense_grid` 走 `mode='grid'`，仍过键集合、单位量纲、合法域与档位四项检查）、且不存在绕过 `agent.validate` 直达 `sim.simulate` 的代码路径；`origin` 属于 `{baseline, manual}` 的候选，其取值落在 `design_space.variables.<name>.domain` 闭区间内并已过 `config.schema` 的域内校验、且 `origin` 如实记录（不要求命中档位，见 Requirement 10 的 AC10）。
3. IF 候选的键集合与 `design_space.variables` 的键集合不相等，THEN THE `agent.validate` SHALL 拒绝该候选并记录原因 `key_mismatch`。
4. IF 候选任一取值无单位、量纲不能归一到 SI、或不是有限实数（非数值类型、NaN 或 ±inf），THEN THE `agent.validate` SHALL 拒绝该候选并记录原因 `unit_or_dimension`。
5. IF 候选取值落在 `design_space.variables.<name>.domain` 闭区间之外，THEN THE `agent.validate` SHALL 拒绝该候选并记录原因 `out_of_domain:<name>`。
6. IF 候选取值未命中 `design_space.variables.<name>.ticks` 展开所得的档位集合（`ticks` 为显式列表时即该列表；为 `{count, spacing: log}` 时展开为以 `domain` 两端为端点、按对数等比排列的 `count` 个档位；命中判定为存在某个档位 `tick` 使 `|v − tick| <= design_space.tick_match_rel_tol × |tick|`，`tick_match_rel_tol` 默认 `1e-6`），THEN THE `agent.validate` SHALL 拒绝该候选、记录原因 `off_tick:<name>`、不静默吸附到最近档位，并在 `rejected` 中回传与输入逐键相同的原始取值；THE `agent.validate` SHALL 使参与该判定的档位值一律为经 design.md §5.3.1 的档位字面量格式化规则规范化后的取值，与写入 prompt 的档位字面量为同一组十进制文本，容差只作兜底。
7. IF 候选的 `candidate_id` 已存在于 `tested` 或本轮 `accepted` 集合，THEN THE `agent.validate` SHALL 拒绝该候选并记录原因 `duplicate`。
8. THE `agent.validate.tick_distance` SHALL 按 `max_i |log10(a_i) − log10(b_i)| / log10(tick_ratio_i)` 计算候选间距离，其中 `tick_ratio_i` 为该变量档位序列的相邻档位比，该比值因 `ticks` 由 Requirement 3 的 AC15 强制等比而唯一有定义，计算结果为非负实数且相邻档位间的距离为 1.0。
9. WHERE `mode='explore'`（`mode` 的默认取值），IF 候选到 `tested` 与本轮 `accepted` 并集的最小 `tick_distance` 严格小于 `design_space.novelty_min_ticks`，THEN THE `agent.validate` SHALL 拒绝该候选并记录原因 `low_novelty:<距离>`，其中 `<距离>` 按 design.md §5.3.1 登记的距离数值格式（`format(d, ".4f")`）格式化；该并集为空时不因新颖度拒绝该候选。
10. WHERE `mode='local_refine'`，IF 候选已通过键集合、单位量纲、合法域、档位与重复五项检查且其到 `tested` 与本轮 `accepted` 并集的最小 `tick_distance` 严格小于 `novelty_min_ticks`，THEN THE `agent.validate` SHALL 接受该候选，并把近邻标注写入 `ValidationOutcome.notes`（`Mapping[str, str]`，键为该候选的 `candidate_id`、值为含该最小 `tick_distance` 数值的标注文本，该数值按 design.md §5.3.1 的距离数值格式格式化）；WHERE `mode` 为 `explore` 或 `grid`，THE `agent.validate` SHALL 使 `notes` 为空映射。
11. WHEN `agent.validate` 返回时，THE `agent.validate` SHALL 满足 `len(accepted) + len(rejected) == len(raw)`、两集合不相交、两集合内元素保持 `raw` 中的相对顺序，且以相同的 `raw`、`design_space`、`tested` 与 `mode` 两次调用返回逐元素相同且原因字符串相同的结果。
12. WHEN 一个候选进入 `accepted` 时，THE `agent.validate` SHALL 使其 `candidate_id` 等于对归一到 SI 且命中档位后的取值映射按 `canonical_json` 序列化后取 sha256 所得小写十六进制摘要的前 16 个字符。
13. THE `agent.validate` SHALL 不调用 LLM、不发起仿真、不写数据库、不修改 `design_space`。
14. THE `agent.validate` SHALL 按「键集合 → 单位量纲 → 合法域 → 档位 → 重复 → 新颖度」的固定顺序检查每个候选，在首个失败处停止该候选的后续检查，并在同一检查下多个变量同时失败时取 `design_space.variables` 声明顺序中的第一个失败变量名填入原因字符串的 `<name>`，使每个被拒候选只对应一个原因字符串。
15. THE `agent.validate` SHALL 以 `rejected` 中的（原始取值, 原因）二元组为拒绝原因的唯一输出通道，不向数据库、文件或 `design_space` 写入该原因。
16. WHEN `agent.validate` 返回非空 `rejected` 时，THE `run_task()` SHALL 为其中每个二元组调用 `store.record_rejection` 写入一条 `rejections` 支撑表行（含 `rejection_id`、`task_id`、`round_index`、`llm_call_id`、为原始取值规范化 JSON 的 `raw_parameters`、取值同 `candidate_rejected` 校验类 `cause` 的 `reason` 与 `created_at`）、不为其调用 `sim.simulate`，且使其对 `runs.budget_units` 的增量为 0。
17. THE `agent.validate` 的 `mode` SHALL 取值于 `{explore, local_refine, grid}`；WHERE `mode='grid'`，THE `agent.validate` SHALL 只执行键集合、单位量纲、合法域与档位四项检查、跳过重复与新颖度两项检查，该模式供 `reference.dense_grid_scan` 使用，因此 `design_space.novelty_min_ticks` 的取值不构成对网格点集的约束。
18. WHEN `run_task()` 为本轮调用 `agent.validate` 时，THE `controller` SHALL 按确定性规则决定 `mode`：连续无改善轮数为 0 时取 `explore`；连续无改善轮数不小于 1 且 `SearchState.current_best` 存在时取 `local_refine`；连续无改善轮数不小于 1 而 `current_best` 不存在时取 `explore`；`reference.dense_grid_scan` 固定传入 `grid`。THE `agent.propose` SHALL 不提供影响 `mode` 取值的输出字段或代码路径。

**追溯**：design.md §5.1 / §5.3.1 / §6.7 / §6.9 / §7.2 / §8.1 / §8.3 / §10 CP-2、CP-8 / §16 T18 / §17.2 L12-1 ~ L12-6、L23-1；V2.0 §8.2、§12.2 验收 6；原始需求文档 §3 任务3。

---

### Requirement 13: 工程知识检索与先验隔离

**User Story:** 作为电源设计工程师，我希望检索只影响「先试哪里」而不改变合法域，且关键器件参数只从结构化配置读取，以便 LLM 摘要无法把错误的器件数值带进候选。

#### Acceptance Criteria

1. WHEN `retrieval.ingest` 被调用时，THE `retrieval.ingest` SHALL 只摄取调用方在 `paths` 参数中显式给出的文件、不自动发现语料，使 `kind` 取值属于 `{datasheet, app_note, experience_card, history_report}`（`history_report` 为保留取值，历史项目报告是 V2.0 §7 明确列出的经验来源之一），为每个被摄取文件写入一条 `evidence` 行（`evidence_id`、等于该 `kind` 的 `source`、为文件路径加页号或节标识的 `locator`、按 design.md §5.3.1 计算为 `sha256(normalized_text.utf-8)` 的 `text_hash`、`ingested_at`），并返回全部新写入行的 `evidence_ids`。
2. IF `paths` 中任一路径命中任务集隔离黑名单（该黑名单以 `retrieval/ingest.py` 的代码内常量 `TASK_SET_BLACKLIST` 承载，不落配置项——可配置的安全规则意味着一次配置失误即可让答案泄入检索库），THEN THE `retrieval.ingest` SHALL 拒绝整次调用、不写入任何 `evidence` 行、不写入任何 FTS5 索引项、对该次调用中未命中黑名单的路径一并不摄取，并返回指示命中任务集隔离的错误。
3. WHEN `run_task()` 组装 `SearchState` 时，THE `retrieval.retrieve` SHALL 返回三元组 `(suggested_start_points, evidence_notes, evidence_ids)` 作为 `SearchState.evidence` 传入，使三个序列各自的条目数不超过 `top_k`（以 `retrieval.retrieve` 的函数签名默认值 5 承载，不落配置项、不进任何哈希），并使以相同 `SearchState` 与相同检索库内容的两次调用返回条目与顺序完全相同的三元组。
4. THE `retrieval` 包的导入闭包 SHALL 不含 `sim` 与 `config.loader` 的写接口，其数据库写入只作用于 `evidence` 表与其 FTS5 索引表，不写 `candidates` 表、`runs` 表与四个 YAML 配置文件。
5. THE PowerAgent 系统 SHALL 只从 `constraints.yaml` 的 `design_space.device_limits` 读取关键器件参数（`cout_c` 单位 F、`cout_esr` 单位 ohm、`l_per_phase` 单位 H，三者的 `source` 均非空），且不从 LLM 输出、`evidence_notes`、`suggested_start_points` 与检索片段四类来源写入这三项参数。
6. WHEN `retrieval.retrieve` 返回非空 `suggested_start_points` 时，THE `run_task()` SHALL 先以这些取值调用 `agent.validate`、只把 `accepted` 集合中的候选送入 `sim.simulate`，不提供绕过 `agent.validate` 的路径，并使被拒起点对 `runs.budget_units` 的增量为 0。
7. THE `retrieval` SHALL 不修改 `SearchState.legal_domain` 与 `SearchState.hard_constraints`；对落在 `design_space.variables.<name>.domain` 与其 `ticks` 展开档位集合内的任一取值，THE `agent.validate` SHALL 在「`SearchState.evidence` 取自 `retrieval.retrieve`」与「`SearchState.evidence` 三个序列均为空」两种输入下给出相同的接受或拒绝结果与相同的原因字符串。
8. THE `retrieval` SHALL 以 SQLite FTS5 全文检索实现，不引入向量数据库或独立知识平台。
9. IF 检索库无 `evidence` 行或本次查询无命中，THEN THE `retrieval.retrieve` SHALL 返回 `suggested_start_points`、`evidence_notes` 与 `evidence_ids` 三者均为空序列的三元组，且 THE `run_task()` SHALL 以该空 `SearchState.evidence` 继续本轮 `agent.propose` 调用、不抛出异常、不中止任务；连续空检索不构成 `stop_and_ask_human` 的触发条件，`cause` 取值 `retrieval_insufficient` 已从 `runs.cause` 的 `Literal` 枚举中删除，系统不存在任何触发它的条件。

**追溯**：design.md §5.3.1 / §6.8 / §11.1 / §14 / §15 / §16 T19 / §17.2 L13-1 ~ L13-5；V2.0 §7、§2.2；原始需求文档 §1 目标（RAG）。

---

### Requirement 14: SQLite 唯一事实源、幂等写入与产物原子落盘

**User Story:** 作为软件工程师，我希望全部事实写入 SQLite 且产物先校验后原子移动，以便每个候选都能追溯到模型、配置、场景、波形、指标与审批。

#### Acceptance Criteria

1. WHEN `store` 打开数据库连接后，THE `store` SHALL 使 `PRAGMA journal_mode` 的回读值为 `wal`、`PRAGMA foreign_keys` 的回读值为 `1`，使 `tasks`、`scenario_set`、`candidates`、`runs`、`metric_results`、`constraint_results`、`approvals`、`interventions`、`llm_calls`、`evidence`、`freezes`、`baselines`、`rejections` 共 13 张表均存在（8 个主要实体 + 5 张支撑表 `scenario_set` / `evidence` / `freezes` / `baselines` / `rejections`），并以该数据库为全部事实的唯一写入目标。
2. THE `store` SHALL 只在 `runs` 表设置 `status` 列，其取值域为 `{running, waiting, done, failed}`。
3. THE `runs` 表 SHALL 以 `(task_id, candidate_id, scenario_id, attempt)` 为唯一键记录粒度。
4. THE `store.tx` SHALL 以 `BEGIN IMMEDIATE` 开启事务，并在上下文正常退出时提交该事务。
5. WHEN 写入运行结果时，THE `store` SHALL 按「`ArtifactStore.stage` 写临时文件 → 读回该临时文件计算 sha256 并与写入内容的 sha256 比对 → 原子移动到最终路径 → 在 `store.tx` 事务内提交数据库引用」的顺序执行，并在提交后使最终路径的文件存在、对应临时文件不再存在；THE `ArtifactStore.stage` SHALL 把临时文件写在最终路径的同一目录下并命名为 `.<name>.tmp`，从构造上保证原子移动的源与目标同卷，因此系统不设「临时目录与产物目录同卷」的部署前提、不新增声明临时目录的配置项、不做同卷启动期断言。
6. WHEN 已 commit 的产物引用被读回时，THE `store` SHALL 使读回内容按 sha256 计算所得的哈希等于该产物在 `ArtifactStore.commit` 时校验通过的哈希值（往返属性），产物内容哈希算法固定为 sha256、不随实现或平台变化。
7. IF 对同一 `run_id` 尝试写入不同结果，THEN THE `store.close_run_ok` 与 `store.close_run_failed` SHALL 以 `UPDATE ... WHERE status='running'` 的影响行数为 0 判定该写入被拒绝、回滚所在事务、以异常报告该拒绝，并使该 `runs` 行的 `status`、`failure_class`、`cause`、`waveform_ref`、`observable_ref` 与 `ended_at` 六列保持首次终结时的取值不变。
8. THE `store` SHALL 把四个配置文件快照与各自哈希、波形、指标 JSON、图、报告、审批记录与 LLM 原文分别落到 `artifacts/<task_id>/` 下的 `config`、`waveforms`、`metrics`、`plots`、`reports`、`approvals`、`llm` 子目录。
9. THE `store` SHALL 只按调用方传入的值原样写入并读回 `constraint_results.feasible`、`metric_results.valid`、`runs.failure_class`、`runs.cause` 与 `tasks.stop_reason` 五项、不自行计算或改写这五项，不读取 `metrics.yaml` 与 `constraints.yaml` 中的任何阈值，且不提供排序候选、判定可行性、判定停止或签发审批的代码路径。
10. IF `store.tx` 上下文内抛出异常，THEN THE `store.tx` SHALL 回滚该事务、使全部表的行内容与进入该上下文之前逐行相同，并把该异常原样向调用方传播、不吞异常也不改写异常类型。
11. IF `ArtifactStore.commit` 的 sha256 比对不通过或该临时文件长度为 0，THEN THE `ArtifactStore.commit` SHALL 不执行原子移动、不返回引用字符串、删除该临时文件并以异常报告校验失败，且使最终路径与数据库既有行不发生任何变化。
12. IF `store.open_run` 以已存在的 `(task_id, candidate_id, scenario_id, attempt)` 四元组插入 `runs` 行，THEN THE `store.open_run` SHALL 拒绝该插入、回滚所在事务，并使已有的该 `runs` 行内容不变。
13. THE `store` SHALL 使 `tasks` 表含 `cause` 列（承载 `stop_reason` 的伴随 `cause`，重启后可回溯）与 `calibration_hash` 列（`TEXT NOT NULL DEFAULT ''`，PoC 轨为空串、工程轨 M2 之后非空），并使 `approvals` 表含 `extra_units` 列（仅 `kind='budget_increase'` 时非空，承载追加的 engine 启动额度）；该三列之外本轮不新增表列。

**追溯**：design.md §2.2 / §5.1 / §5.3.1 / §6.9 / §16 T6 / §17.1 Q10 / §17.2 L14-1、L14-2、L15-1、L16-1、L17-1；V2.0 §11.1、§11.2、§12.2 验收 8；原始需求文档 §3 任务2 解析仿真结果。

---

### Requirement 15: 两级缓存与预算计量

**User Story:** 作为使用者，我希望预算以「真实启动 Simulink 的次数」计量、且指标口径变更不使波形缓存失效，以便在排查指标定义期间不被迫重跑数千次仿真。

#### Acceptance Criteria

1. THE `store.cache` SHALL 以 `sha256(canonical_json({model_package_hash, model_variant, execution_env_hash, candidate_normalized, scenario_id, scenario_spec_version, margin_primary_method}))` 计算 `simulation_key`，其中 `candidate_normalized` 为经 `agent.validate` 归一到 SI 后的 `parameters_si`（键集合等于 `design_space.variables` 的键集合、各值落在 `domain` 闭区间内且命中 `ticks`）、`margin_primary_method` 为条件性字段（仅 `scenario.require_margin` 为真时取 `metrics.yaml` 的 `margin_extraction.primary_method`，否则取空值），`canonical_json` 取 design.md §5.3.1 规则，且参与该哈希的键集合封闭为上述七项、不含其他字段。
2. THE `store.cache` SHALL 以 `sha256(canonical_json({simulation_key, metrics_hash, constraints_hash}))` 计算 `evaluation_key`。
3. THE `store.cache` SHALL 使 `metrics_hash` 与 `constraints_hash` 不参与 `simulation_key` 的计算。
4. WHERE `model_package_hash`、`execution_env_hash` 与 `scenario_set_hash` 均不变、候选集合不变且已有波形与观测量产物仍可读，WHEN 仅 `metrics.yaml` 或 `constraints.yaml` 发生变更时，THE PowerAgent 系统 SHALL 使全部 `(候选, 场景)` 对变更前后的 `simulation_key` 逐字节相同、`evaluation_key` 不同，并使该次重算对 `SUM(runs.budget_units)` 的增量为 0、不发生新的 Simulink 启动。
5. WHEN `evaluation_key` 命中缓存时，THE `run_task()` SHALL 为该 `(候选, 场景)` 写入一条 `runs` 行（`status='done'`、`attempt=1`、`budget_units=0`、`cache_hit=1`、`waveform_ref` 与 `observable_ref` 指向被复用的产物、`evaluation_key` 列填入该键），直接引用该 `evaluation_key` 对应的 `metric_results` 与 `constraint_results` 行、不重算指标也不重新判定约束，并使该处理对 `SUM(runs.budget_units)` 的增量为 0、不发生 Simulink 启动；该 `runs` 行为必写项而非可选的追溯性优化，理由是 Requirement 9 的 AC5（CP-6 的完备性判定）从 `runs` 取行，缓存命中不开行会使该场景在相异 `scenario_id` 计数中缺失，从而把一个实际已跑完的候选判为「未跑完」而排除。
6. WHEN `evaluation_key` 未命中而 `simulation_key` 命中缓存且被复用产物可读且其 sha256 往返校验通过时，THE `run_task()` SHALL 复用已缓存的波形引用重算指标、在 `require_margin` 为真时复用观测量引用重算裕量数值，并使该 `runs` 行的 `budget_units` 为 0、`cache_hit` 为 1；IF `simulation_key` 命中而被复用产物缺失或其往返校验失败，THEN THE `run_task()` SHALL 把本次视为未命中、按 AC7 重新仿真并计入完整预算，并把原 `runs` 行的 `cause` 标为 `artifact_missing`。
7. WHEN `evaluation_key` 与 `simulation_key` 两级均未命中而需要真实启动 Simulink 时，THE `BudgetLedger.reserve` SHALL 在写入该 `runs` 行的同一 `BEGIN IMMEDIATE` 事务内、且在调用 `sim.simulate()` 之前先占 `budget_units`，其值为 1 并在 `require_margin` 为真时加上 `metrics.yaml` 的 `margin_extraction.extra_engine_starts_per_candidate`，且在该事务回滚时使该 `runs` 行与其 `budget_units` 同时不生效。
8. WHEN 发生 `transient_error` 重试时，THE `BudgetLedger` SHALL 为每次重试按该场景的完整 `budget_units` 重复计入预算。
9. THE `BudgetLedger.used()` SHALL 等于按 `task_id` 过滤的 `SUM(runs.budget_units)`，该求和计入该任务全部 `runs` 行而不排除任何状态的行（含 `status='failed'`、`cause='process_restart'` 与 `cache_hit=1` 的行），并在该任务无 `runs` 行时取 0。
10. IF 本次真实启动所需的 `budget_units` 大于 `BudgetLedger.remaining()`，THEN THE `run_task()` SHALL 不先占该 `budget_units`、不启动 Simulink、以 `stop_reason='budget_exhausted'` 停止任务，并使 `SUM(runs.budget_units)` 不超过 `budget_max_starts` 与已批准追加量之和。
11. WHEN 工程师批准追加预算时，THE `BudgetLedger.grant` SHALL 把追加额度写入该 `approvals` 行的 `extra_units` 列、不在内存中持有该额度，并使 `BudgetLedger.remaining()` 等于 `tasks.budget_max_starts + SUM(approvals.extra_units WHERE task_id=? AND kind='budget_increase' AND decision='approve') − BudgetLedger.used()`，从而在崩溃重启后可完全从 SQLite 重建可用额度，且使同一 `approval_id` 至多计入一次。
12. THE `config.schema` SHALL 约束 `metrics.yaml` 的 `margin_extraction.extra_engine_starts_per_candidate` 满足 `0 <= v <= 4`，并在越界时抛出校验错误、不返回配置模型，使单个字段无法任意放大 AC7 的预算先占量与 Requirement 2 的墙钟估算式。

**追溯**：design.md §5.1 / §5.3 / §5.3.1 / §5.4 / §6.2.3 / §8.4 / §8.6 / §10 CP-4、CP-5 / §11.1 / §13 / §16 T7 / §17.1 Q5 / §17.2 L8-3、L15-1 ~ L15-4；V2.0 §11.3、§12.2 验收 5。

---

### Requirement 16: 失败分类、停止判定与中断恢复

**User Story:** 作为使用者，我希望失败只分四类、停止原因始终可打印、且崩溃重启不覆盖历史记录，以便我能据停止原因直接决定下一步动作。

#### Acceptance Criteria

1. THE `store` SHALL 把失败分类限定为 `candidate_rejected`、`transient_error`、`stop_and_ask_human`、`budget_exhausted` 四类，细节写入 `cause` 文本字段；THE `store` SHALL 使 `transient_error` 类的 `cause` 取值含 `artifact_missing`（`simulation_key` 命中但产物缺失或往返校验失败时标记，归 `transient_error` 而非 `candidate_rejected`，因产物丢失是存储层问题、与候选可行性无关），并使 `cause` 取值集合不含 `retrieval_insufficient`。
2. THE `store.repo` SHALL 以 `Literal` 枚举承载 `cause` 取值集合，新增取值须修改代码而非修改配置；THE `run_task()` SHALL 在运行期不产生 `dual_model_inconsistent`、`parallel_nondeterminism` 与 `baseline_infeasible` 三个取值——前两者只由 `controller.preflight` 与 Dual-Model Consistency / Parallel Determinism 两个条件测试层产生，`baseline_infeasible` 只由 `controller.preflight` 的 Baseline 门禁分支产生并落在 `task_kind='baseline_gate'` 的 `tasks` 行。
3. WHEN 一个候选被判不可行时，THE `run_task()` SHALL 以 `candidate_rejected` 记录原因并继续处理下一个候选。
4. IF `SimulationResult.status` 为 `engine_transient` 且该 `(候选, 场景)` 的当前 `attempt` 小于 `task.yaml` 的 `budget.max_attempts_per_scenario`（该字段是原 `budget.max_transient_retries` 的改名，语义为「每候选每场景的最大 `attempt` 数」，默认 2 即最多重试 1 次），THEN THE `run_task()` SHALL 把当前 `runs` 行以 `status='failed'`、`failure_class='transient_error'`、`cause='engine_transient'` 与非空 `ended_at` 关闭、新开 `attempt` 加 1 的 `runs` 行重试（`attempt` 自 1 起编号、每候选每场景独立计数）、为该次重试按该场景的完整 `budget_units` 计入预算，并不修改任何已为 `done` 或 `failed` 的行。
5. IF 该 `(候选, 场景)` 的 `attempt` 已达 `task.yaml` 的 `budget.max_attempts_per_scenario` 而 `status` 仍为 `engine_transient`，THEN THE `run_task()` SHALL 把该 `runs` 行以 `status='failed'`、`failure_class='transient_error'`、`cause='engine_transient'` 关闭、不再新开 `attempt` 行、不写入 `tasks.stop_reason`、继续处理下一个候选，并使该候选因缺少完整 Evaluation 集而不进入 worst-case 聚合。
6. WHEN `should_stop()` 判定停止时，THE `run_task()` SHALL 把取值属于 `{budget_exhausted, stop_and_ask_human, no_improvement, target_reached}` 的非空 `stop_reason` 写入 `tasks` 行、把其伴随 `cause` 写入 `tasks` 表的 `cause` 列（取值为 `agent_returned_no_candidate`、`agent_recommended`、`first_feasible` 与 `stop_and_ask_human` 的全部 `cause`，无伴随 `cause` 时写空值）、使 `TaskOutcome.stop_reason` 与 `TaskOutcome.cause` 分别与这两列相同，并打印该 `stop_reason` 与其伴随 `cause`（无伴随 `cause` 时该位置打印为空）。
7. WHEN `agent.propose` 返回的 `stop_recommendation` 为真且连续无改善轮数不小于 1 时，THE `controller.stop.should_stop` SHALL 以 `stop_reason='no_improvement'` 与 `cause='agent_recommended'` 停止；WHEN 该建议为真而连续无改善轮数为 0 时，THE `controller.stop.should_stop` SHALL 判定为继续。
8. WHERE `task.yaml` 提供 `objective_target.target_value`，WHEN 当前最佳可行候选的主目标聚合值满足严格的 `value <= target_value` 时，THE `should_stop()` SHALL 以 `target_reached` 停止；THE `should_stop()` SHALL 不对该比较套用 `metrics.yaml` 的 `objective.tie_tolerance`，因 `tie_tolerance` 描述的是测量分辨力而 `objective_target` 是使用者写下的显式达标线，需要留余量时由使用者自行调低 `target_value`。
9. WHERE `task.yaml` 的 `stop.stop_on_first_feasible` 为 `false`（默认值），THE `should_stop()` SHALL 在出现首个可行候选时继续寻优，并把首次可行解轮次记录为日志字段。
10. WHEN 连续无改善轮数达到 `task.yaml` 的 `stop.no_improvement_rounds` 时，THE `should_stop()` SHALL 以 `no_improvement` 停止。
11. WHEN 每轮结束时，THE `run_task()` SHALL 先按最差场景聚合刷新当前最佳可行候选的主目标聚合值，在该值相对本轮开始前的减少量不大于 `metrics.yaml` 的 `objective.tie_tolerance` 时把连续无改善轮数加 1、在该减少量大于该容差或本轮首次出现可行候选时把连续无改善轮数置 0；WHEN `agent.propose` 返回空候选集因而本轮不产生新 `runs` 行时，THE `run_task()` SHALL 把连续无改善轮数加 1。
12. WHEN 进程启动后、在 `controller.preflight`、Checkpoint 1 与首轮迭代之前，THE `controller.recovery.reap_orphan_runs` SHALL 无条件执行（不由 `resume` 参数门控），在一个事务内把全部 `status='running'` 的行置为 `status='failed'`、`failure_class='transient_error'`、`cause='process_restart'` 与非空 `ended_at`、保留其 `budget_units`、不修改任何已为 `done` 或 `failed` 的行，并返回处理行数；该判定不作心跳判活，单进程串行下进程启动即意味上一进程已终止，因此残留的 `running` 行永远是孤儿——按 `resume` 门控只会让它在 `resume` 为假的启动中永久残留并持续计入预算，且使 AC14 第四条断言在该情形下不成立。
13. WHEN 恢复完成后，THE `controller.recovery.rebuild_state` SHALL 完全从 SQLite 重建 `SearchState`，`run_task()` 不持有不可恢复的隐藏业务状态。
14. WHEN 在仿真前、仿真后、产物写入与数据库提交四处注入点中任一处崩溃并重启后，THE PowerAgent 系统 SHALL 使同一 `(task_id, candidate_id, scenario_id, attempt)` 至多存在一条终态 `runs` 行、使崩溃前已提交的终态行内容逐字段不变、使按 `task_id` 过滤的 `SUM(runs.budget_units)` 不小于崩溃前的值，且不存在 `status='running'` 的行。
15. WHEN 多个停止条件同时成立时，THE `controller.stop.should_stop` SHALL 按「预算已耗尽 → `budget_exhausted`；存在待处理 `stop_and_ask_human` 条件 → `stop_and_ask_human`；`objective_target.target_value` 已达 → `target_reached`；`stop.stop_on_first_feasible` 为真且存在可行候选 → `target_reached`；连续无改善轮数不小于 `stop.no_improvement_rounds` → `no_improvement`」的固定顺序返回首个成立条件对应的 `stop_reason`、在均不成立时返回继续，且不新增该四项之外的 `stop_reason` 取值。
16. WHEN 进程结束时，THE `cli` SHALL 按「`no_improvement` 与 `target_reached` ⟹ 退出码 0；`budget_exhausted` 与 `stop_and_ask_human` ⟹ 退出码 2；参数或配置错误（含 `PreflightError`）⟹ 退出码 1」的固定映射返回退出码，使外部脚本可仅凭退出码判断是否需要人介入而不解析文本，且不为「达标与否」单设退出码取值（达标与否从 `stop_reason` 与报告读取）。

**追溯**：design.md §5.1 / §6.1 / §6.2.3 / §6.2.5 / §8.7 / §8.8 / §10 CP-7 / §11.1 / §11.2 / §16 T11 / §17.1 Q4、Q7 / §17.2 L15-2、L16-1 ~ L16-4、L19-2、L24-1；V2.0 §11.2、§11.4、§12.2 验收 5。

---

### Requirement 17: 结果可复现性与回归门禁

**User Story:** 作为技术负责人，我希望同一配置的重复运行在容差内一致、且固定 fixture 候选的指标受 CI 断言保护，以便仿真接口、指标计算或候选校验的回归被立即发现。

#### Acceptance Criteria

1. WHEN 同一候选与同一场景在两次运行中的 `model_package_hash`、`execution_env_hash`、`metrics_hash` 与 `constraints_hash` 四者相同时，THE PowerAgent 系统 SHALL 使两次均 `valid=1` 的每个 `metric_id` 满足 `|m₁ − m₂| ≤ max(compare_tolerance.absolute, compare_tolerance.relative × max(|m₁|, |m₂|))`（`compare_tolerance` 的组合语义确认为 `max(absolute, relative × 参考值)`，未给出的字段按 0 计，该口径由指标 Owner 在 M1 出口的审批记录中确认）、使每个 `metric_id` 的 `valid` 标志在两次运行中相同，并使每条 `constraint_results.feasible` 分类在两次运行中相同。
2. WHEN 同一配置的两次运行完成时，THE PowerAgent 系统 SHALL 使两次 `tasks` 行的 `model_package_hash`、`metrics_hash`、`constraints_hash`、`scenario_set_hash`、`execution_env_hash` 与 `calibration_hash` 六者逐字符相同（`calibration_hash` 绑定 `configs/calibration.yaml` 与数据集划分记录，PoC 轨两次均为空串）、使按 `started_at` 排序的 `runs.status` 迁移序列相同、使每条 `constraint_results.feasible` 相同、使 `SUM(runs.budget_units)` 相同、使产物引用存在性相同（`waveform_ref` 两次均非空，`require_margin` 为真时 `observable_ref` 两次均非空，引用串中仅 `task_id` 与 `run_id` 段允许不同），浮点指标按 AC1 的容差规则判定而不要求逐位一致。
3. THE CI 回归 SHALL 对 3 个固定 fixture 候选的每个 `metric_id` 与随 fixture 入库于 `tests/fixtures/expected/<candidate_id>.json` 的期望值比较，套用 AC1 的容差规则并同时比较 `valid` 标志，且使失败信号指向具体 `metric_id`；期望值文件与波形资产同目录树，其变更须在 PR 描述中说明理由（过程性约束，不落代码）。
4. THE CI 回归 SHALL 仅以入库的 3 个固定 fixture 候选及其固定波形资产为输入，不调用 `agent.propose`、`agent.validate` 与 `controller.run_task` 等候选生成或寻优组件，且不设算法对照基线。
5. THE 测试体系 SHALL 由 Schema/Unit、Metric Fixture（时域）、Metric Fixture（裕量）、MATLAB Contract、Baseline Gate、Integration、Recovery 七层，加 Dual-Model Consistency 与 Parallel Determinism 两个条件层构成，不新增测试层。
6. THE Schema/Unit 层与两个 Metric Fixture 层 SHALL 在既未安装 MATLAB 也不具备 MATLAB 许可证的环境中全部通过，使其执行期间的 MATLAB engine 启动数为 0，并使其输入仅取自仓库内已入库的固定二进制波形资产与配置文件、不读取运行期生成的产物。
7. WHEN 每次 push 与每次 PR 发生时，THE CI SHALL 被触发；WHERE CI 环境不具备 MATLAB 许可证，THE CI SHALL 只执行 Schema/Unit 与两个 Metric Fixture 层这三层，并把 MATLAB Contract、Baseline Gate、Integration、Recovery 与两个条件层共六层在结果中标记为「未执行 · 本地执行」、不把这六层记为通过；许可证具备后可把 Integration 与 Baseline Gate 纳入 CI，但该纳入不构成本轮的验收标准。
8. IF AC1 的比对中任一 `metric_id` 的差异超出容差、任一 `metric_id` 的 `valid` 标志不同、或任一 `constraint_results.feasible` 分类不同，THEN THE Integration 层 SHALL 判定该比对不通过、输出不一致的 `metric_id`、两次的 `run_id` 与两次的指标值，并不修改已入库的两次结果。
9. IF CI 回归中任一 fixture 候选的任一 `metric_id` 超出容差或其 `valid` 与期望值不同，THEN THE CI 回归 SHALL 判定失败，并输出该 `metric_id`、该 fixture 候选标识、期望值与实测值。
10. THE PowerAgent 系统 SHALL 只在 Integration 层与 CI 回归中判定 AC1 的可复现性属性（CP-1）、在运行期不判定该属性，且不为该属性的违反新增 `runs.cause` 取值；运行期判定它必须把每个候选跑两遍，代价与收益不成比例。
11. THE Schema/Unit 层 SHALL 以 `tests/unit/test_report_wording.py` 承载报告用语的负向约束，断言三项：报告文本不含禁用词表所列词及其等价表述、不出现 `regret` / `simulations-to-target` / `搜索效率` / `收敛速度` 四类表述、`runs` 表派生的过程量日志字段（首次可行解轮次、可行候选率、重复率）不出现在结论段的渲染上下文键中；该断言不新增测试层，测试分层仍为 7+2。

**追溯**：design.md §5.1 / §6.10 / §10 CP-1 / §12 / §16 T16、T22 / §17.1 Q6 / §17.2 L17-1 ~ L17-6、L21-4、L23-4；V2.0 §12.1、§12.2 验收 4。

---

### Requirement 18: 报告渲染、数值可回溯与结论边界

**User Story:** 作为工程师，我希望寻优前后对比报告由模板直接渲染数据库、每个数值都能回溯到具体记录，以便报告不含幻觉数值也不含越界结论。

#### Acceptance Criteria

1. WHEN 执行 `poweragent report --task <id>` 时，THE `report.render_report` SHALL 用 Jinja2 模板渲染 SQLite 内容，在未显式指定输出路径时输出 `artifacts/<task_id>/reports/report_<task_id>.md` 并返回该路径，且不发起任何 LLM 推理 API 调用、使 `llm_calls` 表行数在渲染前后相同。
2. THE `report.build_context` SHALL 使模板可见的全部数值来自 SQL 查询结果，模板中不出现数据库之外的数值。
3. WHEN 报告渲染完成时，THE PowerAgent 系统 SHALL 使报告中出现的每一个数字字面量（模板固定文本中的章节序号与 `Top 1/2/3` 名次序号除外）都能以主键定位到 `metric_results`、`constraint_results`、`runs` 或 `tasks` 中的唯一一条记录，并使报告不含无法如此定位的数字字面量；THE `report` SHALL 对全部渲染数值统一采用 design.md §5.3.1 登记的报告数值格式（`format(v, ".4g")`）。
4. THE `templates/report.md.j2` SHALL 依次渲染结论边界声明、任务与配置哈希、Baseline 前值基线、Top 1/2/3 的参数与 worst-case 指标、硬约束逐条结果、寻优前后对比表、关键波形图、过程埋点摘要，以及 `推荐理由` 与 `限制说明` 两个占位段，其中结论边界声明按 `tasks.simulation_only` 的取值二选一渲染且两个分支的文本均只取自模板已定义的结论表述集合，Top 名次取自 `eval.aggregate` 排序的前三名并在可行候选少于三个时按实际数量渲染、把缺位名次标记为不可用，且 `report.render_report` 的输出中两个占位段为空段。
5. IF `推荐理由` 或 `限制说明` 占位段由 LLM 填写，THEN THE PowerAgent 系统 SHALL 以按 design.md §5.3.1 的报告数值格式格式化后的字符串形式判定该段中的全部数字字面量是否属于模板已渲染数值集合（两侧使用同一格式化函数），并在出现任一不属于该集合的数字字面量时判定该报告不合规。
6. THE `report` SHALL 使渲染的全部数值只来自 SQL 查询结果、不渲染项目级结论语句（含达标线判定、采纳结论与否决结论），且不向 `approvals` 表写入任何行、使该表行数在渲染前后相同。
7. IF 人工介入、任务开始与结束时间戳、LLM 调用三类埋点中任一项未落库，THEN THE `report` SHALL 在报告中把该项标记为不可用，且不进行估算、不填入默认值、不做插值、不取用其他任务的值。
8. THE `report.plots` SHALL 把 Baseline 候选与 Top 1/2/3 候选各自主目标 worst-case 所在的 Evaluation 场景运行渲染为关键波形图、把按 `model_package_hash` 与 `constraints_hash` 两个哈希与当前 `tasks` 行逐一相同的最近一次成功的 `task_kind='dense_grid'` 任务的参考扫描记录渲染为响应面图，输出到 `artifacts/<task_id>/plots/`，并使报告以文件引用方式嵌入这些图、图题内不出现库外数值；IF 不存在这样的 `dense_grid` 任务，THEN THE `report` SHALL 把该图标记为不可用、不新增跨任务引用列、不取用两个哈希不匹配的扫描记录。
9. IF `--task` 指定的 `task_id` 在 `tasks` 表中不存在，THEN THE `cli.cmd_report` SHALL 拒绝渲染、不产生报告文件与图文件、以非零退出码结束，并输出指明该任务不存在的错误提示。
10. WHERE `tasks.stop_reason` 为空（任务进行中），THE `report.render_report` SHALL 允许渲染报告、在报告顶部标注「任务进行中 · 非终态」，并按当前已完成数据渲染 Top 名次与寻优前后对比表且把两者一并标注为非终态；THE `report.render_report` SHALL 不为该情形提供拒绝渲染的分支。
11. THE PowerAgent 系统 SHALL 只在 `tests/unit/test_report_numeral_closure.py` 中于开发期断言 AC5 的占位段数值子集属性，运行期不执行该校验，且不引入运行期的引用校验器角色。
12. THE `report` SHALL 使指标数值的单位取自 `metrics.yaml` 的 `metrics.<metric_id>.unit`、使硬约束条目的单位取自 `constraints.yaml` 中该约束声明的单位，且依赖 `controller.preflight` 按 Requirement 2 的 AC13 在启动期断言同一物理量在两处声明的单位一致，因此渲染期不做单位优先级仲裁。

**追溯**：design.md §5.3.1 / §6.2.2 / §6.10 / §6.12 / §10 CP-9、CP-15 / §16 T20 / §17.2 L18-1 ~ L18-5；V2.0 §9.3、§9.4、§12.2 验收 8；原始需求文档 §3 任务6、§4 交付物5。

---

### Requirement 19: 三个人工介入点、审批与 Apply 落盘

**User Story:** 作为电源设计工程师，我希望人工介入只发生在三个位置、且模型落盘必须由我批准，以便自动化收益不被频繁询问抵消，同时安全边界与最终设计决策仍在我手上。

#### Acceptance Criteria

1. WHEN `controller.preflight` 通过且预算账本与 `SUM(runs.budget_units)` 的一致性断言完成后、进入寻优主循环之前，THE `run_task()` SHALL 打印目标、硬约束、参数范围与档位、场景、预算五项，在已传入 `--yes` 时直接判为确认、未传入时读取恰好一次交互确认输入，把确认通过的结果写入 `approvals(kind='checkpoint1', decision='approve')`，并使该行写入之前不新增任何 `runs` 行、不增加预算已用量。
2. WHILE 任务处于自动执行中，THE `run_task()` SHALL 使其循环体内不存在 `input()` 或任何等待人工输入的调用，使 `stop_reason` 为 `stop_and_ask_human` 与 `budget_exhausted` 的两种终止进入 Checkpoint 2，并使 `stop_reason` 为 `no_improvement` 与 `target_reached` 的两种终止不触发任何人工介入。
3. WHEN Checkpoint 2 触发时，THE `cli` SHALL 打印停止原因并以非零退出码结束。
4. WHEN 工程师在 Checkpoint 3 批准或否决最终推荐时，THE `cli.cmd_report` SHALL 以 `poweragent report` 的新增参数承载该审批入口（`--approve <candidate_id> --approver <name> --second-approver <name> [--note <text>]` 与 `--reject <candidate_id> --approver <name> --note <text>`）而不新增第五个 CLI 命令，且 THE `store` SHALL 写入 `approvals(kind='final_recommendation')` 行并使 `decision`、`approver`、`second_approver`、`result_hash` 与 `candidate_id` 五列均非空；WHEN `kind` 为 `track_selection` 时，THE `store` SHALL 使 `decision`、`approver`、`second_approver` 与 `result_hash` 四列均非空；IF 上述任一列为空，THEN THE `store.record_approval` SHALL 拒绝该写入且不产生审批行。
5. THE `store.record_approval` SHALL 使每条审批行的 `result_hash` 等于写入时刻按 design.md §5.3.1 登记的重算范围（候选参数、`worst_case`、逐场景指标、逐场景约束判定与 `model_package_hash` / `constraints_hash` / `metrics_hash` / `scenario_set_hash` 四个哈希；`runs.elapsed_ms`、`cache_hit` 与 `budget_units` 等过程量不在范围内）重算所得的值、在重算值与待写入值不一致时拒绝写入，并使已写入的 `approvals` 行不存在被更新或删除的代码路径。
6. WHEN 执行 `poweragent apply --candidate <id> --approval <id>` 时，THE `sim.apply_model.apply_candidate` SHALL 校验存在 `kind='final_recommendation'`、`decision='approve'`、`candidate_id` 匹配且 `result_hash` 等于按 AC5 的同一重算范围算出的当前值的审批行。
7. IF 审批行不存在、其 `kind` 非 `final_recommendation`、其 `decision` 非 `approve`、其 `candidate_id` 与本次调用不匹配、或其 `result_hash` 与按 AC5 的重算范围算出的当前值不等，THEN THE `sim.apply_model.apply_candidate` SHALL 拒绝执行、不产生任何模型文件与临时文件、不写入模型包登记记录、使原模型文件的内容哈希与调用前相同，并返回指示未通过校验项的错误。
8. WHEN 审批校验通过时，THE `sim.apply_model.apply_candidate` SHALL 先把注入该候选参数后的模型写入临时路径、再以一次原子移动生成路径含 `candidate_id` 的新模型文件，使原模型文件的内容哈希与调用前相同，并在写入或移动中断时不留下部分写入的模型文件。
9. THE `sim.apply_model.apply_candidate` SHALL 把新的 `model_package_hash` 作为独立模型包登记，`freezes(kind='model_package')` 的原值保持不变。
10. THE `cli.cmd_apply` SHALL 是 `apply_candidate` 的唯一调用入口，`controller` 与 `agent` 不提供调用该函数的代码路径。
11. IF Checkpoint 1 的确认结果为 `reject` 或未取得确认，THEN THE `run_task()` SHALL 写入 `approvals(kind='checkpoint1', decision='reject')` 行、不进入寻优主循环、不新增任何 `runs` 行、不增加预算已用量，并打印未确认原因。
12. IF 派生出的新模型文件路径已存在，THEN THE `sim.apply_model.apply_candidate` SHALL 拒绝执行、不修改该路径下的既有文件、不修改原模型文件，并返回指示目标路径已存在的错误。
13. THE `cli` SHALL 要求 `--approver` 在命令行显式给出、不从环境变量、git config 或任何其他来源推断审批人身份（自动推断身份会让误签变得无声），且 THE `store.record_approval` SHALL 断言 `second_approver` 与 `approver` 不相等、在两者相等时拒绝该写入且不产生审批行。
14. WHEN 进程结束时，THE `cli` SHALL 按 Requirement 16 的 AC16 的同一映射返回退出码（`no_improvement` 与 `target_reached` ⟹ 0；`budget_exhausted` 与 `stop_and_ask_human` ⟹ 2；参数或配置错误含 `PreflightError` ⟹ 1），使 Checkpoint 2 的两类终止与正常收敛在退出码上可区分。

**追溯**：design.md §5.3.1 / §6.1 / §6.3.3 / §7.6 / §8.10 / §9.1 / §10 CP-10 / §11.2 / §14 / §17.2 L19-1 ~ L19-4；V2.0 §6.5、§11.5、§12.2 验收 8；原始需求文档 §4 交付物4（输出优化后的设计参数集）。

---

### Requirement 20: CLI 命令、状态输出与过程埋点

**User Story:** 作为使用者，我希望四个 CLI 命令覆盖全部操作、状态行一眼看清进度与阻塞原因，且过程埋点从 M1 起连续，以便端到端评估所需数据不因补录缺失而失真。

#### Acceptance Criteria

1. THE `cli` SHALL 提供 `poweragent run <task.yaml>`、`poweragent report --task <id> [--approve <candidate_id> --approver <name> --second-approver <name> [--note <text>]] [--reject <candidate_id> --approver <name> --note <text>]`、`poweragent apply --candidate <id> --approval <id>`、`poweragent log --task <id> --reason <text> [--checkpoint <cp>] [--action <text>]` 恰好四个命令，使 `poweragent --help` 列出的子命令集合与该四项完全一致、使各命令的用法串与 `cli` 中对应函数的签名逐参数一致，且不含第五个子命令的代码路径。
2. WHILE `poweragent run` 执行中，WHEN 每一条 `runs` 行由 `status='running'` 转为终态时或每一轮寻优结束时，THE `cli.format_status` SHALL 输出取自 `RunSnapshot.phase` 的当前阶段、取自 `tasks.simulation_only` 的验收轨道、取自 `BudgetLedger.used()` 的已用预算与取自 `BudgetLedger.remaining()` 的剩余预算（均以 engine starts 计）、当前最佳可行候选的 `candidate_id`（尚无时取固定占位值 `none`）、`cp1` 与 `cp3` 合计与 `cp2` 合计两个人工介入计数、自 `tasks.started_at` 起的累计墙钟（单位 h、保留 1 位小数）、该 `task_id` 全部 `llm_calls.tokens` 之和、取自 `tasks.stop_reason` 与 `tasks.cause` 的阻塞原因（未停止时取 `none`），以及产物目录路径；THE `cli.format_status` SHALL 不输出 LLM 累计成本字段（单价会漂移、需要一个新配置项承载且不影响任何判定，成本核算由使用方在 LLM 服务商侧完成），使 `RunSnapshot.phase` 为取值属于 `{preflight, checkpoint1, optimize, finish}` 的内存态枚举、只用于显示、不参与任何判定、不新增表列（从 SQLite 重建后取 `optimize`），并不引入定时器或按固定时间间隔输出的路径。
3. THE `cli.format_status` SHALL 使返回值不含换行符并写入标准输出，且不使用光标定位、屏幕重绘或分屏控件。
4. WHEN 执行 `poweragent log` 时，THE `store.log_intervention` SHALL 写入一条 `interventions` 行，其 `checkpoint` 取值属于 `{cp1, cp2, cp3, other}`（未提供 `--checkpoint` 时取 `other`）、`reason` 为 1 至 500 字符的非空文本、`action` 在未提供时为空字符串、`created_at` 为写入时刻由系统时钟生成的 UTC ISO 8601 秒级时间戳，且不接受由命令行参数指定的时间戳。
5. THE `store` SHALL 从 M1 起使每条 `tasks` 行的 `started_at` 非空、使每条 `stop_reason` 非空的 `tasks` 行的 `ended_at` 非空、使每条 `llm_calls.created_at` 落在同一 `task_id` 的 `started_at` 与 `ended_at` 之间（任务未结束时以查询时刻为上界），并使每条 `interventions.created_at` 不早于同一 `task_id` 的 `started_at`（Checkpoint 3 的介入晚于任务结束属合法情形，不计违规）。
6. THE `store` SHALL 使 Checkpoint 1 与 Checkpoint 3 的介入计数与 Checkpoint 2 的介入计数作为两个独立数值可被查询，且不提供把两者相加为单一人工介入计数的输出路径。
7. THE `cli` SHALL 不决定候选、场景或重试策略，其对 MATLAB 的访问仅通过 `sim.apply_model`。
8. THE PowerAgent 系统 SHALL 只从环境变量 `POWERAGENT_LLM_API_KEY` 读取 LLM API Key，不把该凭据写入四个 YAML、数据库或 `artifacts/`。
9. IF `cli` 收到未定义的子命令、缺失必需参数、`--checkpoint` 取值不属于 `{cp1, cp2, cp3, other}`、或 `--task` 指向 `tasks` 表中不存在的 `task_id`，THEN THE `cli` SHALL 输出指明拒绝原因的错误、以非零退出码结束、不写入任何数据库行，且不生成任何产物文件。
10. IF 某任务的人工介入、任务开始与结束时间戳、LLM 调用三类埋点不满足 AC5 的连续性判定，THEN THE `report` SHALL 把依赖该埋点的指标标记为不可用，且不输出估算值与事后补录值。

**追溯**：design.md §5.1 / §6.1 / §14 / §16 T21 / §17.2 L20-1 ~ L20-5；V2.0 §9.4、§10.1；原始需求文档 §3 任务6。

---

### Requirement 21: 端到端任务评估与结论口径

**User Story:** 作为需求方，我希望系统价值由固定任务集上的端到端评估承担、且结论用原始数值描述，以便我看到的是与工程师手动流程的直接对照而不是统计学包装。

#### Acceptance Criteria

1. WHEN M1 出口判定通过时，THE PowerAgent 项目 SHALL 在 0.5 个工作日（4 工作小时）内定稿 `task_set.md`，内含 3 至 5 个任务、每个任务的目标表述、以「冻结约束下的可行判定与主目标数值方向」表述且不含主观措辞的成功判定，以及工程师手动完成同一任务的估时（单位 h、保留 1 位小数）。
2. WHEN `task_set.md` 定稿时，THE PowerAgent 项目 SHALL 加注日期并把其 sha256 写入 `freezes(kind='task_set')`，此后不修改该文件；THE PowerAgent 系统 SHALL 不对该加注日期与 `freezes(kind='task_set').frozen_at` 的一致性做机器校验（由人工核对，解析人写的自然语言日期属过度设计，内容未变已由 sha256 保证）。
3. THE PowerAgent 项目 SHALL 使 `freezes(kind='task_set')` 行的 `frozen_at` 早于 `agent.propose`（design.md §16 的 T17）的首个代码提交时间戳，并在 M4 评审时逐项核对这两个时间戳。
4. WHEN M5 执行时，THE PowerAgent 项目 SHALL 为每个任务记录三个指标的原始数值：（a）「是否在预算内找到可行候选」，其为真的判据是在 `SUM(runs.budget_units) ≤ tasks.budget_max_starts` 的前提下存在至少一个在 Evaluation 层全部场景 `constraint_results.feasible=1` 的候选，同时记录 `origin='agent'` 的候选数、`SUM(runs.budget_units)` 与 `tasks.budget_max_starts`；（b）端到端墙钟，取 `tasks.ended_at − tasks.started_at` 换算为 h 并保留 1 位小数、为含等待的单值口径（含 MATLAB 许可证等待、`runs.status='waiting'` 的工程师等待与纯计算时长，三者不分列——与工程师估时对照的是「从需求到候选的总时长」，工程师估时同样含其等待）、与 `task_set.md` 的工程师估时并列；（c）工程师是否判定候选值得进一步验证，其判定对象为该任务终止时排序键最优的可行候选，落成一条 `approvals(kind='m5_assessment')` 行，其 `decision` 取 `approve`（值得进一步验证）或 `reject`（不值得）、`note` 存至少一条理由文本、`candidate_id` 绑定该判定对象、`approver` 记判定人标识。
5. THE PowerAgent 项目 SHALL 使任务级指标恰好为 AC4 的三项，且不在 `task_set.md`、任务报告与交付物6 项目总结文档中为这三项定义阈值、达标线、评分或放行判定。
6. THE 报告与总结文档 SHALL 以描述性表述呈现 M5 结果，使其文本不出现「显著」「显著性」「非劣」「非劣性」「p 值」「假设检验」「统计功效」「统计上」这 8 个词，把样本量标注为任务数（3 至 5）与每个任务的 `SUM(runs.budget_units)`，并标注 AC4（c）项为单人主观判定；THE Schema/Unit 层 SHALL 以 Requirement 17 的 AC11 所述 `tests/unit/test_report_wording.py` 的禁用词表扫描承载该 8 词的自动检查，不新增测试层。
7. THE PowerAgent 项目 SHALL 把首次可行解轮次、可行候选率与重复率记录为 `runs` 表派生的日志字段，不作为项目结论指标。
8. WHEN M4b 执行时，THE PowerAgent 项目 SHALL 产出篇幅不超过 1 页的观察记录，使该记录不进入任务报告与交付物6 项目总结文档，且不作为 M5 启动或任何里程碑放行的前置条件。
9. THE 交付物6 项目总结文档 SHALL 回答三句结论：「做到了什么」逐任务引用 AC4 的三项数值、「没做到什么」列出 AC4 数值未覆盖的缺口与未启用的条件模块、「下一步需要什么」逐条给出后续项，且三段均不引入 AC4 与 AC7 记录之外的数值。
10. IF 某任务终止时不存在可行候选，THEN THE PowerAgent 项目 SHALL 仍记录三项原始数值，使 AC4（a）项的布尔值取假并给出 `SUM(runs.budget_units)` 与 `tasks.budget_max_starts`、AC4（b）项照常计算、AC4（c）项记为「不适用」并附不适用理由，且三项均不留空。

**追溯**：design.md §4.5 / §5.1 / §6.10 / §11.2 / §12 / §16 T14、T23、T24 / §17.2 L21-1 ~ L21-4；V2.0 §9.2、§9.5、§12.2 附加项、§13.2；原始需求文档 §3 任务6、§4 交付物6。

---

### Requirement 22: 条件能力——模型校准（M2）

**User Story:** 作为数据 Owner，我希望在实测数据合格时执行模型校准并输出参数置信区间，以便「使仿真结果尽可能逼近实际测试结果」这一首要目标有可核查的进展。

#### Acceptance Criteria

1. WHERE `simulation_only` 为 `false`，THE `calib.estimate` SHALL 只从 `configs/calibration.yaml` 的校准参数表估计校准值，使该表条目数为 3 至 5、每条含单位、批准物理范围（下界小于上界）、来源与可观测信号四个非空字段，并使该文件含取值为 `0.95` 的 `confidence_level` 字段作为 `confidence_interval` 的置信水平，其中「高影响且可辨识」由工程师在 M2 审批记录中逐条判定并记录。
2. THE `calib` SHALL 以（板卡, 版本, 工况组）三元组为最小划分单元把数据划入 Calibration / Validation / Test 三个数据集，使同一三元组的全部波形整体分入其中一个集合、三个集合的三元组集合两两无交集且各至少含 2 个三元组（`configs/calibration.yaml` 的 `dataset_partition.min_triplets_per_partition`），并使 Test 分区额外覆盖至少 2 个不同工况组（`dataset_partition.test_min_condition_groups`），且不把同一波形切片后随机分散到不同集合。
3. THE `calib.estimate` SHALL 只读 Calibration 分区拟合参数、只用 Validation 分区在多初值结果间选取采纳结果、在校准值确定后只读取 Test 分区一次用于计算 Test 误差，且不以 Test 分区作为任何阈值的来源。
4. THE `calib.estimate` SHALL 采用多初值局部最小二乘（`scipy.optimize.least_squares`，`multi_start=5`，初值取批准范围内的 Latin Hypercube）执行参数估计。
5. WHEN 校准完成时，THE `calib` SHALL 输出每个校准参数的校准值、批准物理范围、按 `confidence_level` 计算且与该参数单位一致、下界不大于上界的 `confidence_interval` 与多初值收敛离散度，全部校准参数两两组合的相关系数（无量纲、取值在 −1 至 1 之间），Calibration / Validation / Test 三个分区各一个并标注单位的误差数值，以及 Residual vs Time / Vin / Load / Temperature 四张诊断图各一张；IF 采纳解与其余收敛解的相对离散度大于 `configs/calibration.yaml` 的 `multi_start.spread_warn_rel`（0.10），THEN THE `calib` SHALL 输出告警并要求工程师复核残差图，且不阻断本次估计。
6. IF 残差对 Vin、Load 或 Temperature 之一做一元线性回归所得的 `|斜率 × 该自变量量程|` 大于该分区的残差 RMS（即判为该自变量方向上存在系统性偏差），THEN THE PowerAgent 项目 SHALL 在 M2 审批记录中写入模型结构复查结论，并使再次估计时校准参数表的条目数不多于原批准条目数、参数集变更须重新审批。
7. WHEN 校准模型通过冻结审批时，THE `calib.export_uncertainty` SHALL 以机器可读形式输出 `calibration_uncertainty_for_robustness`，使其条目数等于批准校准参数条目数、每项含 `parameter`、等于该参数冻结校准值的 `frozen_value`，以及下界不大于上界且单位与该参数一致的 `confidence_interval`，供 M6 消费。
8. WHERE `simulation_only` 为 `true`，THE `calib.export_uncertainty` SHALL 输出空文件，且 THE `report` SHALL 说明该维度缺失的原因。
9. THE PowerAgent 系统 SHALL 不在同一次优化中混合调整校准参数与设计变量，两类参数分表管理。
10. WHEN 冻结校准模型时，THE PowerAgent 项目 SHALL 仅在「Test 分区误差按逐指标 worst-case 口径（各工况组取最差）逐项不超过 `metrics.yaml` 中对应指标的 `compare_tolerance`」「每个校准值落在该参数批准物理范围内（含边界）」「审批记录绑定的模型包哈希、数据集划分记录与配置哈希与当前值一致」三项同时成立时批准冻结，并在任一项不成立时不冻结该校准模型、退回重新审批。
11. IF 校准参数表条目数不在 3 至 5 之间、任一条目缺少必需字段、多初值全部未收敛、或采纳解落在批准物理范围之外，THEN THE `calib.estimate` SHALL 不输出校准值与置信区间文件、报错指出不合规或未收敛的具体条目，并使数据集划分与既有冻结校准值保持不变。
12. IF Calibration、Validation 与 Test 三个分区中任一分区为空或其三元组数少于 `dataset_partition.min_triplets_per_partition`，THEN THE `calib.estimate` SHALL 报错拒绝执行、不输出校准值与置信区间文件、不以放宽独立 Test 要求的方式降级执行，并使数据集划分与既有冻结校准值保持不变。
13. WHERE `simulation_only` 为 `false`，THE `store` SHALL 使该任务 `tasks` 行的 `calibration_hash` 等于对 `configs/calibration.yaml` 与数据集划分记录按 design.md §5.3.1 的 `canonical_json` 规则序列化后取 sha256 所得的值；WHERE `simulation_only` 为 `true`，THE `store` SHALL 使该列为空串。

**追溯**：design.md §4.4 / §4.4.1 / §5.1 / §5.3.1 / §6.11 / §16 T15 / §17.1 Q9 / §17.2 L17-1、L22-1 ~ L22-6；V2.0 §5、§13.1、§14.3；原始需求文档 §3 任务4、§5 待确认事项第 3 项。

---

### Requirement 23: 条件能力——Dense Grid 参考扫描（M3）与鲁棒性扫描（M6）

**User Story:** 作为电源设计工程师，我希望看到二维响应面并让 Top 候选通过角点与容差验证，以便我能判断 Rcomp/Ccomp 如何影响瞬态与裕量，并区分「仿真域候选」与「工程推荐」。

#### Acceptance Criteria

1. WHEN 执行 M3 时，THE `reference.dense_grid_scan` SHALL 新建一条独立的 `task_kind='dense_grid'` 的 `tasks` 行落库，使其 `runs` 行与两级缓存键的构造规则与 `task_kind='optimize'` 任务相同，把该扫描产生的全部 `budget_units` 计入该 `dense_grid` 任务、不计入任何 `optimize` 任务，并使其 `budget.max_engine_starts` 与 `budget.max_wallclock_hours` 取自为该扫描单独审批的额度。
2. THE `reference.dense_grid_scan` SHALL 以 `constraints.yaml` 的 `design_space.variables.rcomp.ticks` 与 `design_space.variables.ccomp.ticks` 的笛卡尔积为扫描点集（各维 12 档、共 144 点），并以 `mode='grid'` 调用 `agent.validate` 校验该点集（只执行键集合、单位量纲、合法域与档位四项检查、跳过重复与新颖度），使 144 点全部判为 `accepted`、其 `rejected` 中不出现 `key_mismatch`、`unit_or_dimension`、`out_of_domain` 与 `off_tick` 原因；因该模式跳过新颖度检查，`design_space.novelty_min_ticks` 的取值不再受「必须不大于 1.0」的约束，该值可按探索需要自由冻结。
3. WHEN 扫描完成时，THE `reference.response_surface` SHALL 对 `metrics.yaml` 的 `objective.primary` 聚合值与 `phase_margin` 各输出一张以 rcomp 档位与 ccomp 档位为两轴的响应面图，把未取得有效指标的网格点标注为无效、不以插值或默认值填充，并返回非空的图文件路径列表。
4. IF 扫描点数与冻结场景集中 `tier='evaluation'` 行数之积（含 `require_margin` 为真的行所需的每候选额外启动数）大于为该扫描审批的 `budget.max_engine_starts`、或按 P-1 `single_run_s` 折算的墙钟估算大于该扫描审批的 `budget.max_wallclock_hours × 3600`，THEN THE PowerAgent 项目 SHALL 改执行 `reference.tiered_grid_scan`，其点集取 `design_space.variables.<name>.ticks` 的偶数索引子集（12 档取 6 档）的笛卡尔积（6×6 = 36 点，等比档位的偶数索引子集仍等比、比值为原比值的平方，因此 `tick_distance` 与响应面的对数等距性质均保持不变），且 THE `report` SHALL 把该结果标注为近似参考、使其结论用语只取自模板已定义的结论表述集合，该集合不含「全局最优」及等价表述。
5. THE PowerAgent 系统 SHALL 不基于 Dense Grid 建立 Regret 体系或搜索效率对照，并以 Requirement 17 的 AC11 所述 `tests/unit/test_report_wording.py` 的第 2 与第 3 条断言承载该约束的可观测形式（不出现 `regret` / `simulations-to-target` / `搜索效率` / `收敛速度`，且 `runs` 派生的过程量日志字段不进入结论段的渲染上下文键）。
6. WHEN 执行 M6 时，THE `robustness.sweep` SHALL 对 `eval.aggregate.rank`（`top_n=3`）输出的 Top 候选，在冻结场景集中 `tier='robustness'` 的批准电气角点行、`task.yaml` 的 `robustness.component_tolerance.*.relative_range` 上下端点组合，以及 M2 导出的 `calibration_uncertainty_for_robustness` 各项 `confidence_interval` 上下端点组合三个维度上逐条判定 `hard_constraints`，并把结果以 `tier='robustness'` 的 `runs` 行与 `constraint_results` 行落库。
7. WHERE `calibration_uncertainty` 为 `None`、其所指文件不存在、或其 `calibration_uncertainty_for_robustness` 节无条目，THE `robustness.sweep` SHALL 跳过校准参数置信区间维度、只在批准电气角点与器件容差两个维度上执行，且 THE `report` SHALL 标注被跳过的维度及其缺失原因（`simulation_only` 为 `true`，或 M2 未输出置信区间）。
8. IF 不存在可行候选，THEN THE `report` SHALL 输出可审计的结论，其最小内容为五项：各设计变量 `domain` 两端与 `ticks` 首末档位构成的合法域边界、逐条硬约束的 `value` 与 `threshold_source`、`scenario_set_hash` 与场景行摘要、取自 `rejections` 表的被拒候选按 `reason` 的计数、已用预算与上限。
9. WHERE Baseline Gate 曾通过且停止原因为 `no_feasible_region`，THE `report` SHALL 在该结论段首位列出合法域边界（各设计变量 `domain` 两端与 `ticks` 首末档位）作为怀疑对象、把 `hard_constraints` 逐条阈值列于其后，且不把硬约束阈值列在首位。
10. WHERE `simulation_only` 为 `false`，THE `report` SHALL 只把在 M6 已执行的全部维度组合上 `hard_constraints` 逐条均判为可行的 Top 候选标记为工程推荐，并把其余 Top 候选标记为仿真域候选、列出其被违反的硬约束与对应的维度组合。

**追溯**：design.md §6.7 / §6.10 / §6.12 / §6.13 / §11.1 / §12 / §13 / §16 T16、T25 / §17.1 Q8 / §17.2 L23-1 ~ L23-4；V2.0 §9.1、§6.3、§11.4、§12.2 验收 7。

---

### Requirement 24: 条件能力门禁——平均模型一致性与并行确定性

**User Story:** 作为模型 Owner，我希望平均模型与开关模型的一致性、以及并行执行的确定性都在启用前被验证，以便硬约束在平均模型上判定、主目标在开关模型上评价这一分工是可证明的。

#### Acceptance Criteria

1. WHERE `model.yaml` 的 `averaged_model_required` 为 `true`，THE `model.yaml` SHALL 同时绑定开关模型与平均模型的依赖闭包，并给出至少 1 条 `dual_model_consistency.checkpoints`，每条含须存在于 `task.yaml` 的 `scenarios` 中的 `scenario_id`，以及取值大于 0 的无量纲相对偏差上限 `steady_tol` 与 `transient_tol`；THE `dual_model_consistency` 节 SHALL 仅供 M-1/M0 的 Dual-Model Consistency 测试层与 `controller.preflight` 消费，运行期不复核。
2. WHERE `averaged_model_required` 为 `true`，IF `model_package.averaged` 节为空，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('averaged_model_missing')`。
3. WHERE `averaged_model_required` 为 `true`，IF 双模型一致性验证记录缺失、其 `ok` 为 `false`、或其绑定的 `model_package_hash` 与当前合并闭包的 `model_package_hash` 不一致，THEN THE `controller.preflight` SHALL 抛出 `PreflightError('dual_model_inconsistent')`、阻止进入 M1，且不启动任何仿真；该记录由 Dual-Model Consistency 测试层产生、由 `controller.preflight` 消费，`run_task()` 不产生该记录。
4. THE Dual-Model Consistency 测试层 SHALL 对 `dual_model_consistency.checkpoints` 的全部检查点（不抽样）以 `model.yaml` 的 `baseline.parameters_si` 为候选参数、在同一场景规范下分别执行 switching 与 averaged 变体，在 `metrics.yaml` 中 `output_ripple` 的稳态窗口内按两变体 Vout 稳态值计算稳态相对偏差、在 `overshoot` 与 `undershoot` 的阶跃瞬态窗口内按两变体 Vout 偏离量峰值计算瞬态相对偏差，在全部检查点两项偏差均不超过对应 `steady_tol` 与 `transient_tol` 时记录 `ok=true`、否则记录 `ok=false`，并在记录中写入逐检查点实测偏差与所绑定的 `model_package_hash`。
5. THE `run_task()` SHALL 在运行期不复核双模型一致性、不产生 `cause='dual_model_inconsistent'`，该一致性只由 Dual-Model Consistency 测试层按 AC4 验证并由 `controller.preflight` 按 AC3 消费；IF 该测试层判定偏差超出容差，THEN THE Dual-Model Consistency 测试层 SHALL 记录 `ok=false` 并在输出中给出触发的 `scenario_id` 与两项实测偏差，且 THE `controller.preflight` SHALL 据此阻断进入 M1。补运行期触发时机等于在运行期引入第二条采集路径、直接改变预算量级，而它要防的是模型与方法层面的系统性问题，开发期一次性验证即可发现，逐候选复核只是重复付费。
6. THE `store.cache` SHALL 在 `simulation_key` 中包含取值域为 `{switching, averaged}` 的 `model_variant`、不按模型变体拆分 `model_package_hash`，并使同一候选同一场景在两个模型变体上的 `simulation_key` 不相等。
7. WHERE `model.yaml` 的 `runtime.execution_mode` 为 `parsim`，THE PowerAgent 项目 SHALL 在启用前通过 Parallel Determinism 测试层在 Requirement 17 的 AC3 所用的同 3 个固定 fixture 候选参数与全部 `tier='evaluation'` 场景上对 `parsim` 与串行结果的比较（不另立独立候选清单），其通过条件为每个已激活指标满足 `|m_parsim − m_serial| ≤ compare_tolerance(m)` 且两种模式的 `constraint_results.feasible` 分类完全相同。
8. IF Parallel Determinism 测试的通过记录缺失、其判定为未通过、或其绑定的 `model_package_hash` 与 `execution_env_hash` 与当前值不一致，THEN THE `controller.preflight` SHALL 以 `execution_mode='serial'` 执行本任务、不以 `parsim` 启动任何仿真，并在启动输出中说明回退原因。
9. THE `run_task()` SHALL 在运行期不复核 `parsim` 与串行结果的一致性、不产生 `cause='parallel_nondeterminism'`，该一致性只由 Parallel Determinism 测试层在启用前按 AC7 验证；IF 该测试层检出任一已激活指标的差异超出 `compare_tolerance` 或其 `constraint_results.feasible` 分类不同，THEN THE Parallel Determinism 测试层 SHALL 判定未通过并输出不一致的 `metric_id` 与两种模式的取值，且 THE `controller.preflight` SHALL 按 AC8 以 `execution_mode='serial'` 执行本任务。理由同 AC5：运行期逐候选双跑会直接改变预算量级，而并行不确定性属方法层面的系统性问题，启用前一次性验证即可发现。

**追溯**：design.md §4.2 / §5.3 / §6.2.2 / §8.5 / §10 CP-12 / §11.1 / §12 / §16 T13、T26 / §17.1 Q5 / §17.2 L24-1 ~ L24-3；V2.0 §3.2、§3.3、§6.2、§10.2、§12.1、§12.2 附加项。

---

## 与 design.md 决策记录的对应关系

design.md §17 已由「待确认事项」改为「决策记录」：§17.1 是原 Q1 ~ Q12 的已确认登记，§17.2 是 78 条 L 编号的裁定登记。下表给出 Q1 ~ Q12 的已确认裁定与本文档验收标准的对应关系。**若某项裁定被再次修正，须同步更新对应需求的验收标准。** 本文档不重新讨论这些问题。

| §17.1 编号 | 已确认裁定要点 | 受影响的需求与验收标准 |
| --- | --- | --- |
| Q1 | `sensitivity` 保留在次目标序列末位但默认不激活，不参与排序 | Requirement 1.4；Requirement 9.8 |
| Q2 | 本轮改判：删除 `constraints.yaml` 的 `model_variant` 字段，硬约束改按 `observable` + `sense` 判定，模型变体由场景行与观测量可得性决定 | Requirement 9.1、9.2、9.10、9.13 |
| Q3 | 裕量链路拆「采集计预算 / 计算不计预算」两段 | Requirement 8.4、8.5、8.11；Requirement 15.7 |
| Q4 | `stop.stop_on_first_feasible` 默认 `false`；首次可行解轮次仅为日志字段；另提供 `objective_target` | Requirement 16.8、16.9；Requirement 21.7 |
| Q5 | `simulation_key` 增加 `model_variant` 字段，不拆分 `model_package_hash` | Requirement 15.1；Requirement 24.6 |
| Q6 | CI 只跑不依赖 MATLAB 的 Schema/Unit 与两个 Metric Fixture 层，fixture 波形作为固定二进制资产入库 | Requirement 17.6、17.7 |
| Q7 | 崩溃重启时保留 `running` 行的 `budget_units`，预算宁可多计不可少计 | Requirement 16.12、16.14 |
| Q8 | Dense Grid 以独立 `task_kind='dense_grid'` 落库、预算独立审批；本轮扩展至全部非 `optimize` 的 `task_kind`（含 `baseline_gate`） | Requirement 23.1；Requirement 10.2 |
| Q9 | `calib.estimate()` 多初值局部最小二乘（`least_squares`，`multi_start=5`，Latin Hypercube 初值） | Requirement 22.4 |
| Q10 | 新增 `artifacts/<task_id>/llm/` 存 prompt 与输出原文 | Requirement 11.12；Requirement 14.8 |
| Q11 | schema 校验失败回灌重试上限落在 `budget.max_llm_repair_rounds`，默认 2、只计 token；本轮补上界 `0 <= v <= 5` | Requirement 11.7 |
| Q12 | `efficiency` 默认不激活，保留在次目标序列中 | Requirement 1.3 |

L5-1 ~ L24-3 共 78 条遗留项的裁定登记见 design.md §17.2（按 R5 ~ R24 分组，每条给出裁定、判据与落点）。本文档的验收标准已按这些裁定同步；各需求末尾的「追溯」行标注了对应的 L 编号。两份文档冲突时以 design.md 为准。

需求方的书面确认项（V2.0 §1.3、design.md §17.4）不属于实现问题，列在下一节末尾。

---

## 仍需 M0 填写的物理数值

**这些不是未决策项。** 机制、字段、口径与控制流已在 design.md 中定完（裁定登记见 design.md §17.2），本节只列仍等 Owner 填值的物理数值。未填即 `controller.preflight` 抛 `PreflightError('config_incomplete: <字段路径>')`，`config` 不推断默认物理值（Requirement 2 的 AC4）。

按文件与 Owner 分列，与 design.md §17.3 同步：

| 文件 | 待填字段 | Owner | 来源 |
| --- | --- | --- | --- |
| `task.yaml` | `objective_target.target_value`、`budget.max_engine_starts`、`budget.max_wallclock_hours`、`stop.no_improvement_rounds`、场景行的 `vin_v` / `temp_c` / `load_start_a` / `load_end_a` / `slew_a_per_us`、`robustness.component_tolerance.*.relative_range` | 使用者 | 任务定义 |
| `model.yaml` | `io_contract.solver.{max_step, rel_tol, stop_time}`、`io_contract.vout_target_v`、`io_contract.divergence_guard.{vout_abs_max, iphase_abs_max}`、`runtime.max_wallclock_per_run_s`、`averaged_model_required`、`baseline.parameters_si`、`baseline.measured_evidence_ref`、`dual_model_consistency.checkpoints[].{steady_tol, transient_tol}` | 模型 Owner | P-1 / P-2 探针实测 + 电源设计 |
| `metrics.yaml` | 各指标的 `compare_tolerance.{absolute, relative}`、`output_ripple.filter`（带宽）、`margin_extraction.{primary_method, cross_check_method, cross_check_tolerance, cross_check_record.sha256, extra_engine_starts_per_candidate}`、`objective.tie_tolerance` | 指标 Owner | M1 出口冻结（容差可在 M1 出口前更新一次，见 Requirement 3 的 AC8） |
| `constraints.yaml` | `hard_constraints.*.value`（4 条）、`design_space.variables.*.{domain, ticks}`、`design_space.novelty_min_ticks`、`design_space.device_limits.*.{value, source}` | 电源设计 | M0 出口冻结，变更需重新审批 |

已给定默认值、可直接使用而无需 M0 填写的字段：`constraints.yaml` 的 `design_space.tick_match_rel_tol`（`1e-6`）、`task.yaml` 的 `llm.timeout_s`（120）与 `llm.max_network_retries`（2）、`task.yaml` 的 `budget.max_attempts_per_scenario`（2）、`configs/calibration.yaml` 的 `confidence_level`（0.95）、`dataset_partition.{min_triplets_per_partition, test_min_condition_groups}`（2 与 2）与 `multi_start.spread_warn_rel`（0.10）。

三项填值上的注意事项：

- `model.yaml` 的 `runtime.max_wallclock_per_run_s` 推荐取 `ceil(3 × probe.single_run_s)`（Requirement 2 的 AC1）；`io_contract.divergence_guard` 的两项推荐取对应硬约束的 2 倍，且必须不低于对应硬约束值，否则 `config` 按 Requirement 3 的 AC14 报错。
- `design_space.variables.*.ticks` 必须等比，且 `tick_match_rel_tol` 须严格小于 `0.01 × (tick_ratio − 1)`，由 `config.schema` 按 Requirement 3 的 AC15 断言。
- `design_space.novelty_min_ticks` 可按探索需要自由冻结，不再受「必须不大于 1.0」的约束（Dense Grid 的 144 点走 `validate(mode='grid')`，跳过新颖度检查，见 Requirement 23 的 AC2）。

另保留 V2.0 §1.3 的需求方书面确认项（非实现问题，design.md §17.4）：若 P-3 判定进入 PoC 轨，阶段1 对需求首要目标「使仿真结果尽可能逼近实际测试结果」为**零进展**。该确认必须在 M0 之前完成，`task.yaml` 的 `simulation_only` 值即该决策的机器化形式。对应 Requirement 1.5、1.8 与 Requirement 22.8。
