"""poweragent/sim/simulate.py

`simulate()` 只读执行（`design.md` §6.3.2 / §6.3.3；`tasks.md` T5 / 任务 7.2；
需求追溯 R4.4, R4.5, R4.6, R4.7, R4.9, R5.6；属性 CP-3）。

本模块实现 design.md §6.3.2 给出的函数：

    def simulate(candidate: Candidate, scenario: ScenarioSpec, *,
                 model_cfg: ModelConfig, session: MatlabSession,
                 run_id: str, artifacts: ArtifactStore) -> SimulationResult:
        \"\"\"运行时注入参数，绝不写模型文件。require_margin 场景内联执行线性分析/
        频响估计，其 engine 启动计入返回值的 engine_starts。\"\"\"

## 与 design.md 字面签名的五处偏离（均为显式、必要、最小的接口扩展）

design.md §6.3.2 给出的签名是「这个函数做什么」的说明，不是完整的可执行接口——
它省略了三类只有在真正落地时才会暴露的必需输入。本模块沿用任务 10.1
（`vout_target_v`）与本文件姐妹任务已确立的先例：**上游一次性算好的值，以显式
关键字参数向下游传入，而不是让下游重新计算或"凑巧从某个全局状态里拿到"**。

1. **`frozen_fingerprint: str`（必填 kwonly）** —— design.md §6.3.3：「每次
   `simulate()` 前做 `fast_fingerprint`，不一致即升级为全量重算」。这个"冻结值"
   按定义产生于 `run_task()` 入口（`requirements.md` R4.6：「与 `run_task()`
   入口时对同一闭包计算并冻结的值比对」），而 `run_task()` 本身（任务 14.4/14.5）
   在本任务执行时尚未实现。`simulate()` 没有任何机制可以"自己知道"这个值——
   它不持有任务级状态、不访问 SQLite——因此必须由调用方在每次调用时显式传入。
   这与"forbidden：sim 包不做业务判定"的模块边界（design.md §2.3）一致：判断
   "冻结值是什么"是编排层的职责，`simulate()` 只负责拿到冻结值后去比较。

2. **`frozen_model_package_hash: str`（必填 kwonly）** —— 同一理由，用于全量
   重算后的escalation 比对（R4.7：「全量 `model_package_hash` 重算后仍不一致」
   所比对的对象）。`run_task()` 入口也会冻结这个全量哈希（它与 `freezes` 表
   `kind='model_package'` 行的值一致，但 `simulate()` 不读 `freezes` 表——
   读表是 `preflight()` 的职责，`simulate()` 只读调用方给的值）。

3. **`task_id: str`（必填 kwonly）** —— `ArtifactStore.stage()`（任务 8.2）的
   签名是 `stage(task_id, kind, name)`，`task_id` 是必需参数。design.md §6.3.2
   的字面签名里没有 `task_id`（只有 `run_id`），但 `run_id` 与 `task_id` 是两个
   不同的标识符（一个任务下有多个 run）。产物目录布局（design.md §2.2）按
   `<task_id>/<kind>/<name>` 分层，因此必须知道 `task_id` 才能落盘。

4. **`base_dir: str | Path = "."`（可选 kwonly）** —— `sim.hashing.
   resolve_dependency_closure()`（任务 3.1）的签名要求一个 `base_dir` 用于把
   `model.yaml` 里的相对路径解析为绝对路径。design.md §6.3.2 的字面签名同样没有
   这个参数。给出默认值 `"."`（当前工作目录）是因为大多数调用场景下 CLI 进程的
   CWD 就是仓库根目录（`model.yaml` 里的相对路径本就是相对仓库根目录书写的，
   见 `configs/model.yaml` 模板的 `models/buck_12ph.slx` 等路径），因此不强制
   调用方每次都显式传入；需要非默认工作目录的调用方（例如测试）可覆盖。

5. **`metrics_cfg: MetricsConfig`（必填 kwonly，任务 11.2 新增）** —— design.md
   §6.3.2 字面签名同样没有这个参数，但本任务（11.2）要求验证
   `require_margin=true` 时 `SimulationResult.engine_starts >= 1 +
   metrics_cfg.margin_extraction.extra_engine_starts_per_candidate` 这条
   requirements.md R8.4 的后置条件——这个数值只存在于 `MetricsConfig.
   margin_extraction.extra_engine_starts_per_candidate`（`config/schema.py`
   任务 5.1，`Field(ge=0, le=4)`），`simulate()`/`simulate_batch()` 在任务 11.2
   之前完全没有任何途径读到它，因此无法校验这条后置条件，必须新增形参。

   **传"整个 `MetricsConfig`"而不是只传抽取出的两个 int/字符串值：这是一个
   显式的先例选择，两个先例都存在于本代码库、且指向相反方向，此处说明为何
   选前者。** 先例 (a)：`model_cfg: ModelConfig` 本就整节传入本模块（而不是
   拆成 `model_path`/`io_contract` 等散装参数），本模块内部按需访问其子字段。
   先例 (b)：`eval/metrics.py` 的 `compute_metrics()` 与 `eval/constraints.py`
   的 `judge()` 都是接**抽取出的具体值**（`guard`、`vout_target_v`）而不是整个
   `MetricsConfig`/`ModelConfig`。本模块选择先例 (a)（整节传入），理由：
     - `metrics_cfg.margin_extraction` 除本任务实际读取的
       `extra_engine_starts_per_candidate` 与 `primary_method` 外，还有
       `cross_check_method` / `cross_check_tolerance` / `cross_check_record`
       三个字段——这些字段本模块不读（裕量交叉核对是 `eval/margin.py` 任务
       11.4 与开发期一次性工作流的职责，见下方"`margin_cfg_json` 第四实参"
       一节），但它们与本模块确实读取的两个字段同属`margin_extraction`
       这一个内聚的配置节，未来若 `matlab/+pa/run_linear_analysis.m` 需要
       更多该节字段（例如把 `cross_check_tolerance` 也塞进 MAT 产物做
       事后可追溯记录），改动点在"如何构造 `margin_cfg_json`"这一处，不
       需要改 `simulate()`/`simulate_batch()` 的签名再加一个新形参。
     - 先例 (b) 之所以拆成散装值（`guard`/`vout_target_v`），是因为那两个值
       各自来自**不同**的配置文件/配置节（`guard` 来自
       `model.yaml.runtime.divergence_guard`、`vout_target_v` 来自
       `model.yaml.io_contract`），拆开传递能让调用方一眼看出这个函数依赖
       哪两个具体值、不必读全部 `ModelConfig`。本任务的情形不同：本模块
       需要的字段全部落在同一个子节（`metrics_cfg.margin_extraction`）内，
       没有"来自多个不相关配置节、拆开更清晰"的理由，整节传入不会引入
       额外的歧义。
     - 传整节而非只传 `extra_engine_starts_per_candidate: int` 单值，还避免
       了"两个签名都叫 `metrics_cfg` 却一个是 `MetricsConfig` 一个是
       `int`"的命名混淆——`eval/margin.py` 的 `extract_margin()`（任务 11.3）
       已经使用 `metrics_cfg: MetricsConfig` 这个形参名，本模块新增同名
       形参、同一类型，读者不需要跨模块记两套`metrics_cfg` 的类型。

以上五项均标注给未来 `run_task()` 落地时的接线者（任务 14.4/14.5）：那里需要
在任务开始时计算并冻结 `frozen_fingerprint` / `frozen_model_package_hash`
一次，随后在每次 `simulate()` 调用时原样传入本函数；`metrics_cfg` 同样由
`run_task()` 入口读取任务绑定的 `metrics.yaml` 后原样传入，`simulate()` 不
读取配置文件、不持有 `Store` 引用去查 `metrics_hash`。

## `model_cfg` 的类型：`ModelConfig`（pydantic）与 `sim.hashing` 的 `Mapping` 接口之间的接缝

design.md §6.3.2 把 `model_cfg` 标注为 `ModelConfig`（`config/schema.py` 的
pydantic v2 模型，任务 5.1）；而 `sim/hashing.py`（任务 3.1）的
`resolve_dependency_closure()` / `model_package_hash()` 签名接受
`Mapping[str, Any]`（普通字典结构，不认识 pydantic 模型）。这是两个任务各自
独立落地时天然出现的接口接缝，本模块在此处显式弥合：**每次调用 `simulate()`
时，先用 `model_cfg.model_dump(mode="json")` 把 pydantic 模型转换成
`sim.hashing` 期望的纯字典结构，再传给 `resolve_dependency_closure()` /
`model_package_hash()`**。选择在 `simulate()` 内部转换（而不是要求
`sim.hashing` 改为认识 pydantic 模型、或要求调用方预先转换）的理由：
`sim.hashing` 的 `Mapping[str, Any]` 类型标注本就足够宽松，接受 `model_dump()`
产出的普通 dict 不需要改动那个模块；把转换收在 `simulate()` 内部，是因为
`simulate()` 本就需要同时使用「pydantic 模型」（`model_cfg.model_package.
switching.entry` 之类的属性访问，用于确定 `model_path`）与「字典结构」（喂给
`sim.hashing`），两种形态都要，转换点自然落在这里。

`mode="json"` 而非默认的 `mode="python"`：`model_config.hashing.canonical_json`
（`model_package_hash()` 内部调用）只认识 JSON 兼容的标量类型（`str` / `bool`
/ `int` / `float` / `None` / `Mapping` / `Sequence`），`model_dump(mode="json")`
保证输出全部是这些类型（例如 `Path` 会被转成 `str`），避免 `canonical_json`
遇到无法识别的对象类型而抛 `TypeError`。

## MATLAB Engine 返回值形状的假设（无法用真实 Engine 验证，显式记录）

`session.call("pa.simulate_once", ...)` 通过 `MatlabSession.call()` → 
`self._engine.feval(fn, *args, nargout=nargout)` 发起调用（`sim/engine.py`）。
`feval` 返回的 MATLAB struct 在 Python 侧的具体形状（是原生 `dict`、是支持
`__getitem__` 的 `matlab.engine` 代理对象、还是需要属性访问的对象）取决于
已安装的 MATLAB Engine for Python 版本与 MATLAB 端 `simulate_once.m` 返回的
struct 结构，而本环境的 Simulink 许可证当前不可用（见任务上下文），无法用
真实 Engine 验证。本模块因此实现一个防御性的字段访问助手 `_field()`：优先
尝试 `result[name]`（覆盖 `dict` 与任何支持下标访问的代理对象），失败
（`TypeError` / `KeyError` / `IndexError`）时回退到 `getattr(result, name)`
（覆盖需要属性访问的对象）。这个假设未经真实 Engine 验证，标注为待
MATLAB Contract 测试层（任务 2.5 / 7.4 一类的 `[需 MATLAB]` 任务）用真实
返回值核对的项。

## 模型不变性检查（CP-3，本任务的核心）：两级、成本递增、只在需要时升级

design.md §6.3.3：「模型不变性检查分两级以控制成本：`preflight()` 做全量
`model_package_hash`；每次 `simulate()` 前做 `fast_fingerprint`，不一致即
升级为全量重算，仍不一致则 `stop_and_ask_human(cause='model_mutated_
during_optimize')`。」实现为三步、逐级升级，**指纹匹配的常见路径完全不触碰
全量哈希**（不逐次重新读取闭包内每个文件的完整内容——那正是"全量"要控制的
成本）：

  1. 计算 `fast_fingerprint(closure_paths)`，与 `frozen_fingerprint` 比较。
     **相等 ⟹ 直接继续执行，不计算 `model_package_hash`、不触发停止**
     （R4.9：「继续执行本次 `sim.simulate()`，且不触发 `stop_and_ask_human`」——
     这是最常见的路径：闭包文件的 `(size, mtime_ns)` 与任务开始时一致，说明
     大概率没人动过模型包，没必要为了确认这一点去重新读取并摘要全部文件字节）。
  2. 不相等 ⟹ 升级：计算全量 `model_package_hash(closure_paths, model_dump)`，
     与 `frozen_model_package_hash` 比较。
     - 相等（**仅时间戳被触碰、内容与成员集合未变**，正是 `fast_fingerprint`
       与 `model_package_hash` 非等价性——任务 3.2 的属性——存在的意义）⟹
       视为假警报，**继续执行，不触发停止**（R4.9 同样覆盖这条路径：条文中
       「`fast_fingerprint` 与冻结值不一致而全量重算后与冻结值一致」）。
     - 不相等（**内容或闭包成员集合确实变了**）⟹ 真实的模型突变：**不发起
       本次 Simulink 调用**、抛出 `ModelMutatedDuringOptimizeError`（见下方
       "谁负责 stop_and_ask_human"一节），函数在这一步返回之前不做任何其他
       副作用（不调用 `session.call()`、不调用 `artifacts.stage()`）。

## 谁负责 `stop_and_ask_human`：`simulate()` 只负责拒绝并说明原因，不碰数据库

requirements.md R4.7 与本任务描述都把"以 `stop_and_ask_human(cause=
'model_mutated_during_optimize')` 停止、不写任何仿真结果行"写在同一句里，
但这是 `run_task()`（尚未实现，任务 14.4/14.5）层面的**控制流后果**，不是
`simulate()` 自身的职责：`simulate()` 不写数据库（它甚至不持有 `Store` 的
引用——design.md §6.3.2 的字面签名里没有 `store` 参数），因此它没有能力
"不写任何仿真结果行"这件事本身——它能做的只是"不产生任何东西可写"。
本模块把这条控制流实现为**异常**：`simulate()` 在检测到真实模型突变时抛出
`ModelMutatedDuringOptimizeError`（而不是返回某种"失败态"的 `SimulationResult`
——`SimulationResult.status` 的五值枚举里没有任何一个值语义上表示"我们甚至
没有尝试仿真"，硬塞一个会污染那个枚举的既定含义）。调用方（`run_task()`）
捕获这个异常后，自行决定：不发起 `sim.simulate()` 的后续重试、把它翻译成
`stop_and_ask_human(cause='model_mutated_during_optimize')` 的控制流、并且
（因为从未调用 `open_run()`）天然不会写入任何 `runs` 行——"不写任何仿真结果行"
是"从未开始写"的自然结果，不需要 `simulate()` 主动去"不写"什么。

## 依赖闭包"逐字节不变"的后置条件：不做，因为不需要

requirements.md R4.4 与本任务描述都要求"依赖闭包内每个文件在返回后逐字节不变"。
本模块不在 `simulate()` 返回前再计算一次哈希去验证这件事，理由：

  - `simulate()` 的实现路径里**没有任何写文件的代码**（不调用 `save_system`
    及任何等价写入 API；对模型的访问全部通过 `session.call("pa.simulate_once",
    ...)` 转交 MATLAB 侧，MATLAB 侧的 `+pa` 包同样只读打开模型、经模型工作区
    注入运行时参数）。"逐字节不变"是"从不写入"的自然推论，不是需要在事后
    用哈希去确认的独立性质——若真的动手验证，需要在调用前后各做一次
    `model_package_hash()`，这恰恰是任务描述本身警告过的"昂贵且冗余"
    （design.md §6.3.3 的"控制成本"设计意图）。
  - 真正兜底这条不变量的机制是**下一次** `simulate()` 调用前的 `fast_fingerprint`
    检查（本函数第一步）：如果本次调用真的意外写坏了闭包内的文件，下一次调用
    的指纹比对会捕捉到并升级到全量哈希，从而在下一次调用时被发现——这正是
    两级设计本身的意图，不需要每次调用都自证一次。

## `require_margin` 场景的内联裕量采集：与 `matlab/+pa/run_linear_analysis.m`（任务 11.1）的软依赖

design.md §6.3.2 docstring 明确要求："`require_margin` 场景内联执行线性分析/
频响估计，其 engine 启动计入返回值的 `engine_starts`"。本模块因此在
`status='ok'` 且 `scenario.require_margin` 为真时，额外发起一次
`session.call("pa.run_linear_analysis", ...)`。

`matlab/+pa/run_linear_analysis.m`（任务 11.1）与本任务在同一批次并行派发，
撰写本文件时尚未落地（`matlab/+pa/` 目录下不存在该文件）。本模块按
`tasks.md` 任务 11.1 登记的返回值契约（"返回 `status` / `freq_response_path`
/ `method` / `engine_starts`"）与 `pa.simulate_once` 已有的三参数惯例
（`model_cfg_json, params_json, scenario_json`）编写调用点：MATLAB 侧函数
的确切参数顺序/命名一旦在任务 11.1 落地后与本文件假设不一致，需要同步调整
本模块的调用点（软依赖，与 `sim/hashing.py` 顶部对 `config/hashing.py` 的
软依赖说明是同一模式）。

裕量采集失败（例如 `freq_response_path` 为空）时，本模块只是让
`observable_ref` 保持 `None`、不额外报错——把"采集/提取失败后如何触发
`candidate_rejected` 与连续失败熔断"的完整控制流留给任务 11.3
（`eval/margin.py extract_margin()` 及其分层控制流），本任务（7.2）只负责
"发起这次调用、把它的 engine 启动计入返回值"这一层最小接线。

## 任务 11.2 补丁一：`pa.run_linear_analysis` 调用点缺失的第四个实参
`margin_cfg_json`（真实缺口，非本任务新引入）

任务 7.2 撰写本文件时，`matlab/+pa/run_linear_analysis.m`（任务 11.1）尚未
落地（见上方"与 `matlab/+pa/run_linear_analysis.m`（任务 11.1）的软依赖"
一节），本模块因此按彼时唯一可比照的惯例（`pa.simulate_once` 的三参数形状）
写下 `session.call("pa.run_linear_analysis", model_cfg_json, params_json,
scenario_json, nargout=1)`——只传三个 JSON 参数。任务 11.1 落地后，
`run_linear_analysis.m` 的**实际**签名是四参数：

    function res = run_linear_analysis(model_cfg_json, params_json, scenario_json, margin_cfg_json)

第四个参数 `margin_cfg_json` 解码后必须含 `primary_method` 字段（`.m`
文件内 `primary_method = margin_cfg.primary_method` 一行，用于在
`linear_analysis_on_averaged` 与 `freq_response_estimator_on_switching`
两个分支间路由）——本文件此前的三参数调用点完全没有传这个参数，属于任务
7.2 遗留的真实调用点缺口（本任务范围内的 bug，不是任务 11.2 新引入的
需求），本任务在此修复：两个调用点（`simulate()` 步骤 4、`simulate_batch()`
步骤 4）均新增构造并传入 `margin_cfg_json`，取值为

    margin_cfg_json = json.dumps({"primary_method": metrics_cfg.margin_extraction.primary_method})

只含 `primary_method` 一个键（`run_linear_analysis.m` 实际只解码这一个
字段），不塞入 `margin_extraction` 节的其余四项（`cross_check_method` /
`cross_check_tolerance` / `extra_engine_starts_per_candidate` /
`cross_check_record`）——那四项分别属于开发期一次性交叉核对（`eval.margin.
cross_check_margin()`，任务 11.4）与本模块自己的后置条件校验（见下一节），
`.m` 文件当前的实现不读取、也不应该被诱导去读取它们（往 MAT 落盘产物里塞
用不到的字段只会让"这个 JSON 里到底哪些字段有效"变得模糊）。

## 任务 11.2 补丁二：`require_margin=true` 时 `engine_starts` 后置条件
（R8.4）—— 断言而非静默信任

requirements.md R8.4 与本任务描述都要求："`require_margin` 为真时
`sim.simulate` SHALL 返回 ... 不小于 `1 + extra_engine_starts_per_candidate`
的 `SimulationResult.engine_starts`"。这是一条**关于最终返回值的后置条件**
（`1` 来自 `pa.simulate_once` 自身的一次仿真启动，`extra_engine_starts_per_
candidate` 描述"每候选额外仿真启动数"这个探针实测值——`extra_engine_starts_
per_candidate` 这个名字本身就是"每候选"而非"每次裕量采集"的粒度，但对单次
`simulate()`/`simulate_batch()` 调用而言，每次调用产生的额外启动数就是这个
探针值，此处按调用层面校验），不是本函数能够单方面"制造"出来的东西——
`margin_result.engine_starts` 是 MATLAB 侧 `run_linear_analysis.m` 实际执行
`linearize`/`frestimate` 的真实计数，本函数唯一能做的是**读取**它、**加总**
到返回值的 `engine_starts`，而不能凭空把它改大以满足这条后置条件（伪造一个
不真实的启动计数会让 `BudgetLedger` 的预算记账依据一个虚假值，比不校验这条
后置条件更糟）。

本模块的选择：**在计入之后，对总和做一次防御性断言**——若 `status == 'ok'
and scenario.require_margin` 为真而累加后的 `engine_starts <
1 + metrics_cfg.margin_extraction.extra_engine_starts_per_candidate`，抛出
新增的 `MarginEngineStartsUnderreportedError`（而不是静默返回一个违反
R8.4 后置条件的 `SimulationResult`）。这是一个显式的权衡记录：

  - **考虑过、放弃的方案：静默信任 MATLAB 侧上报值，不做本函数内校验。**
    放弃理由——若 `run_linear_analysis.m` 因为某个未来的重构（例如把
    `engine_starts` 的计数口径改错、或某个分支忘了 `engine_starts =
    engine_starts + 1`）导致上报值系统性偏低，这个偏差会一路穿透到
    `BudgetLedger.reserve()` 的预算记账（`requirements.md` R15.7：预算先占
    量按 `1 + extra_engine_starts_per_candidate` 计算，与本函数返回的
    `engine_starts` 是两个独立计算出的数字，理应相等但没有任何运行期机制
    验证它们确实相等）——直到某次预算对账或事后审计时才会被发现，此时早已
    没有任何线索定位是哪次调用、哪个分支的问题。这与本代码库已确立的
    `PreflightError`"不降级放行"哲学、以及 `ModelMutatedDuringOptimizeError`
    "检测到问题就拒绝执行而不是返回一个语义不完整的结果"的既有模式相反。
  - **本模块选择的方案：断言 + 显式异常。** 让这条后置条件的违反在**发生的
    那一次调用**就变成一个清晰的、指名道姓的异常（异常消息含实际
    `engine_starts`、期望下界、`run_id`），而不是一个静默传播的数据质量
    问题。代价——这确实是"用运行时断言表达一个本应由类型系统或契约测试保证
    的性质"，如果未来 MATLAB Contract 测试层（任务 2.5 一类）能在 `.m`
    文件层面直接验证 `engine_starts` 计数的正确性，本函数内的这层断言会
    变成冗余的双重保险；但目前 `run_linear_analysis.m` 没有任何契约测试
    覆盖（本环境 Simulink 许可证不可用，任务 11.1 的 `[需 MATLAB]` 一类
    验证尚未发生），这层断言是唯一能在 Python 侧捕获该问题的机制，因此
    保留而不是省略。
  - 断言的落点是**总和**（`engine_starts`，已加总 `pa.simulate_once` 的
    `1` 与 `margin_engine_starts`），不是单独校验 `margin_engine_starts`
    本身——R8.4 的原文本身就是对 `SimulationResult.engine_starts`（返回值
    整体）的约束，不是对裕量采集这一步单独提出的约束（`pa.simulate_once`
    本身在极端情况下也可能不止启动一次引擎，尽管当前 `.m` 实现固定为
    `engine_starts = 1`——校验总和而非分项，与需求原文的约束对象一致）。
  - `status != 'ok'`（`pa.simulate_once` 本身失败，未进入裕量采集分支）或
    `scenario.require_margin` 为假时，不做这条校验——R8.4 的前提是"场景的
    `require_margin` 为真"，`status != 'ok'` 时函数根本不会进入裕量采集
    这段代码（见上方步骤 3/4 的条件判断），这条断言天然只在裕量采集确实
    发生过之后才检查。

## 任务 11.2 确认一：运行期不存在调用 `cross_check_method` 或
`eval.margin.cross_check_margin` 的代码路径

requirements.md R8.2 与本任务描述都要求"系统中不存在在 `run_task()` 执行期
调用 `cross_check_method` 或 `eval.margin.cross_check_margin` 的代码路径"。
本文件（`sim/simulate.py`）满足这条要求，且满足方式是**简单的缺省**（结构性
不存在，不是加了一段"跳过交叉核对"的分支逻辑）：本文件全文没有任何一处
`import poweragent.eval.margin`、没有任何一处引用 `cross_check_method` 字段、
没有任何一处调用名为 `cross_check_margin` 的函数（已用 `grep` 核对：搜索
`cross_check` 在本文件内零命中）。本文件唯一发起的裕量相关调用是
`session.call("pa.run_linear_analysis", ...)`（对应 `margin_extraction.
primary_method` 指定的采集方法），与 `cross_check_method`（`metrics.yaml`
`margin_extraction.cross_check_method` 字段，供 `eval.margin.cross_check_margin`
——任务 11.4，开发期一次性工作流——使用）是两个完全不相交的代码路径。

## 任务 11.2 确认二："采集产物随 `simulation_key` 缓存"——`simulate()` 自身
不实现缓存逻辑，缓存发生在调用方

design.md §8.4 的 `run_tier` 伪代码把缓存查询（`store.find_cached_simulation
(sim_key)`）放在调用 `sim.simulate()` **之前**：`simulate()` 只在缓存未命中
时才会被调用（见 `store/repo.py` 的 `find_cached_simulation()`，任务 8.3/9.x
已落地）。这意味着"采集产物随 `simulation_key` 缓存"这条要求，不是
`simulate()` 内部需要新增代码去实现的东西——`simulate()` 的整份返回值
（含裕量场景的 `observable_ref`）就是调用方在缓存未命中时唯一拿到的产物，
调用方随后把这份返回值（连同计算出的 `simulation_key`）一并写入 `runs` 行
（`store.repo.open_run()` 的 `simulation_key` 列），下次同一 `(候选, 场景)`
再次请求时，`store.find_cached_simulation()` 按该键查到这条 `runs` 行、直接
复用其 `observable_ref`，短路掉本次 `simulate()`/`session.call()` 调用。

这条缓存语义能够覆盖裕量产物，前提是 `store/cache.py` 的 `simulation_key()`
（任务 9.1，已落地）在字段集合中**条件性**含 `margin_primary_method`
（`scenario.require_margin` 为真时取 `metrics_cfg.margin_extraction.
primary_method`，否则取 `None`——已用 `grep` 核对 `store/cache.py` 的
`simulation_key()` 签名与 docstring，字段确实存在且语义与此处描述一致）。
这是本模块与 `store/cache.py`（不同任务批次落地）之间的一处跨模块设计一致性
确认，**不要求本文件新增任何代码**：`simulate()` 不计算 `simulation_key`、
不查询也不写入缓存，那两侧都是调用方（未来的 `run_task()`，任务 14.4/14.5）
的职责，本文件只需要保证它的返回值（`observable_ref` 等）本身是"可被缓存
复用的、内容确定的产物"，这一点已经成立（同一份已落盘 MAT 文件的 artifact
引用，不含任何随调用变化的非确定性内容）。

## `params_json` / `scenario_json` 不使用 `canonical_json`

`matlab/+pa/simulate_once.m` 对三个 JSON 参数只做 `jsondecode`，不比较、不
哈希它们的文本形式——跨 MATLAB 边界不存在"规范化形式"的要求（`canonical_json`
的存在理由是让同一内容在不同环境下产生*相同的哈希*，而这里没有哈希需求）。
本模块因此用普通 `json.dumps()` 序列化，不引入 `config.hashing.canonical_json`
这一跨边界不必要的依赖。

## 任务 7.3：`simulate_batch()` 与 `inspect_model()` 封装

design.md §6.3.2 给出的这两个函数的字面签名：

    def inspect_model(model_cfg: ModelConfig, variant: ModelVariant) -> ModelInspection: ...

    def simulate_batch(requests: Sequence[tuple[Candidate, ScenarioSpec]], *,
                       model_cfg: ModelConfig, session: MatlabSession,
                       artifacts: ArtifactStore) -> list[SimulationResult]:
        \"\"\"映射方式由 model_cfg.runtime.execution_mode 统一决定，调用方不指定。\"\"\"

与 `simulate()`（任务 7.2）已确立的先例相同，本任务在落地时对字面签名做了显式、
最小的必要扩展；以下只记录**新增的**偏离，与 `simulate()` 已登记的四项共享同一
理由（`task_id` / `frozen_fingerprint` / `frozen_model_package_hash` / `base_dir`
的必要性不再重复说明，见上方对应小节）。

### `ModelInspection`：新增的 frozen dataclass

design.md 未在别处给出 `ModelInspection` 的字段定义，本模块按
`matlab/+pa/inspect_model.m`（任务 2.1，已落地）的实际返回 struct 设计：

    status            - MATLAB 侧固定为 'ok'
    model_path         - 回填的模型路径
    checked_params     - 已校验通过的 injectable_params 名称列表（cell）
    checked_signals    - 已校验通过的 output_signals 名称列表（cell）

`inspect_model.m` 的控制流是「全部校验通过才返回，任一 Block Path 或 logsout_name
不存在就抛 `pa:ContractError`」——它不存在"返回失败状态"的路径，`status` 字段在
能被 Python 侧读到的场景下**恒为 `'ok'`**（因为一旦有失败就是异常传播，函数根本
不会走到 `return`）。本模块因此**不在 `ModelInspection` 里保留 `status` 字段**：
一个只可能取一个值、且这个值仅在"函数正常返回"这一已经蕴含该值的前提下才存在的
字段，携带零信息量，保留它只会诱使调用方写出 `if inspection.status == 'ok':` 这样
的死分支（`inspect_model()` 正常返回本身就是"ok"的证明，失败已经是异常）。

    @dataclass(frozen=True, slots=True)
    class ModelInspection:
        model_path: str
        checked_params: Sequence[str]
        checked_signals: Sequence[str]

放在本模块（而非 `sim/hashing.py`）：`inspect_model()` 函数本身就定义在
`sim/simulate.py`（design.md §6.3.2 的模块归属），其返回类型放在同一文件里，
不额外新增模块。

### `inspect_model()` 的 `session` 参数：与 `simulate()`「必填 kwonly 扩展」同类偏离

design.md 字面签名 `inspect_model(model_cfg, variant) -> ModelInspection` 没有
`session` 参数，但调用 `pa.inspect_model` 必须经由 `MatlabSession.call()`——
`inspect_model()` 没有任何其他途径触达 MATLAB Engine（它不像 `simulate()` 那样
有 `run_id`/`task_id` 之类看起来"可能从别处推断"的参数，`session` 是纯粹缺失的
必需依赖）。本模块把它加为必填 kwonly 参数：

    def inspect_model(model_cfg: ModelConfig, variant: ModelVariant, *,
                      session: MatlabSession) -> ModelInspection: ...

### `inspect_model()` 不捕获 `ContractError`：转译是调用方的职责

任务描述原文："Block Path 或信号名不存在 ⟹ MATLAB 侧 `ContractError` →
`stop_and_ask_human(cause='model_systemic_error')`"——与 `simulate()` 对
`ModelMutatedDuringOptimizeError` 的处理是同一种分工（见上方"谁负责
stop_and_ask_human"一节）：`sim` 层只管拒绝/传播并说明原因，**把异常翻译成
`stop_and_ask_human` 控制流是调用方（`controller`）的职责**。本函数因此不设
`try`/`except` 包裹 `session.call("pa.inspect_model", ...)`：`pa:ContractError`
（在 Python 侧具体表现为 `matlab.engine` 对 MATLAB 端 `error()` 调用抛出的
异常——很可能是 `matlab.engine.MatlabExecutionError`，但如模块顶部"MATLAB
Engine 返回值形状的假设"一节所述，本环境当前无法用真实 Engine 验证这个具体
异常类型）原样向上传播，`inspect_model()` 不捕获、不重新分类、不包装。

### `simulate_batch()` 的 `run_ids` 参数：批量场景下的必要扩展

design.md 字面签名的 `simulate_batch()` 没有为每个请求提供 `run_id` 的途径，但
`SimulationResult.run_id` 是必填字段，而 `run_id` 的产生方式与 `simulate()` 的
`run_id` 参数同理（同一候选/场景组合可能被跑多次，`run_id` 必须由调用方在每次
调用时分配，`sim` 层不生成标识符）。本模块因此新增必填 kwonly 参数
`run_ids: Sequence[str]`，与 `requests` 同长度、同顺序，`run_ids[i]` 对应
`requests[i]`。理由与 `simulate()` 顶部登记的三项扩展同源，此处不重复展开。

### `model_path`：放进每个请求对象，而不是共享的 `model_cfg_json`

`simulate_once.m` 的惯例是把（已由 Python 侧解析好的）`model_path` 塞进共享的
`model_cfg_json`，因为单次调用只有一个场景、只有一个 `model_variant`。批量调用
不满足这个前提：同一批 `requests` 里不同的 `(candidate, scenario)` 对可能声明
不同的 `scenario.model_variant`（`switching` 与 `averaged` 混合在一批里是合法
输入，`ScenarioSpec.model_variant` 逐场景声明，design.md 未规定同批必须同变体）。
design.md §6.4 给出的 MATLAB 侧签名是 `simulate_batch(model_cfg_json,
requests_json)`——只有一个 `model_cfg_json`，因此 `model_path` 不能放在这个
共享对象里，否则无法表达"批内不同请求解析到不同入口文件"。本模块的选择：
`model_path` 计算后放进 `requests_json` 数组的**每个元素**内（与 `params` /
`scenario` 同级），共享的 `model_cfg_json` 只承载 `model_cfg.model_dump()`
本身（不含任何 `model_path` 键）。`matlab/+pa/simulate_batch.m` 落地时需按此
约定从每个请求对象里取 `model_path`，而不是从共享 `model_cfg_json` 里取
（软依赖，见下方说明）。

### `matlab/+pa/simulate_batch.m` 尚不存在：新的软依赖，且无编号任务

design.md §6.4 给出了该函数的注释级签名（"按 execution_mode 选择 serial /
fast restart / parsim"），但 `tasks.md` 里没有任何一条任务显式登记
"实现 `matlab/+pa/simulate_batch.m`"——任务 2.x 只覆盖 `inspect_model.m` /
`apply_params.m` / `map_error.m` / `simulate_once.m`，任务 11.1 覆盖
`run_linear_analysis.m`。撰写本文件时 `matlab/+pa/` 目录下确认不存在
`simulate_batch.m`（已用 `list_directory` 核对）。这意味着：

  - `execution_mode`（`serial` / `fast_restart` / `parsim`）当前**没有任何
    MATLAB 侧实现**——`serial`/`fast_restart`/`parsim` 三者的分支逻辑完全
    不存在，`model.yaml` 的 `runtime.execution_mode` 字段目前只是一个配置项，
    没有代码读取并据此改变行为。
  - 本模块（Python 侧 `simulate_batch()`）按 design.md §6.3.2 docstring
    的字面要求（"映射方式由 execution_mode 统一决定，调用方不指定"）实现：
    Python 侧不读 `execution_mode`、不做任何 serial/fast_restart/parsim 的
    分支判断，只把请求整体转交 `pa.simulate_batch` 一次调用，把"按
    execution_mode 选择分支"完全留给 MATLAB 侧（design.md §6.4 明确该函数
    的职责就是这件事）。**这是有意为之，而不是遗漏**——Python 侧没有
    `execution_mode` 分支代码是符合设计的，缺的是 MATLAB 侧的对应实现。
  - 这与本文件对 `run_linear_analysis.m`（任务 11.1）的软依赖是同一模式：
    按 `tasks.md` 与 design.md 登记的契约编写调用点，实际 `.m` 文件由并行
    排期的另一任务落地。区别在于 `run_linear_analysis.m` 至少有编号任务
    （11.1），而 `simulate_batch.m` 目前**没有任何编号任务**——这是一个需要
    向用户/任务规划者标记的缺口，本任务不擅自新增任务编号，只在此处与执行
    报告中明确指出。

### 批量场景的 `require_margin` 处理：与 `simulate()` 保持一致的裕量采集

design.md §6.4 给出的 `simulate_batch` MATLAB 签名（`model_cfg_json,
requests_json`）里没有任何裕量/线性分析相关的参数或返回字段，`pa.simulate_batch`
的职责被限定为"按 execution_mode 选择执行方式"，不提及裕量采集。但
`simulate()`（任务 7.2）对 `scenario.require_margin=true` 的场景会内联调用
`pa.run_linear_analysis`——同一个场景本身的"是否需要裕量"是场景本身的属性
（`ScenarioSpec.require_margin`），不应该因为"这次是通过 `simulate()` 还是
`simulate_batch()` 跑的"而产生不同的观测量集合（否则同一场景两种调用路径产生
不可比的证据链，`evaluation_key` 的缓存语义也会因此失去意义）。本模块的选择：
批量结果拿到之后，对批内每个 `scenario.require_margin=True` 且该请求结果
`status='ok'` 的条目，**额外单独发起一次** `session.call("pa.run_linear_analysis",
...)`（逐个 request 单独调用，不是批量调用的一部分——`pa.simulate_batch` 的
签名本就没有为它留位置）。这是一个刻意的、可复议的一致性选择：如果未来
`pa.simulate_batch` 的 MATLAB 侧实现扩展为原生支持批量裕量采集（例如
`requests_json` 每项自带 `require_margin` 标记、`pa.simulate_batch` 内部
一并跑完 `run_linear_analysis`），这里的"逐个额外调用"应当被替换为读取批量
返回值里的对应字段，不再单独调用。

### `_check_model_not_mutated` / `_stage_waveform`：从 `simulate()` 提炼的共享私有函数

`simulate()` 与 `simulate_batch()` 都需要「同一套三步模型不变性检查」与
「同一套波形产物落盘逻辑」。本次改动把这两段逻辑从 `simulate()` 的函数体中
提炼为两个模块级私有函数（`_check_model_not_mutated()` / `_stage_waveform()`），
`simulate()` 相应改为调用它们；这是发生在**同一个任务批次自己的文件**内的重构
（与任务 10.2 的 `_find_settle_index` 提炼是同一先例），不触碰其他任务已落地的
文件。提炼前后 `simulate()` 的外部行为不变（同样的输入产生同样的输出/异常），
仅内部实现路径改变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from poweragent.config.schema import MetricsConfig, ModelConfig
from poweragent.sim.backends.buck_model import STEP_TRIGGER_FRACTION
from poweragent.sim.engine import MatlabSession
from poweragent.sim.hashing import (
    fast_fingerprint,
    model_package_hash,
    resolve_dependency_closure,
)
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Candidate, ModelVariant, ScenarioSpec, SimulationResult

__all__ = [
    "ModelInspection",
    "ModelMutatedDuringOptimizeError",
    "MarginEngineStartsUnderreportedError",
    "inspect_model",
    "simulate",
    "simulate_batch",
]


class ModelMutatedDuringOptimizeError(RuntimeError):
    """依赖闭包在 Optimize 期发生真实内容/成员集合变更时抛出（`design.md` §6.3.3；
    需求 R4.7；属性 CP-3）。

    抛出前**不会**调用 `session.call()`（不发起本次 Simulink 调用）、不调用
    `artifacts.stage()` / `artifacts.commit()`。本异常只表示"拒绝执行并说明
    原因"；把它翻译为 `stop_and_ask_human(cause='model_mutated_during_optimize')`
    并确保不写任何 `runs` 行，是调用方（`run_task()`，尚未实现）的职责——见
    模块顶部"谁负责 stop_and_ask_human"一节。
    """


class MarginEngineStartsUnderreportedError(RuntimeError):
    """`require_margin=true` 场景下，加总后的 `engine_starts` 不满足
    requirements.md R8.4 的后置条件（`>= 1 + metrics_cfg.margin_extraction.
    extra_engine_starts_per_candidate`）时抛出（任务 11.2）。

    表示 MATLAB 侧 `pa.run_linear_analysis` 上报的 `engine_starts` 低于探针
    实测的预期额外启动数，属于 MATLAB 侧计数口径 bug 或本文件与 `.m` 文件之间
    的接线错误——见模块顶部"任务 11.2 补丁二"一节的详细权衡记录（断言而非
    静默信任）。抛出时对应的 `SimulationResult` **不会**被返回，调用方
    （未来的 `run_task()`）需要自行决定如何处理（本文件不猜测，只拒绝返回一个
    已知违反后置条件的结果）。
    """


@dataclass(frozen=True, slots=True)
class ModelInspection:
    """`inspect_model()` 的返回值（design.md §6.3.2；`matlab/+pa/inspect_model.m`
    任务 2.1 的实际返回 struct，字段一一对应）。

    不携带 `status` 字段：`inspect_model.m` 只在全部校验通过时才正常返回，任一
    Block Path 或 `logsout_name` 缺失都会抛 `pa:ContractError` 而不是返回一个
    失败态的 struct（见模块顶部"任务 7.3"一节的详细说明）。函数能正常返回本身
    就是"ok"的证明，因此本类型省略这个只可能取一个值的字段。
    """

    model_path: str
    checked_params: Sequence[str]
    checked_signals: Sequence[str]


def _check_model_not_mutated(
    model_dump: Mapping[str, Any],
    *,
    base_dir: str | Path,
    frozen_fingerprint: str,
    frozen_model_package_hash: str,
) -> None:
    """模型不变性检查（CP-3），从 `simulate()` 提炼、供 `simulate()` 与
    `simulate_batch()` 共用（design.md §6.3.3；需求 R4.6, R4.7, R4.9）。

    三步、逐级升级，详细理由见模块顶部"模型不变性检查（CP-3，本任务的核心）"
    一节：
      1. `fast_fingerprint` 与冻结值比较，相等则直接返回（不计算全量哈希）。
      2. 不等则升级为全量 `model_package_hash` 比对，相等则视为假警报，返回。
      3. 全量仍不等则真实模型突变：抛 `ModelMutatedDuringOptimizeError`，
         调用方在捕获前**不会**发生任何 `session.call()` / `artifacts.stage()`
         副作用（本函数本身不产生这类副作用，只做比较与可能的异常）。
    """
    closure_paths = resolve_dependency_closure(model_dump, base_dir=base_dir)
    current_fingerprint = fast_fingerprint(closure_paths)

    if current_fingerprint == frozen_fingerprint:
        # 指纹一致：直接返回，未计算全量哈希（常见/低成本路径）。
        return

    # 指纹不一致：升级为全量重算。
    current_full_hash = model_package_hash(closure_paths, model_dump)
    if current_full_hash != frozen_model_package_hash:
        # 全量仍不一致：真实模型突变。不发起本次 Simulink 调用。
        raise ModelMutatedDuringOptimizeError(
            "dependency closure changed during Optimize "
            f"(frozen fast_fingerprint={frozen_fingerprint!r} != "
            f"current={current_fingerprint!r}; frozen model_package_hash="
            f"{frozen_model_package_hash!r} != current={current_full_hash!r}); "
            "refusing to invoke pa.simulate_once/pa.simulate_batch"
        )
    # 全量一致：仅时间戳被触碰，假警报，继续执行（不触发停止）。


def _stage_waveform(
    artifacts: ArtifactStore, *, task_id: str, run_id: str, local_path: str
) -> str:
    """把 MATLAB 侧产出的本地波形文件经 `ArtifactStore` 提交为已归档产物，
    从 `simulate()` 提炼、供 `simulate()` 与 `simulate_batch()` 共用。

    只在调用方已确认 `status == 'ok'` 时调用；`local_path` 为空或不可读会让
    `artifacts.commit()` 的往返校验失败并抛异常（`ArtifactCommitError`），
    本函数不额外捕获。
    """
    tmp = artifacts.stage(task_id=task_id, kind="waveforms", name=f"{run_id}.mat")
    tmp.write_bytes(Path(local_path).read_bytes())
    return artifacts.commit(tmp)


def _check_margin_engine_starts(
    engine_starts: int, *, metrics_cfg: MetricsConfig, run_id: str
) -> None:
    """校验 R8.4 后置条件（任务 11.2 补丁二，详细权衡记录见模块顶部同名一节），
    从 `simulate()` 提炼、供 `simulate()` 与 `simulate_batch()` 共用。只在
    `status == 'ok' and scenario.require_margin` 均成立、裕量采集已发生之后
    调用，`engine_starts` 传入调用方已加总 `pa.simulate_once` 与
    `pa.run_linear_analysis` 两者的最终值。"""

    required = 1 + metrics_cfg.margin_extraction.extra_engine_starts_per_candidate
    if engine_starts < required:
        raise MarginEngineStartsUnderreportedError(
            f"run_id={run_id!r}: require_margin=true 场景下 engine_starts="
            f"{engine_starts} 低于 R8.4 要求的下界 1 + "
            f"extra_engine_starts_per_candidate={required}；MATLAB 侧 "
            "pa.run_linear_analysis 可能低报了 engine_starts，或存在接线错误"
        )


def _field(result: Any, name: str) -> Any:
    """防御性读取 MATLAB Engine `feval` 返回值的字段。

    优先尝试下标访问（覆盖 `dict` 与任何支持 `__getitem__` 的代理对象），失败时
    回退到属性访问（覆盖需要属性访问的对象）。见模块顶部"MATLAB Engine 返回值
    形状的假设"一节：该行为未经真实 Engine 验证。
    """
    try:
        return result[name]
    except (TypeError, KeyError, IndexError):
        return getattr(result, name)


def _resolve_model_path(
    model_dump: Mapping[str, Any], variant: str, *, backend_id: str
) -> str:
    """按场景声明的 `model_variant` 与**后端**从 `model_cfg.model_dump()` 中取入口
    文件路径，作为喂给后端 `model_cfg_json` 的扁平 `model_path` 键
    （`matlab/+pa/simulate_once.m` 期望的形状——见其文件头注释：
    "model_path（已由 Python 侧按 scenario 的 model_variant 解析出的入口文件
    路径）"）。`ModelConfig` 本身没有扁平的 `model_path` 字段，只有
    `model_package.<variant>.entry` / `.slx_entry` 两组按变体分列的字段，本函数是
    它们之间的解析点。

    ## 为什么要按后端分支

    两个后端仿真同一个电路的两种表示，入口是两个不同的文件（见 `config/schema.py`
    的 `ModelVariantPackage` docstring）：Python 后端读 `entry`（`models/*.yaml`，
    电路参数的声明式定义），MATLAB 后端读 `slx_entry`（`models/*.slx`）。把
    `.yaml` 路径交给 `load_system` 或把 `.slx` 交给 `load_buck_spec` 都会失败，
    且失败信息与真实原因相距很远，因此在这一处按 `backend_id` 显式选择。

    `backend_id` 取自调用方持有的会话对象的 `backend_id` 类属性，与
    `execution_env_hash` 用的是同一个值——"这次用了哪个后端"在系统里只有一个
    事实来源。

    MATLAB 后端而 `slx_entry` 缺失时显式报错，不回退到 `entry`：静默回退会把一个
    `.yaml` 路径传给 `load_system()`，得到的是"找不到系统或文件"，看起来像模型
    文件丢了，而真实原因是配置里没登记 `.slx`。
    """
    variant_section = model_dump.get("model_package", {}).get(variant)
    if not variant_section:
        raise ValueError(
            f"simulate(): scenario.model_variant={variant!r} 在 model_cfg."
            f"model_package 中没有对应的依赖闭包节（该变体可能未启用，例如 "
            f"averaged_model_required=false 时 model_package.averaged 留空）"
        )

    if backend_id == "matlab":
        slx_entry = variant_section.get("slx_entry")
        if not slx_entry:
            raise ValueError(
                f"simulate(): MATLAB 后端需要 model_package.{variant}.slx_entry，"
                f"但该字段为空。请在 model.yaml 中登记 .slx 入口"
                f"（可用 matlab/build_models.m 从 {variant_section['entry']!r} 生成）"
            )
        return str(slx_entry)

    return str(variant_section["entry"])


# `margin_extraction.primary_method` → 该方法要求的模型变体。裕量提取的对象由**方法**
# 决定，与触发它的场景声明的 `model_variant` 无关：`linear_analysis_on_averaged` 必须
# 在平均模型上线性化（开关模型的右端项在每个开关点不连续，在其上 `linearize` 得到的
# 是某个开关瞬间的线性化结果，没有环路裕量的意义），而
# `freq_response_estimator_on_switching` 按定义就在开关模型上做频响估计。
_MARGIN_METHOD_VARIANT = {
    "linear_analysis_on_averaged": "averaged",
    "freq_response_estimator_on_switching": "switching",
}


def _resolve_margin_model_path(
    model_dump: Mapping[str, Any], primary_method: str, *, backend_id: str
) -> str:
    """按 `margin_extraction.primary_method` 解析裕量采集应使用的模型入口路径。

    ## 为什么不能沿用场景的 `model_variant`

    `matlab/+pa/run_linear_analysis.m` 的文件头写明：「本文件信任 model_path 已由
    Python 侧按 margin_cfg.primary_method 解析出正确的模型变体入口……本文件自身不再
    按 primary_method 在 switching/averaged 两个模型文件之间做二次选择」。也就是说
    这个解析是 Python 侧的职责，而此前 `simulate()` / `simulate_batch()` 把**场景
    变体**的 `model_cfg_json` 原样传了过去——需要采集裕量的评价场景用的是开关模型，
    于是 `linear_analysis_on_averaged` 会在开关模型上执行 `linearize`。

    这个缺陷此前被 Python 后端掩盖：`sim/backends/session.py` 的
    `_run_linear_analysis()` 在内部自己改用 `model_package.averaged.entry`
    （见该函数 docstring：「裕量始终在平均模型上提取，与场景声明的 model_variant
    无关」）。两个后端因此对同一个输入做了不同的事，而 MATLAB 侧按契约不做纠正。
    本函数把解析放回它该在的位置。
    """
    variant = _MARGIN_METHOD_VARIANT.get(primary_method)
    if variant is None:  # pragma: no cover - config.schema 已把该字段限为二值枚举
        raise ValueError(
            f"simulate(): 未知的 margin_extraction.primary_method={primary_method!r}，"
            f"取值应属于 {sorted(_MARGIN_METHOD_VARIANT)!r}"
        )
    return _resolve_model_path(model_dump, variant, backend_id=backend_id)


def _build_scenario_payload(
    scenario: ScenarioSpec, run_id: str, *, stop_time_s: float
) -> dict[str, Any]:
    """从 `ScenarioSpec` 取 MATLAB 侧关心的电气量字段，附加 `run_id` 作为波形
    文件名提示（`matlab/+pa/simulate_once.m` 的 `local_resolve_waveform_path`
    逻辑：`scenario_json` 提供非空 `run_id` 字段时取其值作为文件名主体），
    以及 `step_trigger_s`（负载阶跃触发时刻）。

    ## `step_trigger_s`：为什么由本函数算，而不是让后端各自算

    `ScenarioSpec` 里没有阶跃时刻字段——`task.yaml` 的场景行只声明"从多少安培
    跳到多少安培、以多快的斜率"，不声明"什么时候跳"。触发时刻是一条**全系统
    共用的时间轴约定**：`STEP_TRIGGER_FRACTION * stop_time`。

    Python 后端此前是在求解器内部自己算这个值（`averaged.py` / `switching.py`
    各有一行 `t_step = STEP_TRIGGER_FRACTION * stop_time_s`），而 MATLAB 侧
    `simulate_once.m` 的场景注入白名单里根本没有这个字段——模型无从得知何时
    变载，`collect_signals.m` 也无从把它写进波形 MAT。后果是
    `settling_time` / `overshoot` / `undershoot` 三个指标（窗口都是
    `[step_trigger, step_trigger_plus_500us]`）在 MATLAB 后端下全部落
    `no_step_detected`。

    本函数把这个值算一次、放进 `scenario_json`，两个后端都从同一处取用：
    MATLAB 侧写入模型工作区供负载阶跃块读取，并由 `collect_signals.m` 原样写进
    波形 MAT。这样两份波形的阶跃时刻按构造相同，双后端一致性核对比较的才是
    同一段瞬态。

    `STEP_TRIGGER_FRACTION` 从 `sim.backends.buck_model` 导入：那个常量的语义
    是"场景在时间轴上如何展开"，属于两个后端共用的场景约定而非某一个后端的
    实现细节，因此它现在的位置（Python 后端的模型模块内）是次优的。此处选择
    导入而不是搬家：搬动它会触碰 `buck_model.py` / `averaged.py` /
    `switching.py` 及其测试，全部不在本次改动范围内，而导入一个常量是一行。
    两处定义同一个数字才是真正要避免的（会漂移），导入不会。
    """
    return {
        "vin_v": scenario.vin_v,
        "temp_c": scenario.temp_c,
        "load_start_a": scenario.load_start_a,
        "load_end_a": scenario.load_end_a,
        "slew_a_per_us": scenario.slew_a_per_us,
        "step_trigger_s": STEP_TRIGGER_FRACTION * stop_time_s,
        "run_id": run_id,
    }


def simulate(
    candidate: Candidate,
    scenario: ScenarioSpec,
    *,
    model_cfg: ModelConfig,
    metrics_cfg: MetricsConfig,
    session: MatlabSession,
    run_id: str,
    artifacts: ArtifactStore,
    task_id: str,
    frozen_fingerprint: str,
    frozen_model_package_hash: str,
    base_dir: str | Path = ".",
) -> SimulationResult:
    """只读方式执行一次仿真，运行时注入参数，绝不写模型文件（`design.md` §6.3.2）。

    参数（design.md 字面签名之外的五个必要扩展见模块顶部说明）：
      - `task_id` / `frozen_fingerprint` / `frozen_model_package_hash`：必填
        kwonly，由调用方（未来的 `run_task()`）在任务开始时算好后原样传入。
      - `metrics_cfg`：必填 kwonly（任务 11.2 新增），用于构造 `margin_cfg_json`
        （第四实参，见模块顶部"任务 11.2 补丁一"）与校验 R8.4 后置条件
        （见模块顶部"任务 11.2 补丁二"）。
      - `base_dir`：可选 kwonly，默认当前工作目录。

    流程：
      1. 模型不变性检查（CP-3）：`fast_fingerprint` 匹配则直接继续；不匹配则
         升级为全量 `model_package_hash` 比对；全量仍不匹配则抛
         `ModelMutatedDuringOptimizeError`，不发起任何 Simulink 调用。
      2. 构造三个 JSON payload，调用 `pa.simulate_once`。
      3. `status='ok'` 时把 MATLAB 侧产出的本地波形路径经 `ArtifactStore`
         提交为已归档产物，得到 `waveform_ref`；非 `'ok'` 时 `waveform_ref=None`。
      4. `scenario.require_margin` 为真且 `status='ok'` 时，内联调用
         `pa.run_linear_analysis`（软依赖，见模块顶部说明；四实参，含
         `margin_cfg_json`——任务 11.2 补丁一），把其 `engine_starts` 计入
         返回值，产物同样经 `ArtifactStore` 提交为 `observable_ref`；随后
         校验累加后的 `engine_starts` 是否满足 R8.4 后置条件，不满足则抛
         `MarginEngineStartsUnderreportedError`（任务 11.2 补丁二）。
      5. 返回 `SimulationResult`。

    依赖闭包内每个文件在返回后逐字节不变：本函数不实现事后校验，理由见模块
    顶部"依赖闭包'逐字节不变'的后置条件"一节。
    """
    model_dump = model_cfg.model_dump(mode="json")

    # ---- 步骤 1：模型不变性检查（CP-3，提炼为 _check_model_not_mutated） ----
    _check_model_not_mutated(
        model_dump,
        base_dir=base_dir,
        frozen_fingerprint=frozen_fingerprint,
        frozen_model_package_hash=frozen_model_package_hash,
    )

    # ---- 步骤 2：构造 payload 并调用 pa.simulate_once ----
    model_path = _resolve_model_path(
        model_dump, scenario.model_variant, backend_id=session.backend_id
    )
    model_cfg_payload = {**model_dump, "model_path": model_path}

    model_cfg_json = json.dumps(model_cfg_payload)
    params_json = json.dumps(dict(candidate.parameters_si))
    scenario_json = json.dumps(
        _build_scenario_payload(
            scenario, run_id, stop_time_s=float(model_cfg.io_contract.solver.stop_time)
        )
    )

    raw_result = session.call(
        "pa.simulate_once", model_cfg_json, params_json, scenario_json, nargout=1
    )

    status = str(_field(raw_result, "status"))
    elapsed_ms = int(_field(raw_result, "elapsed_ms"))
    engine_starts = int(_field(raw_result, "engine_starts"))

    # ---- 步骤 3：status='ok' 时提交波形产物（提炼为 _stage_waveform） ----
    waveform_ref: str | None = None
    if status == "ok":
        local_waveform_path = str(_field(raw_result, "waveform_path"))
        waveform_ref = _stage_waveform(
            artifacts, task_id=task_id, run_id=run_id, local_path=local_waveform_path
        )

    # ---- 步骤 4：require_margin 场景内联裕量采集（软依赖：任务 11.1） ----
    observable_ref: str | None = None
    if status == "ok" and scenario.require_margin:
        primary_method = metrics_cfg.margin_extraction.primary_method
        margin_cfg_json = json.dumps({"primary_method": primary_method})
        # 裕量采集用的 model_path 由 primary_method 决定，不是场景的 model_variant
        # ——见 _resolve_margin_model_path() 的 docstring。
        margin_model_cfg_json = json.dumps(
            {
                **model_dump,
                "model_path": _resolve_margin_model_path(
                    model_dump, primary_method, backend_id=session.backend_id
                ),
            }
        )
        margin_result = session.call(
            "pa.run_linear_analysis",
            margin_model_cfg_json,
            params_json,
            scenario_json,
            margin_cfg_json,
            nargout=1,
        )
        margin_engine_starts = int(_field(margin_result, "engine_starts"))
        engine_starts += margin_engine_starts

        local_freq_response_path = _field(margin_result, "freq_response_path")
        if local_freq_response_path:
            suffix = Path(str(local_freq_response_path)).suffix or ".mat"
            tmp = artifacts.stage(
                task_id=task_id, kind="metrics", name=f"{run_id}_freq_response{suffix}"
            )
            tmp.write_bytes(Path(str(local_freq_response_path)).read_bytes())
            observable_ref = artifacts.commit(tmp)

        _check_margin_engine_starts(
            engine_starts, metrics_cfg=metrics_cfg, run_id=run_id
        )

    # ---- 步骤 5：返回 SimulationResult ----
    return SimulationResult(
        run_id=run_id,
        status=status,  # type: ignore[arg-type]  # 五值枚举由 MATLAB 侧 map_error.m 保证
        waveform_ref=waveform_ref,
        observable_ref=observable_ref,
        elapsed_ms=elapsed_ms,
        engine_starts=engine_starts,
    )


def inspect_model(
    model_cfg: ModelConfig,
    variant: ModelVariant,
    *,
    session: MatlabSession,
) -> ModelInspection:
    """校验 Block Path 与信号名存在性，返回 I/O 契约实际状态（`design.md` §6.3.2）。

    `session` 为 design.md 字面签名之外的必填 kwonly 扩展：调用 `pa.inspect_model`
    必须经由 `MatlabSession.call()`，`inspect_model()` 没有其他途径触达 MATLAB
    Engine（见模块顶部"`inspect_model()` 的 `session` 参数"一节）。

    构造喂给 `pa.inspect_model` 的 `model_cfg_json`：只含该函数实际读取的两个
    字段——`model_path`（按 `variant` 从 `model_cfg.model_dump()` 解析，复用
    `_resolve_model_path()`）与 `io_contract`（整节原样附带，`inspect_model.m`
    从中取 `injectable_params.<name>.block_path` 与
    `output_signals.<name>.logsout_name`）。

    Block Path 或信号名不存在时，MATLAB 侧抛出 `pa:ContractError`；本函数不
    捕获、不转译，原样向上传播（见模块顶部"`inspect_model()` 不捕获
    `ContractError`"一节）——把它翻译为
    `stop_and_ask_human(cause='model_systemic_error')` 是调用方（`controller`）
    的职责。
    """
    model_dump = model_cfg.model_dump(mode="json")
    model_path = _resolve_model_path(model_dump, variant, backend_id=session.backend_id)

    model_cfg_json = json.dumps(
        {"model_path": model_path, "io_contract": model_dump["io_contract"]}
    )

    raw_result = session.call("pa.inspect_model", model_cfg_json, nargout=1)

    return ModelInspection(
        model_path=str(_field(raw_result, "model_path")),
        checked_params=list(_field(raw_result, "checked_params")),
        checked_signals=list(_field(raw_result, "checked_signals")),
    )


def simulate_batch(
    requests: Sequence[tuple[Candidate, ScenarioSpec]],
    *,
    model_cfg: ModelConfig,
    metrics_cfg: MetricsConfig,
    session: MatlabSession,
    artifacts: ArtifactStore,
    task_id: str,
    frozen_fingerprint: str,
    frozen_model_package_hash: str,
    run_ids: Sequence[str],
    base_dir: str | Path = ".",
) -> list[SimulationResult]:
    """按 `model_cfg.runtime.execution_mode` 批量执行仿真，调用方不逐次指定
    映射方式（`design.md` §6.3.2 / §6.4）。

    参数（design.md 字面签名之外的扩展；`task_id` / `frozen_fingerprint` /
    `frozen_model_package_hash` / `base_dir` 四项的必要性见 `simulate()`
    docstring 与模块顶部说明，此处不重复）：
      - `run_ids`：必填 kwonly，与 `requests` 同长度、同顺序，`run_ids[i]`
        对应 `requests[i]`。见模块顶部"`simulate_batch()` 的 `run_ids` 参数"。
      - `metrics_cfg`：必填 kwonly（任务 11.2 新增），与 `simulate()` 同理，
        用于构造 `margin_cfg_json` 与校验 R8.4 后置条件（见模块顶部"任务 11.2
        补丁一/二"）。

    流程：
      1. 模型不变性检查（CP-3，`_check_model_not_mutated`）**只做一次**，覆盖
         整批请求；真实突变时抛 `ModelMutatedDuringOptimizeError`，不发起任何
         `session.call()`（对整批请求，`session.call()` 次数为零）。
      2. 构造一个共享的 `model_cfg_json`（不含 `model_path`，见模块顶部
         "`model_path`：放进每个请求对象"一节）与一个 `requests_json` 数组
         （每个元素含该请求自己的 `model_path` / `params` / `scenario`），
         发起**一次** `session.call("pa.simulate_batch", ...)`。
      3. 把返回的 N 个原始结果按顺序转换为 `SimulationResult`：`status='ok'`
         的条目提交波形产物（`_stage_waveform`）；其余条目 `waveform_ref=None`。
         返回序列长度恒等于 `requests` 长度、顺序一致，不重排、不丢弃条目。
      4. 对批内每个 `scenario.require_margin=True` 且该条结果 `status='ok'`
         的请求，**额外单独**调用一次 `pa.run_linear_analysis`（不在批量调用
         内；见模块顶部"批量场景的 `require_margin` 处理"一节；四实参，含
         `margin_cfg_json`——任务 11.2 补丁一），把其 `engine_starts` 计入该条
         结果，产物提交为 `observable_ref`；随后逐条校验 R8.4 后置条件，不
         满足则抛 `MarginEngineStartsUnderreportedError`（任务 11.2 补丁二）。

    `matlab/+pa/simulate_batch.m` 尚未落地（`tasks.md` 未登记编号任务，见模块
    顶部说明）：本函数按 design.md §6.4 的注释级契约编写调用点，`serial` /
    `fast_restart` / `parsim` 的实际分支逻辑完全在 MATLAB 侧，本函数不读
    `model_cfg.runtime.execution_mode`、不做任何相关分支。
    """
    if len(run_ids) != len(requests):
        raise ValueError(
            f"simulate_batch(): len(run_ids)={len(run_ids)} != "
            f"len(requests)={len(requests)}，run_ids 必须与 requests 同长度、同顺序"
        )

    model_dump = model_cfg.model_dump(mode="json")

    # ---- 步骤 1：模型不变性检查（CP-3），整批只做一次 ----
    _check_model_not_mutated(
        model_dump,
        base_dir=base_dir,
        frozen_fingerprint=frozen_fingerprint,
        frozen_model_package_hash=frozen_model_package_hash,
    )

    # ---- 步骤 2：构造共享 model_cfg_json 与逐请求 requests_json，发起一次批量调用 ----
    model_cfg_json = json.dumps(model_dump)

    request_payloads = []
    for (candidate, scenario), run_id in zip(requests, run_ids):
        model_path = _resolve_model_path(
            model_dump, scenario.model_variant, backend_id=session.backend_id
        )
        request_payloads.append(
            {
                "model_path": model_path,
                "params": dict(candidate.parameters_si),
                "scenario": _build_scenario_payload(
                    scenario,
                    run_id,
                    stop_time_s=float(model_cfg.io_contract.solver.stop_time),
                ),
            }
        )
    requests_json = json.dumps(request_payloads)

    raw_results = session.call(
        "pa.simulate_batch", model_cfg_json, requests_json, nargout=1
    )

    # ---- 步骤 3：按顺序转换为 SimulationResult 列表 ----
    results: list[SimulationResult] = []
    for (_, scenario), run_id, raw_result in zip(requests, run_ids, raw_results):
        status = str(_field(raw_result, "status"))
        elapsed_ms = int(_field(raw_result, "elapsed_ms"))
        engine_starts = int(_field(raw_result, "engine_starts"))

        waveform_ref: str | None = None
        if status == "ok":
            local_waveform_path = str(_field(raw_result, "waveform_path"))
            waveform_ref = _stage_waveform(
                artifacts,
                task_id=task_id,
                run_id=run_id,
                local_path=local_waveform_path,
            )

        results.append(
            SimulationResult(
                run_id=run_id,
                status=status,  # type: ignore[arg-type]  # 五值枚举由 MATLAB 侧 map_error.m 保证
                waveform_ref=waveform_ref,
                observable_ref=None,  # 裕量场景在步骤 4 中原地补齐
                elapsed_ms=elapsed_ms,
                engine_starts=engine_starts,
            )
        )

    # ---- 步骤 4：require_margin 场景逐个额外调用 pa.run_linear_analysis ----
    for i, (candidate, scenario) in enumerate(requests):
        if not (scenario.require_margin and results[i].status == "ok"):
            continue

        run_id = run_ids[i]
        params_json = json.dumps(request_payloads[i]["params"])
        scenario_json = json.dumps(request_payloads[i]["scenario"])
        primary_method = metrics_cfg.margin_extraction.primary_method
        # 与 simulate() 步骤 4 同一处理：model_path 由 primary_method 决定，不沿用
        # request_payloads[i]["model_path"]（那是场景变体的入口）。见
        # _resolve_margin_model_path() 的 docstring。
        margin_model_cfg_json = json.dumps(
            {
                **model_dump,
                "model_path": _resolve_margin_model_path(
                    model_dump, primary_method, backend_id=session.backend_id
                ),
            }
        )
        margin_cfg_json = json.dumps({"primary_method": primary_method})

        margin_result = session.call(
            "pa.run_linear_analysis",
            margin_model_cfg_json,
            params_json,
            scenario_json,
            margin_cfg_json,
            nargout=1,
        )
        margin_engine_starts = int(_field(margin_result, "engine_starts"))

        observable_ref: str | None = None
        local_freq_response_path = _field(margin_result, "freq_response_path")
        if local_freq_response_path:
            suffix = Path(str(local_freq_response_path)).suffix or ".mat"
            tmp = artifacts.stage(
                task_id=task_id, kind="metrics", name=f"{run_id}_freq_response{suffix}"
            )
            tmp.write_bytes(Path(str(local_freq_response_path)).read_bytes())
            observable_ref = artifacts.commit(tmp)

        total_engine_starts = results[i].engine_starts + margin_engine_starts
        _check_margin_engine_starts(
            total_engine_starts, metrics_cfg=metrics_cfg, run_id=run_id
        )

        results[i] = SimulationResult(
            run_id=results[i].run_id,
            status=results[i].status,
            waveform_ref=results[i].waveform_ref,
            observable_ref=observable_ref,
            elapsed_ms=results[i].elapsed_ms,
            engine_starts=total_engine_starts,
        )

    return results
