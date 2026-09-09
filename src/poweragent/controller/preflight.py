"""poweragent/controller/preflight.py

`preflight()` 与其断言函数（`design.md` §6.2.2；`tasks.md` 任务 10.5 起）。

## 本文件的分波排期

`design.md` §6.2.2 给出的 `preflight()` 完整接口共七个 `check_*` 函数：
`check_config_completeness` / `check_freeze_consistency` /
`check_model_package_hash` / `check_dual_model_consistency` /
`check_margin_extraction_ready` / `check_budget_feasibility` /
`check_baseline_gate`；此外 §6.2.2 文档化的断言清单还含
`metric_not_implemented` / `observable_unbound` / `unit_mismatch` 三项，均由
各自的 `check_*` 函数产生。`tasks.md` 明确按六波派发本文件：

    10.5 -> metric_not_implemented（本文件初始骨架）
    11.4 -> phase_margin_must_be_active / margin_cross_check_missing /
            margin_cross_check_mismatch / margin 与 averaged_model_required
            的联动断言（`check_margin_extraction_ready()`）
    12.6 -> observable_unbound / unit_mismatch（本次新增：
            `check_observable_unbound()` / `check_unit_mismatch()`）
    14.2 -> `check_config_completeness()`（`task.yaml` 无 `tier='evaluation'`
            行一条）/ `check_freeze_consistency()`（三个冻结点中的两个：
            `kind='model_package'` 与 `kind='safety'`）/
            `check_model_package_hash()` / `check_dual_model_consistency()` /
            `check_budget_feasibility()`（墙钟可行性估算，寻优任务口径，
            `estimated_wallclock = probe.single_run_s × budget.
            max_engine_starts`）

    本次（14.2）落地对 `check_model_package_hash()` 与 `check_freeze_consistency()`
    的边界划分：`design.md` §6.2.2 把两者列为**两个独立函数**
    （`check_freeze_consistency(cfgs, store) -> None` /
    `check_model_package_hash(model_cfg, store) -> str`——注意后者标注的返回类型
    是 `str`，不是 `None`），因此本次**不把两者合并**：

    - `check_model_package_hash(model_cfg, store) -> str`：只做「重新解析依赖
      闭包、重算 `model_package_hash`」这一件计算，返回重算出的哈希字符串
      （字面签名的返回类型 `str` 正对应这个「算出来的值」，不是一个纯布尔式
      断言）。它不读 `freezes` 表，不知道「冻结点」这个概念，可以被其他调用方
      单独复用（例如 16.1 的 `check_baseline_gate()` 若需要同一个哈希，不必
      重新解析闭包）。
    - `check_freeze_consistency(constraints_cfg, metrics_cfg, model_cfg, store,
      task_cfg) -> None`：负责「读 `freezes` 表两行、和当前配置计算值比较、
      不一致就 raise」这件比对逻辑，对 `kind='model_package'` 复用
      `check_model_package_hash()` 算出的重算值，自己不重复实现闭包解析。
      `kind='safety'` 的重算值（`H(constraints_hash, metrics_hash,
      scenario_set_hash)`）没有对应的独立 `check_*` 函数——`design.md` §6.2.2
      的七个函数列表里没有为它单列一个函数，因此该重算逻辑内聚在
      `check_freeze_consistency()` 内部，不额外拆分。

    这与 `check_margin_extraction_ready()`（任务 11.4）"签名比 design.md 字面
    给的更宽"是同一处理原则的另一面：那里是「读文档字面签名字数不够、需要加
    形参」，这里是「文档字面已经给出两个独立函数，尊重这个划分、不因为看起来
    有重叠就擅自合并」。

    进一步细化——`check_model_package_hash()` 究竟"检查"什么：既然名字带
    `check_` 前缀（本文件其余全部 `check_*` 函数命中失败即 `raise`），本函数
    也执行真正的比对：重新解析依赖闭包、重算 `model_package_hash`，与
    `store.find_freeze('model_package')` 的既有记录比较，不一致（含缺失该
    冻结行）即 `raise PreflightError`；返回值（`str`，对应 design.md 字面
    签名的返回类型）是这次重算出的哈希，供 `check_dual_model_consistency()`
    复用（`dual_model_inconsistent` 的第三个判据要求拿"当前合并闭包的
    `model_package_hash`"与一致性记录里绑定的哈希比较，复用同一次重算避免
    重复解析闭包）。`check_freeze_consistency()` 对 `kind='model_package'`
    这一半**委托**给 `check_model_package_hash()`（自己不重新实现闭包解析），
    对 `kind='safety'` 这一半自己独立计算并比较——两个 `kind` 没有共用逻辑
    可提炼，分别处理是最简单的实现。

    `base_dir` 形参（`check_model_package_hash()` / `check_freeze_consistency()`
    /`check_dual_model_consistency()` 均新增，后两者只是原样转发给
    `check_model_package_hash()`）：`design.md` §6.2.2 的字面签名没有这个参数，但
    `sim.hashing.resolve_dependency_closure()`（任务 3.1）与
    `sim.simulate()`（任务 7.2）已经确立"接受一个默认值为 `"."` 的 `base_dir`
    kwonly 形参、用于把 `model.yaml` 里的相对路径解析为绝对路径"这一惯例
    （`sim/simulate.py` 模块 docstring 第 4 条）；本文件需要对同一个
    `model_cfg` 做同样的闭包解析，遵循同一惯例，不发明新的路径解析方式。

    ## 任务 14.2：`check_dual_model_consistency()` 的一致性记录读取契约

    `requirements.md` R24.1 / R24.3 与 `design.md` §8.5 都明确"双模型一致性
    验证记录由 Dual-Model Consistency 测试层产生、由 `controller.preflight`
    消费，`run_task()` 不产生该记录"——这条测试层是`tasks.md` 任务 4.2，标记
    `[需 MATLAB]`，本次（14.2）环境不可用 MATLAB/Simulink（许可证错误
    -4,132），该测试层未落地。这与 `check_margin_extraction_ready()`
    （任务 11.4）消费 `cross_check_record`（`margin_cross_check_once` 产生，
    同样是开发期一次性、非本次任务范围）是同一种"软依赖"模式（参见
    `sim/simulate.py` 模块顶部"与 `matlab/+pa/run_linear_analysis.m` 的软
    依赖"一节、`cli.py` 模块顶部"软依赖"一节的行文方式）：本函数按
    `design.md` §8.5 与 R24.1 已经锁定的记录语义（存在与否、`ok` 布尔、绑定的
    `model_package_hash`）定义一个**读取接口契约**，不等测试层落地即可实现
    并单测（用手写的 JSON 文件模拟测试层的输出）。

    契约选择：**JSON 文件**，路径默认 `artifacts/dual_model_consistency.json`
    （通过 `record_path` kwonly 形参可覆盖，供测试传入临时路径）。选择与
    `margin_extraction.cross_check_record` 同构的落点风格——`design.md` §4.3
    已把 `cross_check_record` 定为"模型与提取方法的开发期属性，不按 `task_id`
    分目录、同一模型包下的全部任务共用一份"（L8-1），双模型一致性记录是
    同一性质的开发期属性（M-1/M0 阶段产生一次，`run_task()` 运行期只读不写），
    因此采用同一落点惯例：`artifacts/` 顶层、不按 `task_id` 分目录。这与
    `artifacts/margin_cross_check.json` 并列，都不经过 `config/schema.py` 的
    `CrossCheckRecord`（`model.yaml` 的 `dual_model_consistency` 节本身只有
    `checkpoints` 列表，没有类似 `cross_check_record` 的 `{path, sha256}`
    字段——`config/schema.py` 的 `DualModelConsistency` 模型未定义该字段，本
    函数也不越权给它加字段，这属于 `config/schema.py` 的改动范围，不在本
    任务）。

    JSON 记录的最小形状（本函数只读取这两个键，测试层落地后若记录含更多字段
    如逐检查点偏差，本函数忽略、不校验）：

    ```json
    {"ok": true, "model_package_hash": "<hex>"}
    ```

    文件不存在、内容不是合法 JSON、或 `ok` 缺失/为 `false` 均视为
    "记录缺失或未通过"（R24.3 三个判据里的前两个：记录缺失、`ok=false`）；
    `model_package_hash` 与当前重算值不一致对应第三个判据。三者共用同一个
    错误标识 `dual_model_inconsistent`——`requirements.md` R24.3 原文把三个
    判据并列在同一个 `IF` 分支里、共用同一个 `PreflightError`，不要求区分
    消息文本。
    16.1 -> `check_baseline_gate()`（Baseline Gate 自身的墙钟口径
            `est_gate = probe.single_run_s × units_of(evaluation_rows)`
            是独立的一条断言，与本文件 14.2 的 `check_budget_feasibility()`
            不是同一条、不复用同一份 `estimated_wallclock`）
    31.2 -> `check_baseline_gate()` 的 `parsim` 回退分支消费；顶层
            `preflight()` 编排函数不在本文件已排期的六波中单独出现，留给
            `run_task()` 接线时按需补上，本次（14.2）不提前搭建
    19.1 -> `check_task_set_freeze_consistency()`（`kind='task_set'` 的告警式
            一致性校验，M5 起生效，见该函数自身文档；`Milestone` Literal +
            `_MILESTONE_ORDER` 元组同批新增，供"M5 或之后"的顺序比较使用）

（上一版本文档在此处把 `check_budget_feasibility` 误记在 16.1、`16.1` 误记为
`check_budget_feasibility` 而非 `check_baseline_gate`——已按 `tasks.md` 实际的
六波派发表更正为以上内容。）

## 任务 16.1：`check_baseline_gate()` 的范围收窄、签名扩展与设计决策

`design.md` §6.2.2 给出的字面签名是 `check_baseline_gate(model_cfg: ModelConfig,
store: Store) -> BaselineGateStatus`；但字面签名描述的是"这个函数做什么"（对
`design.md` §8.2 步骤 7 的伪代码而言，`check_baseline_gate()` 只是"执行一次
Baseline 候选的 Evaluation 集评价并返回结果"这一段），伪代码里
`gate_key` 的计算、`store.find_baseline()` 缓存查询、门禁自身墙钟断言、
`store.freeze_baseline()` 的调用与最终的 `RAISE PreflightError` 全部由**顶层
`preflight()` 编排函数**完成，不在 `check_baseline_gate()` 内部。

但本文件截至本次（16.1）**没有任何顶层 `preflight()` 函数**（模块 docstring
"本文件的分波排期"一节已经说明：`run_task()` 目前按 design.md §8.2 的步骤顺序
自己逐个调用九个 `check_*` 函数，没有一个统一的 `preflight()` 入口）。这与
`check_freeze_consistency()`（任务 14.2）把"读 `freezes` 表两行、和当前配置
计算值比较"这类字面上属于 §8.2 步骤 2 的比对逻辑整个收进函数体内、
`check_dual_model_consistency()` 把"读取一致性记录、判定三条件"整个收进
函数体内是**同一种既定处理方式**：本文件的 `check_*` 函数在这个代码库里
承担的范围，本就比 `design.md` 伪代码字面上分配给"同名 `check_*` 调用点"的
范围更宽——因为没有顶层 `preflight()` 去接住伪代码里那些"归属不明确、不属于
任何具名函数"的编排代码。`check_baseline_gate()` 沿用这一先例：本函数把
§8.2 步骤 7 里*除*"抛出最终的 `RAISE PreflightError('baseline_infeasible: ' +
gate.diagnosis_ref)`"这一行**归还给最外层调用方**之外的全部逻辑——`gate_key`
计算、缓存查询、门禁墙钟断言、门禁执行、`freeze_baseline` 写入——都收进本
函数体内。「归还最外层调用方」这一行也不完全归还：本函数在 FAIL 路径上**自己
raise** `PreflightError`（与其余全部 `check_*` 函数一致，不返回一个"失败态"
的 `BaselineGateStatus` 让调用方自己决定是否 raise），只有 PASS 路径才把
"是否要进一步冻结 `passed=True` 的前值基线"这一步留给调用方（见下方"PASS
路径的移交边界"一节）。

### 1. `BaselineGateStatus`：本文件自己的类型，与 `store.repo.BaselineGateStatus`
同名不同形，刻意不复用

`store/repo.py` 已经定义了一个同名的 `BaselineGateStatus`（`find_baseline()`
的返回类型），其自身文档已经写明"那是另一个类型"："`design.md` §6.2.2
另有一个同名但字段不同的 `BaselineGateStatus`（含 `evidence_bound: bool`），
那是 `controller.preflight.check_baseline_gate()` 的返回类型——`evidence_bound`
取自 `model.yaml` 的 `measured_evidence_ref`，不是 `baselines` 表的列"。本
函数因此在本文件内定义自己的 `BaselineGateStatus`（`design.md` §6.2.2 字面
给出的四字段 `passed` / `frozen_result_hash` / `evidence_bound` /
`diagnosis_ref`，本文件补一个 `gate_key: str` 字段——调用方需要这个键去在
`store.find_baseline()`/`store.freeze_baseline()` 之间传递，`design.md`
字面签名没有它是因为伪代码里 `gate_key` 是一个局部变量、不是这个 dataclass
的字段；本文件把它塞进返回值，让 `check_baseline_gate()` 的调用方不需要
在外部重新计算一遍同一个 `gate_key`）。两个同名类型之间没有继承或转换关系，
调用方（未来的 `run_task()` 接线代码）需要自己分辨从 `store.find_baseline()`
拿到的是哪一个（`store.repo.BaselineGateStatus`）、从
`controller.preflight.check_baseline_gate()` 拿到的是哪一个（本文件的
`BaselineGateStatus`）——这不是一处需要消除的重复，是 `store/repo.py`
自己的文档已经承认并接受的设计。

### 2. `evidence_bound` 的取值：`model_cfg.baseline.measured_evidence_ref
is not None`，与 `passed` 无关，两个分支都要算

`evidence_bound` 描述"`model.yaml` 是否绑定了实测记录"，这是 `model_cfg`
本身的静态属性，与本次门禁执行的结果（PASS 还是 FAIL）无关（`requirements.md`
R10.4：`measured_evidence_ref` 非空/为空分别决定 `diagnosis_ref` 指向文件里
的表述基调，但 `evidence_bound` 这个布尔值本身在两种结果下都要给出）。本函数
因此在缓存命中、PASS、FAIL 三条路径的返回值/异常处理里都先计算好这个值，不
只在某一条路径上计算。

### 3. 范围边界：R10.4/R10.8 的"三类固定顺序排查结论"写入不在本任务内

`requirements.md` R10.4（"排查结论按 `measured_evidence_ref` 是否非空调整表述
基调"）与 R10.8（"约束阈值/单位量纲 → 模型结构或参数 → 指标窗口/带宽/无效
条件/提取实现，三类固定顺序，每类写一条命中或已排除的结论"）都要求
`diagnosis_ref` 指向的文件含有相当具体的排查分析内容——但本任务（16.1）的
需求追溯只列了 R2.15, R10.2, R10.3, R10.6, R10.10, R10.11，**不含** R10.4
与 R10.8。本函数在 FAIL 路径上仍然写入一个非空、指向文件的 `diagnosis_ref`
（满足 R10.3"写入 `baselines` 行 `passed=0` 且 `diagnosis_ref` 非空"这一条
结构性要求），文件内容只包含"哪个场景行、哪一类判据（未 `done`/不可行/
指标无效）触发了这次失败"的一句事实陈述，**不**包含 R10.8 要求的三类固定
顺序排查（约束阈值 → 模型结构 → 指标提取，逐类下结论）——那需要人工或另一
个专门函数去逐类核对并下"命中"或"已排除"的结论，是一项超出"判定 PASS/FAIL"
范围的独立分析工作，留给需求追溯明确包含 R10.4/R10.8 的后续任务补齐（若不
存在这样的任务，需要向任务规划者标记这一缺口）。这与 `evidence_bound` 仍然
按 R10.4 的字面定义计算并携带在返回值里并不矛盾：计算一个布尔值是本函数
力所能及的，撰写一份三类排查报告不是。

### 4. `task_kind='optimize'` 门控：由调用方负责，本函数不接受也不检查
`task_kind`

`requirements.md` R10.2 原文"WHERE `task_kind='optimize'`，THE
`controller.preflight` SHALL 执行 Baseline Gate"——这是对"什么时候调用
本函数"的约束，不是"本函数内部要做的判断"。`task_kind` 是 `ddl.sql` 的
`tasks` 表列（`optimize | dense_grid | robustness | baseline_gate`），不是
`config.schema.TaskConfig` 的字段（`task.yaml` 里没有这个键）——`run_task.py`
自己的模块 docstring（"4. `check_baseline_gate()` 的缺口"一节）已经核实过
这一点并据此在当前实现里固定按 `task_kind='optimize'` 落库、跳过对本函数的
调用。本函数因此不接受 `task_kind` 参数、不做"如果不是 optimize 就跳过"的
内部判断——它假定"调用它"这件事本身就意味着"当前正在执行的是一个即将进入
Optimize 主循环的任务，且调用方已经决定现在需要跑一次门禁"，这个决策权
（读 `tasks` 表已有行的 `task_kind`，或读取即将写入的 `task_kind` 字面量）
留在调用方——未来 `run_task()` 接线该函数时，应在自己已知"本次
`create_task(task_kind=...)` 将写入的取值是否为 `'optimize'`"的那个位置
加一层 `if`，而不是把这个判断权转移进本函数（本函数没有能力独立判断这件事：
它不接收、也不应该接收调用方尚未决定好的 `task_kind` 字符串）。

### 5. `run_tier()` 复用决策：功能上干净，但与 `controller/run_task.py`
之间存在真实的循环 import 障碍——用函数体内的延迟 import 解决

`run_tier()`（`controller/run_task.py`，任务 14.4）已经完整实现"单候选、
单 tier 的场景行分层执行：两级缓存查找 + 提前拒绝 + 指标/约束判定 +
`runs`/`metric_results`/`constraint_results` 落库"，逐场景处理 Evaluation
层且遇到单场景约束不可行**不**提前终止整层（"继续跑完该层其余场景行"，
design.md §8.4 R6.7）——这正是 Baseline Gate 需要的行为：Baseline 候选要
"在全部 `tier='evaluation'` 显式场景行上执行"，不能因为某一行不可行就提前
放弃剩余场景（那样反而会丢失"到底几行不可行、具体是哪几行"这类诊断信息）。
调用 `run_tier(candidate, 'evaluation', evaluation_rows, ...)` 一次、把
`scenario_rows` 参数传入全部 Evaluation 行，是最直接的复用路径，本函数
采用这个路径。

但 `controller/run_task.py` 在其模块顶层就 `from poweragent.controller.preflight
import (check_budget_feasibility, check_config_completeness, ...)`——如果
本文件（`controller/preflight.py`）在**模块顶层**反过来 `from
poweragent.controller.run_task import run_tier`，两个模块会形成循环
import：无论哪个模块先被 import，另一个模块在被 import 到"反向 import 那
一行"时，会拿到一个尚未执行完 `import` 语句、因而残缺的模块对象，触发
`ImportError`（"partially initialized module"一类的运行期错误）。这是一个
真实存在的接线摩擦，不是可以简单忽略的细节。

解决方式：把 `from poweragent.controller.run_task import run_tier` 这一行
**移入 `check_baseline_gate()` 函数体内部**（延迟 import，只在函数被真正
调用时才执行，不在模块加载时执行）。这是 Python 里打破循环 import 的标准
手法之一：函数体内的 import 语句直到函数被调用的那一刻才执行，而调用发生
的时点必然在整个程序完成一次完整的模块加载序列之后（不论 `controller.
preflight` 与 `controller.run_task` 谁先被 import，等到某处代码真正调用
`check_baseline_gate(...)` 时，两个模块都早已完整加载完毕），因此不会重新
触发"循环 import"问题。这是一个**显式记录的接线摩擦**（"clean reuse but
real import friction"），不是本函数悄悄绕过的隐藏问题——若未来
`controller/run_task.py` 或 `controller/preflight.py` 的模块级 import
结构发生变化（例如两者其中一个不再依赖另一个），这处延迟 import 可以恢复
为普通的模块顶层 import，但目前的依赖方向要求它保持延迟。

### 6. 场景行类型转换：不导入 `run_task.py` 的私有 `_to_repo_scenario()`，
在本文件内重新写一份等价的最小转换函数

`run_tier()` 要求的 `scenario_rows: Sequence[store.repo.ScenarioSpec]`
是跨模块契约类型；而 `controller.scenario.evaluation_rows()`（本文件已在
14.2 阶段 import）返回的是该模块自己的本地 `ScenarioSpec`（`controller/
scenario.py` 顶部文档已承认的"临时应对"，字段形状相同、`spec_version`
额外持有）。`controller/run_task.py` 已经为同一个转换写过一个私有函数
`_to_repo_scenario()`（下划线前缀，未出现在其 `__all__` 导出列表中）。本
文件**不**导入那个私有函数（跨模块引用另一个模块的私有名称违反该下划线
前缀本身传达的"仅供本模块内部使用"约定），而是在本文件内重新写一份内容
等价、字段逐一对应的最小转换函数 `_to_repo_scenario_spec()`——这是对同一段
≈15 行纯字段搬运逻辑的一次刻意重复（不是可提炼的公共逻辑：提炼需要新建一个
两个模块都能安全 import 的第三处位置，例如把它挪进 `controller/scenario.py`
自身，但那属于对已完成任务的返工，不在本任务范围内），比引入额外的延迟
import 或者修改另一个模块的导出表更简单。

### 7. PASS / FAIL 判定：不能只看 `TierResult.passed`，必须直接查
`runs` / `constraint_results` / `metric_results` 三表

`TierResult.passed`（`run_tier()` 的返回值字段）对 Evaluation 层的语义是
"该 tier 是否在未提前中止的情况下跑完"——它的文档明确说明"约束不可行
（`feasible=False`）本身不会使 Evaluation 层的 `passed` 变为 `False`"
（`run_tier()` 对 Evaluation 层的既定行为：单场景不可行只记录、继续跑完
剩余场景）。但 `requirements.md` R10.3 要求的判据是"任一行不满足
`runs.status='done'`，或任一行 `constraint_results.feasible=0`，或任一
激活指标 `metric_results.valid=0`"——这恰好覆盖了 `TierResult.passed=True`
时仍可能存在的"某一行不可行"这类情形。因此本函数在 `run_tier()` 返回之后，
**不管 `TierResult.passed` 是 `True` 还是 `False`**，都对
`evaluation_rows(task_cfg)` 的每一个 `scenario_id` 逐一查询该 Baseline
候选（`origin='baseline'` 候选的 `candidate_id`）在本次 `baseline_gate`
`task_id` 下对应的 `runs` 行（取 `status`）、`constraint_results.feasible`、
以及 `metrics_cfg.active_metrics` 每一项在 `metric_results.valid` 的取值
——命中第一个违规场景即停止并记为诊断依据（fail-fast，与本文件其余
`check_*` 函数一致）。这些查询没有对应的 `Store` 专用方法（`store/repo.py`
的既定范围原则是"不提供判定可行性的代码路径"，这里的查询本身不判定
可行性、只是读取既有列的原始值，符合该原则），因此直接经
`store.connection.execute(...)` 发起只读 SQL——`store.connection` 属性
已经是 `Store` 自己文档化的"最小暴露"桥接点，`run_task.py` 也已经在
（`already_frozen` 判断）用同样的方式直接查询 `scenario_set` 表，本函数
沿用同一个既定用法，不新增 `Store` 方法。

`run_tier()` 提前中止（例如某场景 `engine_transient`/`diverged`/
`solver_error`/`timeout`）导致后续场景**完全没有对应的 `runs` 行**这一
情形，天然落在"该 `scenario_id` 查不到任何 `runs` 行"这一支里，与"存在
`runs` 行但 `status != 'done'`"归入同一个 FAIL 分支处理，不需要单独分支。

本函数不在 Baseline Gate 内部实现 `engine_transient` 的重试循环（
`run_task.py` 的 `_run_scenarios_with_retry()` 是私有函数、且服务于寻优
主循环的"跨场景独立 `attempt` 计数"编排，Baseline Gate 是一次性的单趟
判定，design.md §8.2/§13 给 Baseline Gate 的墙钟预算公式
`units_of(evaluation_rows)` 本身也没有为重试预留额外倍数）——单次尝试中
若有任何场景命中 `engine_transient`，该次门禁判定直接失败（`runs.status
!= 'done'`），这是一个已记录的已知限制：真实瞬态故障会被门禁误判为
"Baseline 不可行"，且由于 `gate_key` 缓存机制，这次误判会一直生效直到
四个哈希之一变化才会被迫重跑。这与 R10.3"不以部分场景结果或告警方式放行"
的字面要求一致（不允许因为"这次只是瞬态故障"就悄悄放行或自动重跑），若
需要为瞬态故障提供重试，应作为后续任务的显式扩展，不在本任务内添加。

### 8. `diagnosis_ref`：写入一个非空、指向文件的最小事实陈述，不实现
R10.4/R10.8 的完整排查报告（见第 3 节）

FAIL 路径下，本函数把触发失败的第一个场景/判据组装成一句纯事实陈述（例如
"scenario_id=eval_01: run_id=... status='failed' (expected 'done')"），经
`artifacts.stage()`/`artifacts.commit()`（`ArtifactStore`，任务 8.2，已
落地）写入 `artifacts/<gate_task_id>/baseline_gate/diagnosis.txt`，取
`commit()` 返回的产物引用字符串作为 `diagnosis_ref`。选择复用
`ArtifactStore` 而不是裸 `Path.write_text()`：`commit()` 自带往返校验
（写入后立即读回比对哈希），与本文件其余产物落盘路径（`sim/simulate.py`
的波形产物）走的是同一套"原子写入 + 校验"机制，不为诊断文件发明第二套
落盘方式。

### 9. `gate_task_id`：显式可选参数，未传入时按 `uuid.uuid4()` 生成

`design.md` 未规定 Baseline Gate 的 `task_id` 由谁生成或采用什么格式（
`tasks.task_id` 列只有 `TEXT PRIMARY KEY` 约束，无格式要求；寻优任务的
`task_id` 取自 `task_cfg.task_id`，但 Baseline Gate 是一个独立的
`task_kind='baseline_gate'` 行，不应该复用寻优任务的 `task_id`——两者是
两条不同的 `tasks` 行，共享同一个主键值会撞车）。本函数把 `gate_task_id`
做成一个可选 kwonly 参数（默认 `None`）：传入时使用调用方给定的确定性
值（便于测试与"同一个 `task_cfg.task_id` 每次生成同名门禁任务 ID，方便
人工核对"这类未来场景）；不传入时按 `f"baseline_gate_{uuid.uuid4().hex}"`
生成一个新的随机 ID——与本文件其余"调用方未指定时按既定规则生成"参数
（例如 `check_dual_model_consistency()` 的 `record_path` 默认值）同一
处理原则：给出一个开箱可用的默认行为，同时允许调用方覆盖。

### 10. 门禁自身的 `BudgetLedger`：与寻优任务完全独立的一个新实例，
`task_id` 不同即天然不会互相计入

`controller.budget.BudgetLedger.used()`/`remaining()` 都按
`self.task_id` 过滤 `SUM(runs.budget_units WHERE task_id=?)`（见
`controller/budget.py`）——只要本函数为 Baseline Gate 构造一个绑定
`gate_task_id`（而不是寻优任务的 `task_id`）的独立 `BudgetLedger` 实例，
`run_tier()` 内部对这个 ledger 调用的 `reserve()` 产生的全部
`budget_units` 天然只计入 `gate_task_id` 对应的 `runs` 行，不会被任何
`optimize` 任务的 `BudgetLedger.used()` 查询到（那个查询按不同的
`task_id` 过滤）。这不需要额外的隔离机制——"预算独立审批、不计入寻优
任务"这条要求，由"两个 `BudgetLedger` 实例绑定不同 `task_id`、`Store`
的求和查询按 `task_id` 过滤"这一既有机制结构性保证，本函数只需要
"构造一个新的 `BudgetLedger(store, gate_task_id, gate_budget_max_
engine_starts, gate_budget_max_wallclock_hours × 3600.0)`"，不需要
额外校验或断言这条隔离性质。

### 11. PASS 路径的移交边界（任务 16.1 落地时的状态；已由任务 16.2 闭合，
见下方「任务 16.2」一节）

任务 16.1 落地时，本函数在 PASS 路径上只 `return BaselineGateStatus(
passed=True, frozen_result_hash=None, ...)`，不调用 `freeze_baseline()`
也不 `finish_task()`——把"门禁通过时冻结前值基线"整段留给任务 16.2。这段
历史记录保留在此，作为任务 16.2 补齐内容的对照；任务 16.2 的实现细节见
下方独立一节，不在此重复。

## 任务 16.2：`check_baseline_gate()` PASS 路径的冻结与收尾

### a. `frozen_result_hash` 的计算：复用 `config.hashing.result_hash()`
的既定 9 键契约，不发明新的哈希公式

`design.md` §5.3.1 已经把 `result_hash` 的重算范围锁定为 9 键（
`candidate_id` / `parameters_si` / `worst_case` / `per_scenario` /
`constraints` / 四个哈希），`config/hashing.py` 的 `result_hash()`
（任务 5.3）是这份公式唯一的实现登记处——`requirements.md` R10.5 要求
"冻结为交付物5 的寻优前值基线"用的正是同一份"候选在 Evaluation 集上的
激活指标结果、硬约束判定结果与 worst-case 聚合值"，与 `apply()` 前重算
比对 `approvals.result_hash` 所用的是**同一个函数、同一套字段范围**——
两者绑定的对象不同（一个是 Baseline 候选，一个是寻优推荐候选），但"哪些
输入决定这份结果的不可变性"这一问题的答案是同一份契约，本函数因此直接
调用 `result_hash()`，不为 Baseline Gate 另写一份"看起来差不多"的哈希
公式。

`candidate_id` / `parameters_si` 取自 `check_baseline_gate()` 已经在
函数体内构造好的 `baseline_candidate`（`Candidate` 实例，`origin=
'baseline'`，第 5 步已 `persist_candidate()` 落库）；`model_package_hash`
/ `constraints_hash` / `metrics_hash` / `scenario_set_hash` 复用 `gate_key`
计算时已经算出的同一份值（`_compute_gate_key()` 的三个局部哈希 +
`frozen_model_package_hash`），不重新解析闭包或重新序列化配置——这与
`check_model_package_hash()` 供 `check_dual_model_consistency()` 复用
重算值是同一原则："同一个值只算一次，不同调用点各自复用"。为此，
`constraints_hash_value` / `metrics_hash_value` / `scenario_set_hash_value`
三个局部变量从 `create_task()` 调用点提前抽出并沿用到 PASS 路径，不在
两处各自调用一次 `constraints_hash(...)`/`metrics_hash(...)`/
`compute_scenario_set_hash(...)`。

### b. `worst_case` / `per_scenario` / `constraints` 的取数：手工查库，
不复用 `eval.aggregate.worst_case()`

`_collect_frozen_baseline_snapshot()`（本次新增的模块级私有函数）直接对
`_evaluate_baseline_gate_rows()` 已经确认合规的 `runs`/`metric_results`/
`constraint_results` 三表逐场景查询，不调用 `eval.aggregate.worst_case()`
——原因见该函数自身文档："不复用……"一节：`worst_case()` 底层的
`WORST_CASE_SQL` 依赖 `scenario_set` 表确定 `eval_set`，但 Baseline Gate
的 `gate_task_id` 从未调用 `freeze_scenario_set()`（本函数流程第 4 步只
`create_task()`，不写 `scenario_set` 表——Baseline Gate 是一次性判定，
不是"寻优任务"，不需要该任务自己的冻结场景集副本；`evaluation_rows(
task_cfg)` 已经是它需要的全部场景来源），若误用 `worst_case()`，`eval_set`
CTE 会因空表而返回空结果集，`worst_case()` 因此静默返回 `{}` ——这是一个
比"抛异常"更危险的静默错误（"通过了門禁却冻结了一个空的聚合值"），因此本
函数选择手工聚合，代价是与 `eval.aggregate.py` 的 SQL 逻辑有一处刻意的
重复（`MAX(value)`），但换来的是明确、可读的取数路径，不依赖一张本函数
从未写过的表。

`worst_case` 只取 `MAX(value)`，不按 `metrics_cfg.objective.primary.
direction` 调整符号——`direction` 只影响排序（`eval/aggregate.py`
`sort_key()` 的职责），不改变聚合值本身，`design.md` §5.4 的
`WORST_CASE_SQL` 字面上也只是 `MAX(value)`，本函数遵循同一口径。

### c. PASS 路径的收尾顺序：`freeze_baseline()` 先于 `finish_task()`

与 FAIL 路径（`finish_task()` 先于 `freeze_baseline()`）顺序相反，理由：
FAIL 路径的 `finish_task(stop_reason='stop_and_ask_human', cause=
'baseline_infeasible')` 与 `freeze_baseline(passed=False, ...)` 两次写入
彼此独立、顺序不影响正确性（`freeze_baseline()` 不读 `tasks` 行的
`stop_reason`），此处沿用 PASS 路径新引入的顺序惯例——先把"这次评价的
不可变快照"（`baselines` 行）落定，再把"这条 `baseline_gate` 任务行"标记
为结束（`finish_task()`），语义上更接近"先有结果，再关任务"的自然顺序。
两次写入均为独立的 `Store.tx()` 事务（`freeze_baseline()`/`finish_task()`
各自内部 `with self.tx() as conn:`），不在一个事务内原子完成——`design.md`
未要求两者原子性绑定，且 `freeze_baseline()` 对同一 `gate_key` 幂等
no-op（不会因为重复调用而产生第二次冻结或报错），即使两次写入之间进程
中断，下一次 `check_baseline_gate()` 调用会经 `store.find_baseline(
gate_key)` 命中缓存并直接返回，不会重新执行仿真或误判为未冻结。

### d. 新增 `stop_reason` 取值 `baseline_gate_passed`：与任务 14.7 的
`checkpoint1_rejected` 同一先例、同一论证结构

`store/ddl.sql` 的 `tasks.stop_reason` 列本身只是 `TEXT`（无 `CHECK`
约束把取值域封闭为四值），`should_stop()`（`controller/stop.py`）产出的
四值封闭集合约束字面上只约束"由 `should_stop()` 判定停止"这一条路径——
Baseline Gate 同样不经过 `should_stop()`（它发生在寻优主循环、
`SearchState` 尚未创建之前，`check_baseline_gate()` 本身就是
`preflight()` 的一部分）。`checkpoint1_rejected`（任务 14.7，
`controller/run_task.py` 顶部"12.6 拒绝/未确认路径"一节）已经确立了这条
先例的完整论证：不违反 `should_stop()` 取值域的封闭性约束（因为
`should_stop()` 从未、也不会被要求产出这个值）、不违反
`design.md`/`requirements.md` 中"恰四值"字面表述的限定语境。本函数引入
第六个、同样与 `should_stop()` 四值并列但来源不同的取值：
`_BASELINE_GATE_PASSED_STOP_REASON = "baseline_gate_passed"`——语义为
"该 `baseline_gate` 任务行已顺利通过门禁并完成冻结"，与
`stop_and_ask_human`（FAIL 路径已经使用的既有取值）相对，同一 `task_kind
='baseline_gate'` 的任务行只会以这两个取值中的一个结束。

与 `checkpoint1_rejected` 相同，本次不为这个新取值扩展任何 CLI 侧的退出码
映射表（`cli._exit_code_for_stop_reason()`）——`baseline_gate` 是一条独立
于寻优任务的 `tasks` 行，从未经过 `cli.cmd_run()` 的退出码计算路径（该
路径只处理寻优任务自身的 `TaskOutcome.stop_reason`），这不属于本次任务的
改动范围。

### 12. `probe_single_run_s` / `gate_budget_max_engine_starts` /
`gate_budget_max_wallclock_hours`：无默认值的必填参数，与
`check_budget_feasibility()` 的既有先例同一处理

与 `check_budget_feasibility()`（任务 14.2）的 `probe_single_run_s` 完全
同一理由（探针实测值当前不可得，见该函数文档）：这三个值不接受默认值、
不在本函数内编造。`gate_budget_max_engine_starts` /
`gate_budget_max_wallclock_hours` 对应"为该门禁单独审批的额度"
（`requirements.md` R10.2/R2.15 的原文用词），与
`task_cfg.budget.max_engine_starts`/`max_wallclock_hours`（寻优任务自己
的预算，`Budget` 类型）是两组独立的数值来源——本函数不读
`task_cfg.budget` 的这两个字段去充当门禁自己的审批额度（那正是
R2.15/L10-5 要求避免的"门禁墙钟计入寻优预算"）。三者均为必填 kwonly
标量参数，不塞进某个更大的容器类型。

### 13. `execution_env_hash`：由调用方原样传入，本函数不重新计算

与 `run_tier()` 已确立的先例相同（"`execution_env_hash: str`：
`store.cache.simulation_key()` 的七字段之一，... 本函数不计算、只原样
转发"）：`run_task.py` 的 `_compute_execution_env_hash()` 是该值的唯一
计算逻辑落点（一个私有函数），本文件不重复实现同一段计算、也不跨模块
导入这个私有函数——接收一个必填 kwonly `execution_env_hash: str` 参数，
由调用方（未来接线 `check_baseline_gate()` 的 `run_task()` 代码）传入
它已经算好的同一个值（寻优任务与它的 Baseline Gate 共享同一次
`_compute_execution_env_hash(model_cfg)` 调用结果是合理的——两者面对
同一个 `model_cfg`/执行环境，没有理由算出不同的值）。

本任务（10.5）只实现 `metric_not_implemented` 这一项断言，作为本文件的初始
骨架：`PreflightError` 异常类型（供后续全部波次复用）与
`check_metric_not_implemented()`。顶层 `preflight()` 编排函数与其余
`check_*` 函数留给对应波次的任务添加，不在此提前搭建空壳。

## 任务 11.4：`check_margin_extraction_ready()` 的签名偏离说明

`design.md` §6.2.2 给出的字面签名是
`def check_margin_extraction_ready(metrics_cfg: MetricsConfig) -> None: ...`。
但本函数需要实现的第四条断言（`primary_method='linear_analysis_on_averaged'`
时要求 `averaged_model_required=true`，`tasks.md` 任务 11.4 / `requirements.md`
Requirement 8 的 AC10）天然需要读 `model.yaml` 的
`ModelConfig.averaged_model_required` 字段，仅凭 `MetricsConfig` 无法完成该
断言。因此本函数把签名拓宽为
`check_margin_extraction_ready(metrics_cfg: MetricsConfig, model_cfg:
ModelConfig) -> None`，多加一个 `model_cfg` 形参——这与本文件既有的
`check_model_package_hash(model_cfg, store)` / `check_dual_model_consistency
(model_cfg, store)` 两个函数在 `design.md` §6.2.2 中已经各自比字面签名多带
一个协作依赖形参（`store`）是同一种模式：字面签名给出的是「这个函数大致对应
哪个配置节」，不是逐字节的最终接口；真正需要跨节数据时加形参，不塞入更大的
容器类型或做隐式全局读取。
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from poweragent.config.hashing import (
    canonical_json,
    constraints_hash,
    metrics_hash,
    result_hash,
)
from poweragent.config.hashing import candidate_id as compute_candidate_id
from poweragent.config.hashing import scenario_set_hash as _compute_scenario_set_hash
from poweragent.config.schema import (
    Budget,
    ConstraintsConfig,
    MetricsConfig,
    ModelConfig,
    TaskConfig,
)
from poweragent.controller.budget import BudgetLedger
from poweragent.controller.scenario import compute_scenario_set_hash, evaluation_rows
from poweragent.eval.metrics import IMPLEMENTED_TIME_DOMAIN_METRICS
from poweragent.sim.engine import MatlabSession
from poweragent.sim.hashing import model_package_hash, resolve_dependency_closure
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Candidate, ScenarioSpec as RepoScenarioSpec, Store

__all__ = [
    "PreflightError",
    "check_metric_not_implemented",
    "check_margin_extraction_ready",
    "check_observable_unbound",
    "check_unit_mismatch",
    "check_config_completeness",
    "check_freeze_consistency",
    "Milestone",
    "check_task_set_freeze_consistency",
    "check_model_package_hash",
    "check_dual_model_consistency",
    "check_budget_feasibility",
    "BaselineGateStatus",
    "check_baseline_gate",
]


class PreflightError(Exception):
    """`preflight()` 全部断言共用的异常类型：任一断言失败即 raise 本异常，不降级
    放行（`design.md` §6.2.2）。消息格式沿用各断言在设计文档中给出的字面量
    模式，如 `f"metric_not_implemented: {metric_id}"`——`PreflightError` 本身
    只承载一条消息字符串，不需要额外的结构化字段。
    """


# phase_margin / gain_margin 由 `eval/margin.py`（任务 11.x，尚未落地）实现，
# 不在 `eval/metrics.py` 内——因此不能像时域五项那样从一个已存在的公开常量派生。
# 这两个字符串硬编码为字面量补充，而不是等 `eval/margin.py` 落地后再改为
# import：本断言只需要知道「这两个 metric_id 存在具名计算函数」这一事实，该
# 事实由 design.md §4.3（`MetricsSection.phase_margin` / `.gain_margin` 恰有
# 定义）与任务 11.x 的落地承诺共同保证，不依赖 `eval/margin.py` 的内部实现
# 细节，因此在此硬编码不构成与该模块的耦合。
_MARGIN_METRIC_IDS: frozenset[str] = frozenset({"phase_margin", "gain_margin"})

# `active_metrics` 中「有对应具名计算函数」的 metric_id 全集：五项时域指标
# （`eval/metrics.py`，任务 10.1/10.2）+ phase_margin/gain_margin（上方硬编码）。
# `efficiency` / `sensitivity` 是 `config.schema.MetricId` 中存在的合法取值
# （阶段 2 预留），但阶段 1 没有任何模块实现它们的计算路径（design.md §0.5：
# 「efficiency 与 sensitivity 默认不激活（二维阶段不含）」）——若配置把它们
# 放进 `active_metrics`，本断言应当捕获，而不是让它们悄悄地在
# `compute_metrics()` 里被跳过（`eval/metrics.py` 对不在自己集合内的 metric_id
# 天然不产出结果，参见该模块 `compute_metrics()` 的 docstring）。
_IMPLEMENTED_METRIC_IDS: frozenset[str] = IMPLEMENTED_TIME_DOMAIN_METRICS | _MARGIN_METRIC_IDS


def check_metric_not_implemented(metrics_cfg: MetricsConfig) -> None:
    """`active_metrics` 含在 `eval.metrics`/`eval.margin` 中无对应具名计算函数的
    `metric_id` ⟹ `PreflightError('metric_not_implemented: <metric_id>')`。

    不静默跳过该指标：静默跳过会把配置错误藏到报告里，`eval.metrics` 也不
    提供静默跳过的代码路径（`tasks.md` 任务 10.5）。命中第一个未实现的
    `metric_id` 即立即抛出，不继续收集其余未实现项——`preflight()` 的目标是
    「绝不静默放行」，不是产出一次性的详尽报告。
    """

    for metric_id in metrics_cfg.active_metrics:
        if metric_id not in _IMPLEMENTED_METRIC_IDS:
            raise PreflightError(f"metric_not_implemented: {metric_id}")


# ===========================================================================
# check_margin_extraction_ready()：任务 11.4（design.md §6.2.2；`tasks.md`
# 任务 11.4；需求追溯 R2.5, R2.6, R2.14, R8.1, R8.8, R8.10；属性 CP-11）
# ===========================================================================


def check_margin_extraction_ready(
    metrics_cfg: MetricsConfig, model_cfg: ModelConfig
) -> None:
    """裕量提取链路的四条就绪断言，任一不满足即 `raise PreflightError`、不
    降级放行（与本文件其余 `check_*` 函数的既有原则一致）。

    四条断言按「结构性、成本最低」到「需要读磁盘/算哈希、成本较高」的顺序
    排列（fail-fast）：

    1. `phase_margin_must_be_active`——`active_metrics` 必须含
       `'phase_margin'`。裕量作为硬约束依据（`hard_constraints.
       phase_margin_min`），若它自己都不在 `active_metrics` 里，后续三条
       断言（以及运行期的裕量提取本身）无从谈起，因此排在最前。只读一个
       列表成员资格，不碰文件系统。
    2. `margin_cross_check_missing`——`cross_check_record.path` 所指文件
       必须存在。`config/schema.py` 的 `CrossCheckRecord` 已把 `path` /
       `sha256` 定为必填非空字符串字段（结构层面不可能「为空」，schema
       构造已经在 `config/loader.py` 的管线里替这一半把关），本断言实际
       检查的是「schema 无法在配置加载期验证的那一半」：文件系统状态。
    3. `margin_cross_check_mismatch`——文件存在之后，其内容 sha256 必须与
       `cross_check_record.sha256` 相等，确认核对记录未被后续修改。
       `hashlib.sha256(...).hexdigest()` 的输出恒为小写十六进制，与
       `CrossCheckRecord.sha256` 的 pydantic 正则
       `^[0-9a-f]{64}$`（同样只接受小写）天然一致，因此用普通字符串
       相等比较即可，不需要 `.lower()` 之类的大小写折叠。
    4. `margin_method_model_variant_mismatch`（`tasks.md` 任务 11.4 原文
       未给出这一条的具名错误标识，只写「`PreflightError` 且消息标识两个
       字段路径」——此名为本次新增，取义于「`primary_method` 选择的模型
       变体与 `averaged_model_required` 不一致」）——若
       `primary_method='linear_analysis_on_averaged'`，则必须
       `model_cfg.averaged_model_required=True`，否则线性分析想在一个
       「不需要存在」的平均模型上执行。消息中同时给出
       `margin_extraction.primary_method` 与 `averaged_model_required`
       两个字段路径及其当前取值。

    `cross_check_margin()` 本身（开发期一次性交叉核对的计算逻辑、`ok=false`
    时的 `stop_and_ask_human` 触发）已在任务 11.3 落地于本模块之外的
    `eval/margin.py`，不在本函数职责范围内、不重复实现。核对记录的落盘路径
    （`artifacts/margin_cross_check.json`，不按 `task_id` 分目录）是
    `configs/metrics.yaml` 模板与 `config/schema.py` 既有的结构性事实（本
    函数只消费 `cross_check_record.path` 这个字符串值，不对其内容做路径
    形式上的额外校验——路径是否「按 task_id 分目录」是模板填写惯例，不是
    本函数能够或应该在运行期重新裁决的东西）。
    """

    if "phase_margin" not in metrics_cfg.active_metrics:
        raise PreflightError("phase_margin_must_be_active")

    cross_check_record = metrics_cfg.margin_extraction.cross_check_record
    record_path = Path(cross_check_record.path)
    if not record_path.is_file():
        raise PreflightError("margin_cross_check_missing")

    actual_sha256 = hashlib.sha256(record_path.read_bytes()).hexdigest()
    if actual_sha256 != cross_check_record.sha256:
        raise PreflightError("margin_cross_check_mismatch")

    primary_method = metrics_cfg.margin_extraction.primary_method
    if primary_method == "linear_analysis_on_averaged" and not model_cfg.averaged_model_required:
        raise PreflightError(
            "margin_method_model_variant_mismatch: "
            f"metrics.yaml:margin_extraction.primary_method={primary_method!r} "
            "requires model.yaml:averaged_model_required=true "
            f"(currently {model_cfg.averaged_model_required!r})"
        )


# ===========================================================================
# check_observable_unbound() / check_unit_mismatch()：任务 12.6（design.md
# §6.2.2 断言清单；`tasks.md` 任务 12.6；需求追溯 R2.12, R2.13, R9.13；属性 CP-13）
# ===========================================================================
#
# 签名说明：`design.md` §6.2.2 给出的 `preflight()` 函数清单本身不含
# `observable_unbound` / `unit_mismatch` 对应的字面 `check_*` 签名——那段代码块
# 只列了 `check_config_completeness` 起的七个函数；这两条断言只在紧随其后的
# 纯文本断言清单（`metric_not_implemented` / `observable_unbound` /
# `unit_mismatch`）中以断言语义（不是函数签名）描述。本文件任务 10.5 遇到过
# 同样的情形（`metric_not_implemented`），当时的解法是补一个
# `check_metric_not_implemented(metrics_cfg: MetricsConfig) -> None`；这里延续
# 同一命名与签名风格：`check_observable_unbound` / `check_unit_mismatch`，均取
# `(constraints_cfg: ConstraintsConfig, metrics_cfg: MetricsConfig) -> None`——
# 两条断言都需要同时读 `constraints.yaml` 的 `hard_constraints`（约束名与
# `observable` 字段）与 `metrics.yaml`（`constraint_observables` 键集合、
# `active_metrics`、各指标/观测量的 `unit`），因此两个配置形参缺一不可。
#
# `HardConstraints` 恰五个具名字段（R9.1），遍历顺序固定为
# `vout_min / vout_max / peak_current_max / phase_margin_min / gain_margin_min`，
# 与 `eval/constraints.py` 的 `_HARD_CONSTRAINT_NAMES` 遍历顺序一致（该顺序本身
# 不影响正确性，只影响命中第一个违规约束时的报告顺序，取一致顺序便于测试与
# 报告展示比对）。

_HARD_CONSTRAINT_NAMES: tuple[str, ...] = (
    "vout_min",
    "vout_max",
    "peak_current_max",
    "phase_margin_min",
    "gain_margin_min",
)

# 硬约束名 → 该约束语义上必须匹配的物理单位。与 `eval/constraints.py` 的
# `_HARD_CONSTRAINT_UNITS` 内容相同、但独立定义于本模块，不跨模块导入该私有
# 常量：这五个约束名到单位的绑定是 `requirements.md` Requirement 9 AC1 已锁定
# 的封闭事实（`vout_min`/`vout_max` → V、`peak_current_max` → A、
# `phase_margin_min` → deg、`gain_margin_min` → dB），不是运行期从某个模块
# 动态解析得到的派生值——两个
# 模块各自持有一份该常量镜像是「同一份不会变化的事实各自表达一次」，不是需要
# 消除的重复。这也正是本函数存在的理由：`check_unit_mismatch()` 的职责就是
# 保证 `metrics.yaml` 中该约束语义对应的单位始终与这份固定映射逐字符一致，
# 一旦 preflight 通过，`eval/constraints.py` 硬编码同一份映射即不可能与
# `metrics.yaml` 产生运行期漂移（见该文件模块 docstring「`Violation.unit` 的
# 取数来源」一节）。
_HARD_CONSTRAINT_EXPECTED_UNITS: Mapping[str, str] = {
    "vout_min": "V",
    "vout_max": "V",
    "peak_current_max": "A",
    "phase_margin_min": "deg",
    "gain_margin_min": "dB",
}


def _constraint_observable_keys(metrics_cfg: MetricsConfig) -> frozenset[str]:
    """`metrics_cfg.constraint_observables` 的 YAML 键集合（如 `'obs.vout_min'`），
    不是 pydantic 属性名（`'vout_min'`）——`hard_constraints.<name>.observable`
    在配置文件里写的就是带 `obs.` 前缀的别名形式（见 `configs/constraints.yaml`
    模板），因此判定「`observable` 是否属于 `constraint_observables` 键集合」
    必须比较别名，不能比较属性名。`ConstraintObservables`（`config/schema.py`）
    对每个字段都声明了 `alias=`，故直接读 `model_fields[...].alias`。
    """
    fields = type(metrics_cfg.constraint_observables).model_fields
    return frozenset(field.alias or name for name, field in fields.items())


def _resolve_observable_unit(observable: str, metrics_cfg: MetricsConfig) -> str | None:
    """按 `observable` 字符串解析其在 `metrics.yaml` 中声明的单位；解析不到（即
    该 `observable` 既不在 `constraint_observables` 键集合、也不在
    `active_metrics` 里有对应字段）返回 `None`。

    `None` 表示「取数路径本身就绑定不上」——这正是 `check_observable_unbound()`
    要拦截的情形，`check_unit_mismatch()` 遇到 `None` 时跳过该约束、不重复
    报告 `observable_unbound`（两个断言各管各的错误标识，不越界）。
    """
    constraint_observables = metrics_cfg.constraint_observables
    for attr_name, field in type(constraint_observables).model_fields.items():
        alias = field.alias or attr_name
        if alias == observable:
            return getattr(constraint_observables, attr_name).unit

    if observable in metrics_cfg.active_metrics:
        metrics_section = metrics_cfg.metrics
        if observable in type(metrics_section).model_fields:
            return getattr(metrics_section, observable).unit

    return None


def check_observable_unbound(
    constraints_cfg: ConstraintsConfig, metrics_cfg: MetricsConfig
) -> None:
    """某条 `hard_constraint` 的 `observable` 既不属于
    `metrics_cfg.constraint_observables` 的键集合、也不属于
    `metrics_cfg.active_metrics` ⟹ `PreflightError('observable_unbound:
    <约束名>')`，不进入寻优循环（`requirements.md` R2.12, R9.13）。

    命中第一个违规约束即抛出，不继续收集其余违规项——与本文件
    `check_metric_not_implemented()` 的 fail-fast 原则一致（`preflight()`
    目标是绝不静默放行，不是产出一次性详尽报告）。`judge()`
    （`eval/constraints.py`）按 `entry.observable` 从 `MetricResult` 序列取值，
    若绑定不存在则无据可依，因此本断言必须在寻优循环开始前拦截。
    """
    constraint_observable_keys = _constraint_observable_keys(metrics_cfg)
    active_metrics = set(metrics_cfg.active_metrics)

    for name in _HARD_CONSTRAINT_NAMES:
        entry = getattr(constraints_cfg.hard_constraints, name)
        observable = entry.observable
        if observable not in constraint_observable_keys and observable not in active_metrics:
            raise PreflightError(f"observable_unbound: {name}")


def check_unit_mismatch(
    constraints_cfg: ConstraintsConfig, metrics_cfg: MetricsConfig
) -> None:
    """同一物理量在 `metrics.yaml` 与 `constraints.yaml` 声明的单位字符串不
    逐字符相同 ⟹ `PreflightError('unit_mismatch: <约束名>')`，不进入寻优循环
    （`requirements.md` R2.13）。

    `constraints.yaml` 的 `HardConstraintEntry` 按 R9.1「恰四字段」的约束没有
    自己的 `unit` 字段（`config/schema.py` 顶部「与 `unit_mismatch` 的边界
    划分」一节已论证：这不是遗漏，而是 R9.1 锁定的形状）；「该约束在
    `constraints.yaml` 语义上声明的单位」因此取
    `_HARD_CONSTRAINT_EXPECTED_UNITS` 这份固定映射（`requirements.md`
    Requirement 9 AC1 已把 `vout_min`/`vout_max`/`peak_current_max`/
    `phase_margin_min` 与 V/V/A/deg 的绑定锁定为验收标准），与
    `metrics.yaml` 中该约束 `observable` 实际解析到的单位逐字符比较。

    与其在报告渲染期（任务 26.2）遇到两个单位来源不一致时再做优先级仲裁，
    不如在启动期直接拒绝——报告的单位取数路径依赖两处一致这一前提，仲裁只会
    掩盖配置错误、不会修复它。

    某约束的 `observable` 绑定不上（`_resolve_observable_unit()` 返回
    `None`）时跳过该约束，不在此处重复报出 `observable_unbound`——那是
    `check_observable_unbound()` 的职责。命中第一个单位不一致的约束即抛出，
    不继续收集其余项（fail-fast，与本文件其余 `check_*` 一致）。
    """
    for name in _HARD_CONSTRAINT_NAMES:
        entry = getattr(constraints_cfg.hard_constraints, name)
        actual_unit = _resolve_observable_unit(entry.observable, metrics_cfg)
        if actual_unit is None:
            continue

        expected_unit = _HARD_CONSTRAINT_EXPECTED_UNITS[name]
        if actual_unit != expected_unit:
            raise PreflightError(f"unit_mismatch: {name}")


# ===========================================================================
# check_config_completeness() / check_freeze_consistency() /
# check_model_package_hash() / check_dual_model_consistency() /
# check_budget_feasibility()：任务 14.2（design.md §6.2.2；`tasks.md` 任务
# 14.2；需求追溯 R2.3, R2.4, R2.7, R3.7, R3.10, R3.11, R6.11, R24.2, R24.3；
# 属性 CP-13）
# ===========================================================================
#
# 本节五个函数的范围边界，见本文件模块 docstring "14.2 ->" 一段与其后的
# 补充说明；不再在此重复。


def check_config_completeness(task_cfg: TaskConfig) -> None:
    """`task.yaml` 的 `scenarios` 不含 `tier='evaluation'` 行 ⟹
    `PreflightError('config_incomplete: scenarios[tier=evaluation]')`，且不
    进入寻优循环（`requirements.md` R6.11）。

    这是"配置齐备性"这一大类断言里本文件本轮唯一新增的一条——`config_incomplete`
    的绝大部分情形（必填字段缺失/`null`/占位符残留）已由 `config/loader.py`
    （任务 5.2）在 pydantic 模型构造之前挡住，走不到这里；本函数只补上
    `config/loader.py` 结构层面无法表达的一条业务规则：`scenarios` 列表本身
    非空（schema 已保证）不等于"至少一行 `tier='evaluation'`"，后者是
    跨行的语义约束，`config.schema.TaskConfig` 没有为它写
    `model_validator`（`tasks.md` 任务 14.2 明确把这条断言放在
    `controller.preflight`，不是 `config.schema`）。

    命中判定复用 `controller.scenario.evaluation_rows()`（任务 14.1），不在
    本函数内重新遍历 `task_cfg.scenarios` 按 `tier` 过滤——两处过滤逻辑没有
    理由写两遍。
    """
    if len(evaluation_rows(task_cfg)) == 0:
        raise PreflightError("config_incomplete: scenarios[tier=evaluation]")


def _current_model_package_hash(model_cfg: ModelConfig, *, base_dir: str | Path) -> str:
    """`model_cfg` 对应的当前 `model_package_hash` 重算值：先
    `resolve_dependency_closure()` 得到（已按 `averaged_model_required` 合并
    好的）闭包路径序列，再 `model_package_hash()` 对闭包整体取摘要。

    `model_dump(mode="json")` 转换惯例与 `sim/simulate.py` 模块 docstring
    "`model_cfg` 的 pydantic 模型 → `sim.hashing` 期望的 `Mapping[str, Any]`"
    一节完全一致，不重新发明转换方式。
    """
    model_dump = model_cfg.model_dump(mode="json")
    closure_paths = resolve_dependency_closure(model_dump, base_dir=base_dir)
    return model_package_hash(closure_paths, model_dump)


def check_model_package_hash(
    model_cfg: ModelConfig, store: Store, *, base_dir: str | Path = "."
) -> str:
    """重新解析 `model_cfg` 的依赖闭包并重算 `model_package_hash`；与
    `store.find_freeze('model_package')` 的既有记录比较，缺失该冻结行或
    `hash` 不一致 ⟹ `PreflightError`（消息标识 `kind='model_package'`），且不
    改写该冻结行（本函数只读 `freezes` 表，`freeze()` 本身的"写一次即锁"由
    `store/repo.py` 负责，此处不重复实现）。

    通过时返回本次重算出的哈希字符串（`design.md` §6.2.2 字面签名的返回
    类型为 `str`），供 `check_dual_model_consistency()` 复用，避免对同一个
    `model_cfg` 重复解析闭包（`requirements.md` R3.7, R3.11）。
    """
    current_hash = _current_model_package_hash(model_cfg, base_dir=base_dir)

    freeze = store.find_freeze("model_package")
    if freeze is None or freeze.hash != current_hash:
        raise PreflightError("freeze_inconsistent: model_package")

    return current_hash


def check_freeze_consistency(
    constraints_cfg: ConstraintsConfig,
    metrics_cfg: MetricsConfig,
    model_cfg: ModelConfig,
    task_cfg: TaskConfig,
    store: Store,
    *,
    base_dir: str | Path = ".",
) -> None:
    """三个冻结点中的两个——`kind='model_package'` 与 `kind='safety'`——的
    一致性比对；`kind='task_set'` 是警告式（M5 起、不 raise，`requirements.md`
    R3.13），由独立函数 `check_task_set_freeze_consistency()`（任务 19.1）
    处理，本函数不涉及（见本文件模块 docstring "14.2 ->" 一段 与
    `check_task_set_freeze_consistency()` 自身文档的分工说明）。

    `kind='model_package'` 一半委托给 `check_model_package_hash()`（不重复
    解析闭包，见该函数文档）。`kind='safety'` 一半自行计算：
    `H(constraints_hash, metrics_hash, scenario_set_hash)`——三个具名值经
    `canonical_json` 序列化后取 sha256（`design.md` §4.5 / `store/repo.py`
    模块 docstring 「`freeze()` 的『拒绝重复』语义」一节已明确这条哈希公式
    由调用方——即本函数——计算，`store.freeze()` 本身对 `hash` 内容不作
    解读）。

    `scenario_set_hash` 的重算不能读 `scenario_set` 表（`preflight()` 先于
    `freeze_scenario_set()` 调用，`design.md` §8.1 的 `run_task()` 伪代码：
    `preflight(...)` 在 `freeze_scenario_set(...)` 之前），因此按
    `controller.scenario.compute_scenario_set_hash()`（本任务新增，见该
    模块）从 `task_cfg.scenarios` 直接算出「若现在冻结将得到的哈希」，与
    `freezes(kind='safety')` 记录里早先冻结时算出的值比较——两次算法必须
    完全一致（`canonical_json` + sha256 的确定性序列化保证），否则任何
    `scenarios` 行的后续改动都会被本函数正确地判为不一致。

    两个 `kind` 中任一缺失或不一致，抛出的 `PreflightError` 消息中标识该
    `kind`（`requirements.md` R3.11："在其消息中标识该 `kind`"）。按
    `kind='model_package'` 优先、`kind='safety'` 其次的固定顺序检查，命中
    第一个不一致即返回，不继续检查另一个（fail-fast，与本文件其余
    `check_*` 一致）。
    """
    check_model_package_hash(model_cfg, store, base_dir=base_dir)

    constraints_h = constraints_hash(constraints_cfg.model_dump(mode="json"))
    metrics_h = metrics_hash(metrics_cfg.model_dump(mode="json"))
    scenario_set_h = compute_scenario_set_hash(task_cfg)
    current_safety_hash = hashlib.sha256(
        canonical_json(
            {
                "constraints_hash": constraints_h,
                "metrics_hash": metrics_h,
                "scenario_set_hash": scenario_set_h,
            }
        ).encode("utf-8")
    ).hexdigest()

    freeze = store.find_freeze("safety")
    if freeze is None or freeze.hash != current_safety_hash:
        raise PreflightError("freeze_inconsistent: safety")


# ===========================================================================
# check_task_set_freeze_consistency()：任务 19.1（design.md §4.5；`tasks.md`
# 任务 19.1；需求追溯 R21.2, R3.10, R3.13）
# ===========================================================================
#
# ## 里程碑顺序的显式表示：`Milestone` Literal + `_MILESTONE_ORDER` 元组
#
# `requirements.md` R3.10 的原文是「WHERE 当前里程碑为 M5 或之后」——「M5
# 或之后」是一个对**里程碑顺序**的比较，不是对里程碑名字符串本身的比较。
# `tasks.md` 模块顶部已把这个顺序锁定为固定序列：
# `P → M-1（条件）→ M0 → M1 → M1a → M2（条件）→ M3 → M4 → M4b → M5 → M6`。
# 本文件截至本任务（19.1）之前，代码库任何模块都不存在一个把这个顺序表示
# 为可比较值的类型或常量（`config/schema.py` 未定义、`store/ddl.sql` 的
# `tasks` 表也没有一列承载「当前里程碑」——`tasks` 行本身就是"寻优任务"或
# "Baseline Gate 任务"的一条记录，不是"整个项目当前处于哪个里程碑阶段"这一
# 全局状态的载体，两者是不同粒度的概念，不应该混为一谈）。
#
# 因此本函数把这个顺序显式落成 `Milestone`（`Literal` 别名，逐字符对应
# `tasks.md` 给出的十一个里程碑名）+ `_MILESTONE_ORDER`（同一顺序的元组，
# 下标即定义了「先后」）。「M5 或之后」的判断即
# `_MILESTONE_ORDER.index(current_milestone) >= _MILESTONE_ORDER.index('M5')`
# ——用元组下标比较，不用字符串前缀匹配（"M5 或之后"若用 `current_milestone
# >= 'M5'` 之类的字典序比较，`'M1a' >= 'M5'` 按字典序为假但 `'M6' >= 'M5'`
# 为真、`'M-1' >= 'M5'` 按字典序也为假——字典序恰好在这十一个字面量上给出
# 正确答案纯属偶然，換一批字面量（例如加入 `M10`）就会出错，因此不采用这种
# 脆弱的字符串比较，改用显式定义顺序的元组下标）。
#
# ## `current_milestone` 由调用方显式传入，不在本模块内维护隐藏状态
#
# 与本文件其余 `check_*` 函数一致的既定原则（模块 docstring 「
# `check_margin_extraction_ready()` 的签名偏离说明」一节：「真正需要跨节
# 数据时加形参，不塞入更大的容器类型或做隐式全局读取」）：本函数不去猜测
# 或推断"现在是哪个里程碑"（没有任何数据库列或配置字段记录这一全局事实，
# 见上一节），而是要求调用方（未来接线本函数的 `run_task()` 或人工触发的
# 库调用路径，`design.md` §9.2）显式传入 `current_milestone`。
Milestone = Literal[
    "P", "M-1", "M0", "M1", "M1a", "M2", "M3", "M4", "M4b", "M5", "M6"
]
_MILESTONE_ORDER: tuple[Milestone, ...] = (
    "P", "M-1", "M0", "M1", "M1a", "M2", "M3", "M4", "M4b", "M5", "M6",
)


def check_task_set_freeze_consistency(
    current_milestone: Milestone,
    task_set_md_path: str | Path,
    store: Store,
) -> None:
    """`kind='task_set'` 的告警式一致性校验（`requirements.md` R3.10, R3.13；
    `design.md` §4.5「任务集 | ... | M5 起 `preflight()` 告警式校验」）——
    与 `check_freeze_consistency()` 处理的另外两个 `kind`（`model_package` /
    `safety`）**不一致即 `raise PreflightError`** 的严格语义相反，本函数
    不一致时只输出一条告警，继续执行、不抛异常、不改写 `freezes` 行。

    这是本文件除 `check_freeze_consistency()` 之外，第三个、也是最后一个
    读取 `freezes` 表的断言函数——之所以拆成独立函数而不是塞进
    `check_freeze_consistency()` 内部，见该函数文档已经写明的分工："
    `kind='task_set'` 是警告式（M5 起、不 raise），归任务 19.1，本函数不
    涉及"——两者的失败语义（raise 还是 warn）完全不同，混在同一个
    "一致则返回、不一致则……"分支结构里会让 `check_freeze_consistency()`
    的 fail-fast 属性变得不纯粹（该函数当前对全部它处理的 `kind` 命中不一致
    即 raise，没有例外分支）。

    ## 里程碑门控：`current_milestone` 在 M5 之前时直接返回，不读文件、
    不读 `freezes` 表

    `_MILESTONE_ORDER.index(current_milestone) < _MILESTONE_ORDER.index
    ('M5')` 时立即返回——`requirements.md` R3.10 的字面条件是「WHERE 当前
    里程碑为 M5 或之后」，M5 之前调用本函数是「条件不成立」，不产生任何
    副作用（不计算 `task_set_md_path` 的 sha256、不查询 `store`），与
    `check_dual_model_consistency()` 在 `averaged_model_required=False`
    时立即返回、不读取任何记录是同一处理原则。

    ## 缺失冻结行（`task_set.md` 从未冻结过）：告警，不 raise——与「内容
    不一致」同一分支，不单独区分

    任务描述与 R3.10/R3.13 都没有显式覆盖"`freezes` 表里根本不存在
    `kind='task_set'` 行"这一情形（R3.10 只说「比对……的 hash」，R3.13 只说
    「sha256 与……的 hash 不一致」——两条 AC 的字面表述都隐含"该行存在"这个
    前提）。但 `tasks.md` 的门禁 G-M1a-1 已经在别处明确「`freezes
    (kind='task_set')` 行不存在则任务 23（`agent.propose`，M4 里程碑）不得
    开工」——这是一条**开工前置条件**，由人工/`run_task()` 的其他环节把关，
    不是本函数的职责；`design.md` 里程碑顺序把 M1a（任务集冻结）排在 M5 之前
    （`P → M-1 → M0 → M1 → M1a → ... → M5`），若流程被正确遵循，M5 或之后
    调用本函数时该行必然已经存在（M1a 早已完成）。因此"缺失"在本函数看来是
    一种流程被违反后的**异常状态**，但 R3.13 明确要求"不一致"这一失败模式
    走告警、不 raise——本函数选择把"缺失"与"内容不一致"归入同一个告警分支
    （消息文本区分两者），不为"缺失"单独抛出 `PreflightError`：这与
    `check_freeze_consistency()` 对 `model_package`/`safety` 的处理刻意不同
    （那里"缺失"与"不一致"合并触发同一个 `raise`），但符合本函数自身"告警式"
    的既定基调——若"缺失"要 raise 而"内容不一致"不 raise，会让同一个校验
    函数出现两种互相矛盾的失败语义，比统一为"告警"更令人困惑。

    ## 不改写 `freezes` 行

    本函数只调用 `store.find_freeze('task_set')`（只读），从不调用
    `store.freeze_task_set()` 或任何写入方法——R3.13"且不改写该行"的字面
    要求由"本函数不包含任何写入语句"这一事实直接满足，不需要额外的防御性
    代码。

    ## 告警输出：`print(..., file=sys.stderr)`，不新增日志基础设施

    本代码库当前没有 `logging` 模块的既有使用惯例（`grep` 全库确认），
    `cli.py` 的既有错误路径统一用 `click.echo(..., err=True)`，但本函数
    位于 `controller/` 而非 `cli.py`——CLI 层已有的 `click` 依赖不应该被
    一个 controller 层的库函数（`design.md` §9.2 的库调用路径，本任务描述
    "不新增第五个 CLI 子命令"）反向引入耦合。选择标准库 `print(...,
    file=sys.stderr)`：与本文件其余 `check_*` 函数一样不引入新依赖，
    输出到 stderr 而非 stdout（告警不是本函数的返回值，不应混入任何调用方
    可能对 stdout 做的解析）。
    """
    if _MILESTONE_ORDER.index(current_milestone) < _MILESTONE_ORDER.index("M5"):
        return

    actual_sha256 = hashlib.sha256(Path(task_set_md_path).read_bytes()).hexdigest()
    freeze = store.find_freeze("task_set")
    if freeze is None:
        print(
            "WARNING: freeze_inconsistent: task_set "
            f"(no freezes(kind='task_set') row exists; current sha256 of "
            f"{task_set_md_path!s} is {actual_sha256})",
            file=sys.stderr,
        )
        return
    if freeze.hash != actual_sha256:
        print(
            "WARNING: freeze_inconsistent: task_set "
            f"(frozen hash={freeze.hash!r}, current sha256 of "
            f"{task_set_md_path!s}={actual_sha256!r})",
            file=sys.stderr,
        )


def _load_dual_model_consistency_record(record_path: Path) -> Mapping[str, object] | None:
    """读取 `record_path` 指向的一致性记录 JSON；文件不存在或内容不是合法
    JSON 均返回 `None`（"记录缺失"，见本文件模块 docstring 该函数专属小节）。
    """
    if not record_path.is_file():
        return None
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    return data


def check_dual_model_consistency(
    model_cfg: ModelConfig,
    store: Store,
    *,
    base_dir: str | Path = ".",
    record_path: str | Path = "artifacts/dual_model_consistency.json",
) -> None:
    """`averaged_model_required=true` 时的两条断言（`requirements.md` R24.2,
    R24.3）；`averaged_model_required=false` 时本函数立即返回、不读取任何
    记录（该场景下 `dual_model_consistency` 节按模板留空，`config/schema.py`
    的 `ModelConfig.dual_model_consistency` 默认值即为空 `checkpoints`，见
    `config/dual_model_consistency.py` 模块 docstring 的同一前提）。

    1. `model_cfg.model_package.averaged` 为空 ⟹
       `PreflightError('averaged_model_missing')`。
    2. 读取一致性记录（见本文件模块 docstring "任务 14.2：
       `check_dual_model_consistency()` 的一致性记录读取契约"一节的接口
       契约）：记录缺失、`ok` 非真、或记录绑定的 `model_package_hash` 与
       当前合并闭包的重算值不一致，三者任一命中 ⟹
       `PreflightError('dual_model_inconsistent')`，且（由异常中断执行这一
       事实本身保证）不启动任何仿真。

    "当前合并闭包的重算值"复用 `check_model_package_hash()` 的返回值，不
    在本函数内重新解析闭包——`averaged_model_required=true` 时
    `resolve_dependency_closure()` 本就产出合并闭包（switching + averaged，
    `sim/hashing.py` 任务 4.1 的既有行为），因此这里的重算值与
    `check_freeze_consistency()` 里比对 `kind='model_package'` 冻结行所用的
    是同一个值、同一次计算规则，只是本函数独立调用一次
    `check_model_package_hash()` 以保持自身可独立调用、可独立测试。
    """
    if not model_cfg.averaged_model_required:
        return

    if model_cfg.model_package.averaged is None:
        raise PreflightError("averaged_model_missing")

    current_hash = check_model_package_hash(model_cfg, store, base_dir=base_dir)

    record = _load_dual_model_consistency_record(Path(record_path))
    if (
        record is None
        or not record.get("ok")
        or record.get("model_package_hash") != current_hash
    ):
        raise PreflightError("dual_model_inconsistent")


def check_budget_feasibility(probe_single_run_s: float, budget_cfg: Budget) -> None:
    """`estimated_wallclock = probe_single_run_s × budget.max_engine_starts`；
    超过 `budget.max_wallclock_hours × 3600` ⟹
    `PreflightError('budget_wallclock_infeasible')`（`requirements.md` R2.3,
    R2.7；`design.md` §8.2 步骤 6）。

    `probe_single_run_s` 显式作为形参（不接受默认值、不在此处硬编码任何
    数值）：`design.md` §6.2.2 的字面签名是
    `check_budget_feasibility(task_cfg, metrics_cfg, probe: ProbeRecord)`，
    其中 `probe.single_run_s` 来自 P-1 探针的至少 3 次手动运行取最大值
    （`requirements.md` Requirement 2 AC1/AC2）；`ProbeRecord` 契约与
    `probe_report.md` 消费接口属任务 1.2，`tasks.md` 把它列为"测量门禁类
    任务"、由人工 G-P-1 门禁触发、与其余任务的代码开发顺序无关（`tasks.md`
    "测量门禁类任务"一节）——真实探针实测值目前不可得。本函数因此不接受
    `ProbeRecord` 或 `task_cfg`/`metrics_cfg` 整体（那些字段本函数根本不需要
    读），只接受这条断言实际用到的两个标量：`probe_single_run_s`（调用方从
    `ProbeRecord.single_run_s` 传入，测试中可传入任意 mock 数值）与
    `budget_cfg`（`task_cfg.budget`，`config.schema.Budget`，已含
    `max_engine_starts` 与 `max_wallclock_hours` 两个所需字段）。真实调用点
    的接线（从已加载的 `task_cfg.budget` 与 `1.2` 落地后的 `ProbeRecord` 取值）
    留给 `run_task()` 编排（任务 14.4/14.5），不在本函数内部构造这两个上游
    对象。
    """
    estimated_wallclock = probe_single_run_s * budget_cfg.max_engine_starts
    if estimated_wallclock > budget_cfg.max_wallclock_hours * 3600:
        raise PreflightError("budget_wallclock_infeasible")


# ===========================================================================
# check_baseline_gate()：任务 16.1（design.md §6.2.2, §8.2 步骤 7；`tasks.md`
# 任务 16.1；需求追溯 R2.15, R10.2, R10.3, R10.6, R10.10, R10.11；属性 CP-13）
# ===========================================================================
#
# 本节唯一函数的全部设计决策，见本文件模块 docstring "任务 16.1：
# `check_baseline_gate()` 的范围收窄、签名扩展与设计决策"一节，不再在此重复。


@dataclass(frozen=True, slots=True)
class BaselineGateStatus:
    """`check_baseline_gate()` 的返回类型（`design.md` §6.2.2 字面给出的四
    字段 + 本文件补充的 `gate_key`）。

    与 `store.repo.BaselineGateStatus`（`find_baseline()` 的返回类型）同名
    不同形，两者不互相转换，见本文件模块 docstring 第 1 节。
    """

    passed: bool
    frozen_result_hash: str | None
    evidence_bound: bool
    diagnosis_ref: str | None
    gate_key: str


def _compute_gate_key(
    *,
    model_package_hash_value: str,
    constraints_cfg: ConstraintsConfig,
    metrics_cfg: MetricsConfig,
    task_cfg: TaskConfig,
) -> str:
    """`gate_key = H(model_package_hash, constraints_hash, metrics_hash,
    scenario_set_hash)`，与 `check_freeze_consistency()` 计算
    `kind='safety'` 冻结值的公式同一模式（`canonical_json` + sha256），只是
    多纳入 `model_package_hash` 一项（`requirements.md` R10.6 / `design.md`
    §8.2 步骤 7 字面公式）。
    """
    constraints_h = constraints_hash(constraints_cfg.model_dump(mode="json"))
    metrics_h = metrics_hash(metrics_cfg.model_dump(mode="json"))
    scenario_set_h = compute_scenario_set_hash(task_cfg)
    return hashlib.sha256(
        canonical_json(
            {
                "model_package_hash": model_package_hash_value,
                "constraints_hash": constraints_h,
                "metrics_hash": metrics_h,
                "scenario_set_hash": scenario_set_h,
            }
        ).encode("utf-8")
    ).hexdigest()


def _to_repo_scenario_spec(spec: object) -> RepoScenarioSpec:
    """`controller.scenario.ScenarioSpec`（本地临时类型）→
    `store.repo.ScenarioSpec`（`run_tier()` 实际要求的跨模块契约类型）的
    显式 1:1 字段拷贝。与 `controller/run_task.py` 的私有 `_to_repo_scenario()`
    内容等价但独立定义，见本文件模块 docstring 第 6 节（不跨模块引用另一个
    模块的下划线前缀私有名称）。
    """
    return RepoScenarioSpec(
        scenario_id=spec.scenario_id,  # type: ignore[attr-defined]
        tier=spec.tier,  # type: ignore[attr-defined]
        model_variant=spec.model_variant,  # type: ignore[attr-defined]
        require_margin=spec.require_margin,  # type: ignore[attr-defined]
        vin_v=spec.vin_v,  # type: ignore[attr-defined]
        temp_c=spec.temp_c,  # type: ignore[attr-defined]
        load_start_a=spec.load_start_a,  # type: ignore[attr-defined]
        load_end_a=spec.load_end_a,  # type: ignore[attr-defined]
        slew_a_per_us=spec.slew_a_per_us,  # type: ignore[attr-defined]
        spec_version=spec.spec_version,  # type: ignore[attr-defined]
    )


def _evaluate_baseline_gate_rows(
    store: Store,
    *,
    gate_task_id: str,
    candidate_id: str,
    evaluation_scenarios: tuple[RepoScenarioSpec, ...],
    active_metrics: tuple[str, ...],
) -> str | None:
    """`requirements.md` R10.3 的判据：`evaluation_scenarios` 中任一
    `scenario_id` 不满足 `runs.status='done'`，或 `constraint_results.
    feasible=0`，或任一 `active_metrics` 项 `metric_results.valid=0`，即
    命中第一个违规场景并返回一句纯事实陈述（诊断文本）；全部场景/指标均
    合规则返回 `None`。

    直接经 `store.connection` 发起只读 SQL（`Store` 未提供专用方法，见本
    文件模块 docstring 第 7 节），不判定可行性、只读取既有列的原始值。
    """
    conn = store.connection
    for scenario in evaluation_scenarios:
        row = conn.execute(
            "SELECT run_id, status FROM runs "
            "WHERE task_id=? AND candidate_id=? AND scenario_id=? "
            "ORDER BY started_at DESC LIMIT 1",
            (gate_task_id, candidate_id, scenario.scenario_id),
        ).fetchone()
        if row is None:
            return (
                f"scenario_id={scenario.scenario_id!r}: no runs row found "
                "(expected status='done')"
            )
        run_id, status = row
        if status != "done":
            return (
                f"scenario_id={scenario.scenario_id!r}: run_id={run_id!r} "
                f"status={status!r} (expected 'done')"
            )

        cr_row = conn.execute(
            "SELECT feasible FROM constraint_results WHERE run_id=?", (run_id,)
        ).fetchone()
        if cr_row is None or not cr_row[0]:
            return (
                f"scenario_id={scenario.scenario_id!r}: run_id={run_id!r} "
                "constraint_results.feasible=0 or missing"
            )

        for metric_id in active_metrics:
            m_row = conn.execute(
                "SELECT valid FROM metric_results WHERE run_id=? AND metric_id=?",
                (run_id, metric_id),
            ).fetchone()
            if m_row is None or not m_row[0]:
                return (
                    f"scenario_id={scenario.scenario_id!r}: run_id={run_id!r} "
                    f"metric_results.valid=0 or missing for metric_id={metric_id!r}"
                )

    return None


_BASELINE_GATE_PASSED_STOP_REASON = "baseline_gate_passed"


def _collect_frozen_baseline_snapshot(
    store: Store,
    *,
    gate_task_id: str,
    candidate_id: str,
    evaluation_scenarios: tuple[RepoScenarioSpec, ...],
    active_metrics: tuple[str, ...],
    primary_metric_id: str,
) -> tuple[float, list[dict[str, object]], list[dict[str, object]]]:
    """PASS 路径专属：为 `config.hashing.result_hash()` 组装
    `worst_case` / `per_scenario` / `constraints` 三项（任务 16.2；
    `requirements.md` R10.5, R10.9）。

    只在 `_evaluate_baseline_gate_rows()` 已确认全部场景 `done`/`feasible=1`/
    `valid=1`（即 `failure_reason is None`）之后调用——本函数因此不重复那些
    判定，只负责把已确认合规的行取出、组装为 `result_hash()` 期望的形状。

    `worst_case` 的取法：`design.md` §5.4 的 `WORST_CASE_SQL` 对
    `primary_metric` 取 `MAX(value)`，**不按 `objective.primary.direction`
    调整符号**（方向调整是 `eval/aggregate.py` `sort_key()` 的职责，只影响
    排序，不改变 `worst_case` 这个聚合值本身，见该模块顶部文档「`sort_key()`
    的 tie_tolerance 分桶技术」一节的既定分工）。本函数因此同样只取
    `MAX(value)`，不引入方向调整。

    不复用 `eval.aggregate.worst_case()`：该函数底层的 `WORST_CASE_SQL` 按
    `scenario_set` 表（`WHERE task_id=:task_id AND tier='evaluation'`）确定
    `eval_set`——但 Baseline Gate 的 `gate_task_id` 从未调用
    `freeze_scenario_set()`（`check_baseline_gate()` 只调用
    `store.create_task()`，不写 `scenario_set` 表，见该函数流程步骤 4），
    对该 `task_id` 而言 `scenario_set` 表中没有任何行，`WORST_CASE_SQL` 的
    `eval_set` CTE 因此为空，查询会返回空结果集，不是"抛异常"而是"静默返回
    错误的空聚合"——这种静默错误比显式的手工聚合更危险。本函数直接对
    `_evaluate_baseline_gate_rows()` 已经确认过的 `run_id` 逐个查询
    `metric_results`/`constraint_results`，手工取 `MAX(value)`，不依赖
    `scenario_set` 表。

    `per_scenario` 覆盖 `active_metrics` 的每一项（不仅是 primary），
    `constraints` 每场景一行——与 `design.md` §5.3.1 `result_hash` 重算范围
    的字面形状一致：`per_scenario: [{scenario_id, metric_id, value, valid}]`、
    `constraints: [{scenario_id, feasible, violations}]`。排序留给
    `result_hash()` 内部完成（该函数已实现"内部排序，不要求调用方预先排序"），
    本函数不重复排序。
    """
    conn = store.connection
    per_scenario: list[dict[str, object]] = []
    constraints: list[dict[str, object]] = []
    primary_values: list[float] = []

    for scenario in evaluation_scenarios:
        run_id, _status = conn.execute(
            "SELECT run_id, status FROM runs "
            "WHERE task_id=? AND candidate_id=? AND scenario_id=? "
            "ORDER BY started_at DESC LIMIT 1",
            (gate_task_id, candidate_id, scenario.scenario_id),
        ).fetchone()

        for metric_id in active_metrics:
            value, valid = conn.execute(
                "SELECT value, valid FROM metric_results WHERE run_id=? AND metric_id=?",
                (run_id, metric_id),
            ).fetchone()
            per_scenario.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "metric_id": metric_id,
                    "value": value,
                    "valid": bool(valid),
                }
            )
            if metric_id == primary_metric_id:
                primary_values.append(value)

        feasible, violations_json = conn.execute(
            "SELECT feasible, violations FROM constraint_results "
            "WHERE candidate_id=? AND scenario_id=? AND run_id=?",
            (candidate_id, scenario.scenario_id, run_id),
        ).fetchone()
        constraints.append(
            {
                "scenario_id": scenario.scenario_id,
                "feasible": bool(feasible),
                "violations": json.loads(violations_json),
            }
        )

    worst_case_value = max(primary_values)
    return worst_case_value, per_scenario, constraints


def check_baseline_gate(
    model_cfg: ModelConfig,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
    task_cfg: TaskConfig,
    store: Store,
    *,
    session: MatlabSession,
    artifacts: ArtifactStore,
    execution_env_hash: str,
    frozen_fingerprint: str,
    frozen_model_package_hash: str,
    probe_single_run_s: float,
    gate_budget_max_engine_starts: int,
    gate_budget_max_wallclock_hours: float,
    gate_task_id: str | None = None,
    base_dir: str | Path = ".",
) -> BaselineGateStatus:
    """Baseline 可行性门禁（`design.md` §8.2 步骤 7；`requirements.md` R2.15,
    R10.2, R10.3, R10.6, R10.10, R10.11）：以 `model_cfg.baseline.
    parameters_si` 作为 `origin='baseline'` 候选，在 `evaluation_rows(
    task_cfg)` 的全部场景行上执行，按 R10.3 判定可行性。

    签名相对 `design.md` §6.2.2 字面签名（`check_baseline_gate(model_cfg,
    store) -> BaselineGateStatus`）的全部必要扩展、`gate_key` 计算、缓存
    查询、门禁自身墙钟断言、`run_tier()` 复用决策、PASS/FAIL 判定依据、
    `diagnosis_ref` 落盘方式、`gate_task_id`/预算隔离/`execution_env_hash`
    的处理，见本文件模块 docstring "任务 16.1：`check_baseline_gate()` 的
    范围收窄、签名扩展与设计决策"一节，此处不重复。

    流程：
      1. `gate_key = H(model_package_hash, constraints_hash, metrics_hash,
         scenario_set_hash)`（`model_package_hash` 取
         `frozen_model_package_hash`，即调用方在任务入口已冻结的全量哈希，
         不在本函数内重新解析闭包）。
      2. `store.find_baseline(gate_key)` 命中 ⟹ 不启动任何仿真：按缓存行
         `passed` 值直接返回或 raise（见下方"缓存命中"）。
      3. 未命中 ⟹ 门禁自身墙钟独立断言：`est_gate = probe_single_run_s ×
         len(evaluation_rows(task_cfg))`（预算单位已含裕量额外启动，
         `run_tier()` 内部按需为 `require_margin` 场景累加，此处的
         `len(...)` 只是"场景行数"这个最小可行估算口径的下界，与
         `check_budget_feasibility()` 的 `estimated_wallclock` 公式同一
         精神——只做保守估算，不逐场景区分是否 `require_margin`）；超出
         `gate_budget_max_wallclock_hours × 3600` ⟹
         `PreflightError('baseline_gate_wallclock_infeasible')`，不创建
         `tasks` 行、不发起任何仿真。
      4. `store.create_task(task_kind='baseline_gate', ...)`，独立
         `BudgetLedger` 绑定该 `gate_task_id`。
      5. 构造 `Candidate(origin='baseline', parameters_si=model_cfg.baseline.
         parameters_si)`，经 `store.persist_candidate(origin='baseline')`
         落库（不经 `agent.validate`）。
      6. `run_tier(candidate, 'evaluation', evaluation_scenarios, ...)`
         一次，`scenario_rows` 传入全部 Evaluation 行。
      7. 按 R10.3 判据逐场景查库（`_evaluate_baseline_gate_rows()`）：
         命中即 FAIL——`store.finish_task(stop_reason='stop_and_ask_human',
         cause='baseline_infeasible')`、写诊断文件、
         `store.freeze_baseline(passed=False, diagnosis_ref=...)`、
         `raise PreflightError(f'baseline_infeasible: {diagnosis_ref}')`。
         全部合规即 PASS——返回 `BaselineGateStatus(passed=True, ...)`，
         **不**调用 `freeze_baseline(passed=True, ...)`（留给任务 16.2，
         见模块 docstring 第 11 节）。

    `evidence_bound` 在缓存命中、PASS、FAIL 三条路径均按
    `model_cfg.baseline.measured_evidence_ref is not None` 计算一次
    （与门禁结果无关，见模块 docstring 第 2 节）。
    """
    from poweragent.controller.run_task import run_tier  # noqa: PLC0415 -- 延迟 import，破循环依赖，见模块文档

    evidence_bound = model_cfg.baseline.measured_evidence_ref is not None

    gate_key = _compute_gate_key(
        model_package_hash_value=frozen_model_package_hash,
        constraints_cfg=constraints_cfg,
        metrics_cfg=metrics_cfg,
        task_cfg=task_cfg,
    )

    cached = store.find_baseline(gate_key)
    if cached is not None:
        if not cached.passed:
            raise PreflightError(f"baseline_infeasible: {cached.diagnosis_ref}")
        return BaselineGateStatus(
            passed=True,
            frozen_result_hash=cached.frozen_result_hash,
            evidence_bound=evidence_bound,
            diagnosis_ref=cached.diagnosis_ref,
            gate_key=gate_key,
        )

    evaluation_rows_config = evaluation_rows(task_cfg)
    est_gate = probe_single_run_s * len(evaluation_rows_config)
    if est_gate > gate_budget_max_wallclock_hours * 3600:
        raise PreflightError("baseline_gate_wallclock_infeasible")

    resolved_gate_task_id = gate_task_id or f"baseline_gate_{uuid.uuid4().hex}"

    # 三者在 create_task() 与（PASS 路径）任务 16.2 的 result_hash payload 之间
    # 复用，只计算一次，不重复解析 `constraints_cfg`/`metrics_cfg`/`task_cfg`。
    constraints_hash_value = constraints_hash(constraints_cfg.model_dump(mode="json"))
    metrics_hash_value = metrics_hash(metrics_cfg.model_dump(mode="json"))
    scenario_set_hash_value = compute_scenario_set_hash(task_cfg)

    store.create_task(
        task_id=resolved_gate_task_id,
        simulation_only=task_cfg.simulation_only,
        task_kind="baseline_gate",
        model_package_hash=frozen_model_package_hash,
        metrics_hash=metrics_hash_value,
        constraints_hash=constraints_hash_value,
        scenario_set_hash=scenario_set_hash_value,
        execution_env_hash=execution_env_hash,
        calibration_hash="",
        budget_max_starts=gate_budget_max_engine_starts,
    )

    gate_ledger = BudgetLedger(
        store,
        resolved_gate_task_id,
        gate_budget_max_engine_starts,
        gate_budget_max_wallclock_hours * 3600.0,
    )

    baseline_parameters_si = dict(model_cfg.baseline.parameters_si.model_dump())
    baseline_candidate = Candidate(
        candidate_id=compute_candidate_id(baseline_parameters_si),
        parameters_si=baseline_parameters_si,
    )
    store.persist_candidate(
        baseline_candidate, task_id=resolved_gate_task_id, origin="baseline"
    )

    evaluation_scenarios = tuple(
        _to_repo_scenario_spec(spec) for spec in evaluation_rows_config
    )

    run_tier(
        baseline_candidate,
        "evaluation",
        evaluation_scenarios,
        model_cfg=model_cfg,
        metrics_cfg=metrics_cfg,
        constraints_cfg=constraints_cfg,
        session=session,
        store=store,
        artifacts=artifacts,
        budget_ledger=gate_ledger,
        task_id=resolved_gate_task_id,
        execution_env_hash=execution_env_hash,
        frozen_fingerprint=frozen_fingerprint,
        frozen_model_package_hash=frozen_model_package_hash,
        base_dir=base_dir,
    )

    failure_reason = _evaluate_baseline_gate_rows(
        store,
        gate_task_id=resolved_gate_task_id,
        candidate_id=baseline_candidate.candidate_id,
        evaluation_scenarios=evaluation_scenarios,
        active_metrics=tuple(metrics_cfg.active_metrics),
    )

    if failure_reason is not None:
        tmp = artifacts.stage(
            task_id=resolved_gate_task_id, kind="baseline_gate", name="diagnosis.txt"
        )
        tmp.write_bytes(failure_reason.encode("utf-8"))
        diagnosis_ref = artifacts.commit(tmp)

        store.finish_task(
            resolved_gate_task_id,
            stop_reason="stop_and_ask_human",
            cause="baseline_infeasible",
        )
        store.freeze_baseline(
            gate_key=gate_key,
            task_id=resolved_gate_task_id,
            passed=False,
            frozen_result_hash=None,
            diagnosis_ref=diagnosis_ref,
        )
        raise PreflightError(f"baseline_infeasible: {diagnosis_ref}")

    # ---- PASS：把 Baseline 候选的完整评价快照冻结为交付物5 前值基线 ----
    #
    # `任务 16.2`（见本文件模块 docstring「任务 16.2」一节）：门禁通过时，
    # 把 Baseline 候选在全部 Evaluation 行上的激活指标结果、硬约束判定结果
    # 与 worst-case 聚合值冻结为 `result_hash`，写 `baselines(passed=True,
    # frozen_result_hash=...)` 一行，并把该 `baseline_gate` 任务行收尾。
    worst_case_value, per_scenario, constraints = _collect_frozen_baseline_snapshot(
        store,
        gate_task_id=resolved_gate_task_id,
        candidate_id=baseline_candidate.candidate_id,
        evaluation_scenarios=evaluation_scenarios,
        active_metrics=tuple(metrics_cfg.active_metrics),
        primary_metric_id=metrics_cfg.objective.primary.metric_id,
    )
    frozen_result_hash_value = result_hash(
        {
            "candidate_id": baseline_candidate.candidate_id,
            "parameters_si": baseline_candidate.parameters_si,
            "worst_case": worst_case_value,
            "per_scenario": per_scenario,
            "constraints": constraints,
            "model_package_hash": frozen_model_package_hash,
            "constraints_hash": constraints_hash_value,
            "metrics_hash": metrics_hash_value,
            "scenario_set_hash": scenario_set_hash_value,
        }
    )

    store.freeze_baseline(
        gate_key=gate_key,
        task_id=resolved_gate_task_id,
        passed=True,
        frozen_result_hash=frozen_result_hash_value,
        diagnosis_ref=None,
    )
    store.finish_task(
        resolved_gate_task_id,
        stop_reason=_BASELINE_GATE_PASSED_STOP_REASON,
        cause=None,
    )

    return BaselineGateStatus(
        passed=True,
        frozen_result_hash=frozen_result_hash_value,
        evidence_bound=evidence_bound,
        diagnosis_ref=None,
        gate_key=gate_key,
    )
