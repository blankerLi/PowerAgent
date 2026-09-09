"""poweragent/reference/dense_grid.py

`dense_grid_scan()`（`design.md` §6.12；`tasks.md` 任务 21.1；需求追溯 R23.1,
R23.2；属性 CP-2；门禁 G-M3-1）。

## 本次（21.1）的范围边界：只有 `dense_grid_scan()`

`reference/dense_grid.py` 由 `tasks.md` 分三个顶层子任务派发（21.1 / 21.2 /
21.3），外加一个测试子任务（21.4）。本次**只**落地 `dense_grid_scan()`——
一次性 12×12 参考扫描本身。以下均**不**在本次范围内：

- **`response_surface()`**（任务 21.2）：按 rcomp/ccomp 档位渲染响应面图，
  已在本次落地，实现细节见本文件末尾"`response_surface()`（任务 21.2）
  的实现"一节；该节之前的"`DenseGridResult` 与 `response_surface()` 的
  接口契约"一节记录的是 21.1 落地时对该函数尚未存在的接口预期，21.2 的
  真实实现与该预期完全兼容（见新增小节开头的核对说明），保留原文不删。
- **`tiered_grid_scan()`**（任务 21.3）：预算/墙钟超限时的 6×6=36 点回退，
  未落地。本函数因此**不**在内部实现"超预算即改跑 tiered_grid_scan"的分支
  逻辑（那是 R23.4 的判据、任务 21.3 的职责）——`dense_grid_scan()` 只管
  "被调用时就把 144 点跑一遍"，是否应该被调用（还是改调
  `tiered_grid_scan()`）由调用方在调用前根据成本估算自行决定，与
  `check_baseline_gate()` 不把"是否需要调用它"的判断权收进自身是同一原则
  （见 `controller/preflight.py` 模块 docstring 第 4 节）。若 `run_tier()`
  在执行期间因预算耗尽抛出 `BudgetExhaustedError`，本函数不捕获、原样向上
  传播——这与"提前用估算挡住超预算调用"是两种互补但不同的机制，后者（任务
  21.3 的职责）不在本次实现范围内。

## `agent.validate` 的软依赖处理：`validate_fn` 可注入参数，无默认值

`agent/validate.py`（任务 24.1~24.3）是 M4 里程碑任务，`tasks.md`"优先序
判断标准"一节明确"M4 之前不写 `agent/`"——本任务（21.1）排在 M3，`agent`
包当前只有一个空的 `__init__.py`。`design.md` §6.12 的 `dense_grid_scan()`
伪代码字面上不显式出现 `validate` 调用（伪代码只写"144 个点全部经
`validate(mode='grid')` 校验"这一句话），但 §6.7 / R23.2 / CP-2 都明确要求
"144 点全部经过 `agent.validate()`、不存在绕过校验的仿真路径"——这是一条
安全属性（CP-2 属于阻断性缺陷清单，`design.md` §10 末），不能在 `agent.
validate` 尚未落地时被本函数悄悄跳过或用一个"永远放行"的桩函数代替。

本文件遵循 `controller/run_task.py` 的 `propose_fn` 软依赖处理惯例（该文件
模块 docstring"2. `propose_fn`"一节）：接受一个**可注入的 `validate_fn`
参数**，而不是 `cli.py` 式的"import-and-catch"（该模式适用于"整个下游子
系统尚不存在、此路径在被触达前就该给出清晰提示并返回"的场景，但本函数恰恰
需要在验证脚本里用一个可控的校验实现反复驱动完整流程，`import-and-catch`
只能表达"完全不可用，立即报错退出"，无法满足这一需求）。

**关键区别于 `propose_fn`**：`propose_fn` 在 `run_task.py` 中默认 `None`、
`None` 时退化为"本轮返回空候选集"这一诚实且安全的默认行为（空候选集是
design.md §8.1 伪代码本身承认的合法分支）。`validate_fn` 在本文件中**没有
这样的安全默认值**——不存在一个"合法的默认校验行为"：既不能默认"全部
接受"（那正是 CP-2 要防止的静默绕过），也不能默认"全部拒绝"（那会让
`dense_grid_scan()` 在 `agent.validate` 落地前调用即必然失败，与
"144 点全部应被判为 accepted"这一功能目标矛盾）。因此本函数把
`validate_fn` 定为**必填 kwonly 参数、无默认值**——调用方必须显式提供一个
真正执行四项检查（键集合、单位量纲、合法域、档位）的校验实现，才能调用本
函数；`agent.validate` 落地后，真实接线只需把 `agent.validate.validate`
本身（或对它的一个薄包装，固定 `mode='grid'`）传入即可，本函数的调用契约
不需要任何改动。

`validate_fn` 的调用签名（与 `design.md` §6.7/§7.2 给出的 `validate()` 字面
签名完全一致，供 24.3 落地的真实实现直接原样传入）：

```python
def validate_fn(
    raw: Sequence[Mapping[str, float]],
    *,
    design_space: DesignSpace,
    tested: Sequence[object],   # `agent.validate` 定义的 TestedPoint 序列；
                                # mode='grid' 时不参与新颖度/重复检查，本函数
                                # 恒传入空元组 `()`
    mode: str,                  # 本函数恒传入 "grid"
) -> object:                    # 须具备 `.accepted` / `.rejected` / `.notes`
                                 # 三个属性，形状同 design.md §6.7 的
                                 # `ValidationOutcome`
    ...
```

返回值只要求具备 `accepted`（`Sequence[store.repo.Candidate]`）与
`rejected`（`Sequence[tuple[Mapping[str, float], str]]`）两个属性（`notes`
在 `mode='grid'` 下恒为空、本函数不读取它）——本文件用一个 `Protocol`
（`ValidationOutcomeLike`）表达这个最小契约，不导入任何来自尚不存在的
`agent/validate.py` 的具体类型，避免在该模块落地前产生 import 失败。

## 与 `run_tier()` / `run_task.py` 之间不存在循环 import，采用模块顶层 import

`check_baseline_gate()`（`controller/preflight.py`）必须把
`from poweragent.controller.run_task import run_tier` 放进函数体内做延迟
import，理由是 `controller/run_task.py` 在其模块顶层反过来 `from
poweragent.controller.preflight import (...)`，两者互相依赖、必须打破循环
（见该函数文档"5. `run_tier()` 复用决策"一节）。本文件**不存在**这个问题：
`controller/run_task.py` 全文（已用 `grep` 核对）不 import 任何
`poweragent.reference.*` 的内容——`reference` 包对 `controller` 包是单向
依赖（`reference` 依赖 `controller`，反之不成立），因此本文件可以在**模块
顶层**直接 `from poweragent.controller.run_task import TierResult, run_tier`，
不需要延迟 import 这一层额外的复杂度。

## 场景行的 tier 语义：Evaluation 集，不是 Screening 或全部三层

`design.md` 未在 §6.12 的 `dense_grid_scan()` 伪代码中显式写出
"scenario_rows 取哪一层"，但以下三处互相独立的文本共同确定了答案：

1. `design.md` §12.2 成本估算表："Dense Grid 成本 | 12×12 × Evaluation
   行数，千次量级"——显式点名 Evaluation 行数，不是 Screening 或全部场景行。
2. `tasks.md` 任务 21.4（Dense Grid 单元测试）的描述："扫描点数 ×（Evaluation
   行数，含 `require_margin` 行的每候选额外启动数）超审批
   `max_engine_starts`"——同样显式点名 Evaluation 行数。
3. Dense Grid 是"参考扫描"（`requirements.md` 术语表："用于响应面可视化与
   『Agent 是否具备搜索价值』的直接答案，不作为搜索效率 Oracle"），不是一个
   带早拒绝优化的寻优循环——它不需要 Screening 层"先用少量角点场景快速排除
   明显不可行候选、再对幸存者跑完整 Evaluation 集"这一分层设计的性能收益
   （144 个点本身就是要被逐一跑完并可视化的参考数据，不是需要被"筛选掉"的
   候选池）。`response_surface()`（任务 21.2）要绘制的响应面图基于
   `objective.primary` 与 `phase_margin` 两个指标，这两者都只在
   Evaluation 层场景上计算（`phase_margin` 要求 `require_margin=True`，
   `constraints.yaml` 的 `hard_constraints.phase_margin_min.applies_to_tier`
   固定为 `[evaluation]`），Screening 层场景不产生这些指标。

因此本函数对每个候选调用 `run_tier(candidate, 'evaluation',
evaluation_scenarios, ...)` 一次，`scenario_rows` 传入
`controller.scenario.evaluation_rows(task_cfg)` 的全部行——这与
`check_baseline_gate()` 对其单个 Baseline 候选的处理方式完全一致（同一个
`run_tier()` 调用模式，只是候选数由 1 变为 144）。

## 单候选失败不终止整个扫描：Dense Grid 是参考数据集，不是寻优循环

`run_tier()` 对 Evaluation 层的既定行为是"单场景不可行只记录、继续跑完该层
其余场景"（`TierResult.passed=False` 可能仅表示"该候选在评价过程中遇到了
`diverged`/`solver_error`/`timeout`/重试耗尽的 `engine_transient`"）。本函数
在候选粒度上采用同一精神：**某个候选的 `TierResult.passed=False` 不终止
本次 `dense_grid_scan()` 调用**，本函数记录该结果后继续处理下一个候选。
理由：`requirements.md` R23.3"未取得有效指标的网格点标注为无效，不以插值
或默认值填充"这句话本身就预设了"144 点中允许存在若干个未取得有效指标的
点"这一事实——若本函数在遇到第一个失败候选时就整体 raise 终止，`response_
surface()`（任务 21.2）将永远无法拿到"其余 143 个点的有效结果 + 这一个点
标注为无效"这样的部分成功状态，R23.3 的"标注为无效"分支会变得不可达。
`BudgetExhaustedError`（`run_tier()` 内部的 `budget_ledger.reserve()`
抛出）是这一原则的唯一例外：预算耗尽是全局资源约束，不是某个候选自身的
仿真质量问题，本函数不捕获它、让它原样向上传播终止整个扫描——与
`run_tier()`/`check_baseline_gate()` 对该异常的既定处理方式一致。

## 校验先于任何落库：144 点必须先全部通过 `validate_fn`，否则不创建 `tasks` 行

`validate_fn` 的调用发生在 `store.create_task()` 之前——若 144 个网格点中
出现任何一个未被判为 `accepted`（即 `rejected` 非空），本函数在**创建任何
`tasks`/`candidates` 行之前**直接抛出 `DenseGridValidationError`，不写入
任何数据库状态、不启动任何仿真。这是 CP-2"不存在绕过校验的仿真路径"在本
函数入口处的具体落实：与其在校验失败后继续跑"能跑的那部分点"、把一个不完整
或校验异常的扫描伪装成一次正常完成的 `dense_grid` 任务，本函数选择在校验
这一步就 fail-fast——`design_space.variables.rcomp/ccomp.ticks` 与网格点集
本就是同一份档位定义展开出来的（见 `_build_grid_candidates()`），若真实
`agent.validate`（或验证脚本里的桩实现）判定其中某点越界/单位不符/未命中
档位，几乎可以肯定是 `validate_fn` 实现或 `constraints.yaml` 配置本身出了
问题，而不是"这个点碰巧不可行"——碰巧不可行是 `run_tier()` 仿真阶段才可能
出现的情形（见上一节），不应该在校验阶段发生。

## 预算隔离：与 `check_baseline_gate()` 完全同一机制，不重复论证

`BudgetLedger` 按 `task_id` 过滤 `SUM(runs.budget_units)`——只要本函数为
Dense Grid 构造一个绑定 `resolved_grid_task_id`（而不是任何 `optimize` 任务
的 `task_id`）的独立 `BudgetLedger` 实例，`run_tier()` 内部产生的全部
`budget_units` 天然只计入这个 `dense_grid` 任务，不会被任何 `optimize`
任务的 `BudgetLedger.used()` 查询到。完整论证见 `controller/preflight.py`
模块 docstring"任务 16.1"一节第 10 条，本文件不重复。

## `calibration_hash`：固定空串，与 `check_baseline_gate()` 同一处理

`calib` 模块（M2 里程碑）当前为空文件，`Store.create_task()` 要求
`calibration_hash` 非 `None`。与 `check_baseline_gate()`（非 `optimize`
`task_kind`，同样不涉及校准参数寻优）完全同一理由：Dense Grid 是参考扫描，
不是"寻优任务"，其 `tasks` 行的 `calibration_hash` 字段没有对应的领域含义
需要区分 PoC/工程轨——`check_baseline_gate()` 对这一列固定传入空串（未按
`task_cfg.simulation_only` 分支），本函数遵循同一先例。

## `stop_reason`：新增 `dense_grid_completed`，与 `baseline_gate_passed` /
`checkpoint1_rejected` 同一先例

`store/ddl.sql` 的 `tasks.stop_reason` 列本身只是 `TEXT`（无 `CHECK`
约束），`should_stop()` 产出的四值封闭集合约束字面上只约束"由
`should_stop()` 判定停止"这一条路径——Dense Grid 同样不经过
`should_stop()`（它不是寻优主循环，没有 `SearchState`）。
`controller/run_task.py` 已经确立"`baseline_gate_passed`"、"
`checkpoint1_rejected`"两个与 `should_stop()` 四值并列但来源不同的
`stop_reason` 取值，完整论证见 `controller/preflight.py` 模块 docstring
"任务 16.2 / d"一节与 `run_task.py` 模块 docstring"12.6"一节。本文件引入
第三个同类取值 `_DENSE_GRID_COMPLETED_STOP_REASON = "dense_grid_completed"`
——扫描的 144 个候选全部（无论其 `TierResult.passed` 是否为真，见上一节）
处理完毕后，本函数调用 `store.finish_task(grid_task_id, stop_reason=
"dense_grid_completed", cause=None)` 收尾，避免该 `tasks` 行永久停留在
没有 `ended_at`/`stop_reason` 的"仍在运行"状态（与 `check_baseline_gate()`
文档"12.6 / d"一节给出的理由相同：`store.get_task_summary()` 等既有查询会
把没有 `ended_at` 的行误判为仍在进行）。`BudgetExhaustedError` 传播导致
本函数提前退出的路径**不**调用 `finish_task()`——与 `run_tier()` 对
`BudgetExhaustedError` 的既定处理一致（该异常不在任何层级被捕获转换为一次
"正常收尾"，`tasks` 行保持未结束状态，留给未来的 `reap_orphan_runs()`/
人工介入处理，这与寻优任务耗尽预算时的既定行为一致）。

## `DenseGridResult` 与 `response_surface()`（任务 21.2，尚未落地）的接口契约

`design.md` §6.12 给出的 `response_surface()` 字面签名是
`response_surface(task_id: str, store: Store, out_dir: Path) -> list[Path]`
——它只接受一个 `task_id` 字符串，**不**接受 `dense_grid_scan()` 的返回值
作为参数；`response_surface()` 未来落地时应通过 `task_id` 独立向 `store`
重新查询 144 个候选的 `candidates`/`runs`/`metric_results` 行来绘图（与
`design.md` L18-5"按 `model_package_hash` 与 `constraints_hash` 匹配最近
一次成功的 `dense_grid` 任务"这一关联规则一致：关联凭据是这两个哈希 + `tasks`
表本身，不是一个跨函数传递的内存对象）。

因此本函数返回的 `DenseGridResult`（本文件新增，非 `design.md` 登记类型）
**不是** `response_surface()` 的必需输入——`response_surface(task_id, store,
out_dir)` 只需要 `DenseGridResult.task_id` 这一个字段（一个普通字符串，与
`design.md` 字面签名"`dense_grid_scan()` 返回 `task_id: str`"完全兼容，
`DenseGridResult` 只是把这个字符串包进一个更宽的 dataclass，与
`check_baseline_gate()` 把字面签名的返回类型从"隐含的布尔"扩展为
`BaselineGateStatus` dataclass 是同一种扩展方式，见该函数文档第 1 节）。
`DenseGridResult.points`（144 个 `DenseGridPoint` 的元组，逐点携带
`candidate_id`/`rcomp`/`ccomp`/`tier_result`）是本函数为调用方（尤其是无法
访问 `store` 的验证脚本、或希望在同一进程内避免重新查询数据库的调用方）
提供的**补充**信息，不是 `response_surface()` 未来实现所要求的输入契约——
落地 21.2 时的实现者可以选择完全不使用这个字段、只用 `task_id` 重新查库，
这与本文件的设计完全兼容。

## `response_surface()`（任务 21.2）的实现

**与上一节预期的核对**：上一节写在 21.1 落地时，预测 21.2 会"通过 `task_id`
独立向 `store` 重新查询"，不依赖 `DenseGridResult`。本次实现完全遵循这一点——
`response_surface()` 只接收 `task_id` 与 `store`，函数体内部独立发起 SQL
查询该 `task_id` 下 `origin='dense_grid'` 的候选与其指标结果，不接受、也不
需要调用方传入 `DenseGridResult`。

### 签名相对 `design.md` §6.12 字面签名的扩展

字面签名为 `response_surface(task_id: str, store: Store, out_dir: Path) ->
list[Path]`。本实现追加两个 kwonly 参数：`metrics_cfg: MetricsConfig`、
`constraints_cfg: ConstraintsConfig`。理由：字面签名的三个参数无法唯一确定
"响应面的哪个指标是主目标""rcomp/ccomp 的档位取值是什么"——`objective.
primary.metric_id`（决定绘制哪个指标的响应面）来自 `metrics_cfg`，绘图坐标轴
的 144 个 `(rcomp, ccomp)` 档位组合来自 `constraints_cfg.design_space`（与
`_build_grid_candidates()` 消费的是同一个字段，函数体内部复用同一个
`expand_all_ticks()` 调用，保证坐标轴与 `dense_grid_scan()` 实际扫描的档位
完全一致，不是本函数自行另外猜测的一组刻度）。这与本文件"签名相对字面签名
的全部必要扩展"的既定处理方式（`dense_grid_scan()` 自身也扩展了字面签名）
同源：字面签名是 design.md 给出的"最小可读契约"，具体落地时需要的额外依赖
以 kwonly 参数追加，不改变前三个位置参数的名称与顺序。

`phase_margin` 是否绘制第二张图不通过额外参数控制，而是直接检查
`metrics_cfg.active_metrics`——`requirements.md` R23.3 字面写"对 `objective.
primary` 聚合值与 `phase_margin` 各输出一张"，语气是恒定输出两张，但
`phase_margin` 未激活时数据库里不会有任何 `metric_id='phase_margin'` 的
`MetricResult` 行（`eval.margin.extract_margin()` 只在 `require_margin=true`
的场景上才被调用，且 Dense Grid 的 Evaluation 场景集是否含 `require_margin`
行取决于 `task.yaml` 本身）。本函数不因此报错、也不跳过第二张图——它按
"该指标在 144 点里全部标注为无效"的既有路径处理（见下方"无效点"一节），
第二张图仍然产出（全灰），满足"返回非空图文件路径列表"里"两个文件恒定存在"
这一实现选择（见下一节），不额外引入一个"`phase_margin` 未激活时只返回
1 个路径"的分支——`response_surface()` 的字面签名从不接受
`metrics_cfg.active_metrics` 之外的任何信号来判断"是否该跳过第二张图"，
若真的要表达"跳过"，*产出全灰图并标注为无效*已经是诚实的表达（与产出图
但没有数据点这件事完全一致），额外报错或不产图反而制造了一个字面签名
（`-> list[Path]`）没有描述过的行为分支。

### 输出目录：`out_dir: Path` 直接落盘，不经过 `ArtifactStore`

`design.md` §6.12 字面签名把 `out_dir: Path` 作为直接参数，而不是 `dense_
grid_scan()`（以及 `run_tier()`/`check_baseline_gate()`）统一使用的
`artifacts: ArtifactStore` + `task_id` 组合——这与本文件其余函数的既有产物
落盘方式不同，值得单独记录判断依据：

1. **字面签名本身已经把这个决定做出来了**：design.md 的其余产物型函数
   （`sim.simulate()` 的 `waveform_ref`、`eval.margin` 的频响引用）全部通过
   `ArtifactStore.commit()` 产出一个数据库可追踪的引用字符串，从不在签名里
   直接暴露 `Path`。`response_surface(task_id, store, out_dir: Path) ->
   list[Path]` 是 design.md §6.10 `report.plot_waveform()` / `report.
   plot_response_surface()` 同一签名风格的延似（`out: Path` 参数），这组
   `report.*` 函数从未经过 `ArtifactStore`——报告渲染期产出的图片本身是
   "报告的组成部分"，不是需要被仿真结果引用、参与缓存键或往返校验的"仿真
   产物"，`ArtifactStore.commit()` 的核心价值（sha256 往返校验、原子 move、
   数据库引用绑定、供后续仿真/评价流程按引用复用）在这里都不适用——响应面
   图不会被别的函数按引用读回，它是给人看的终端产物。
2. **`response_surface()` 恰好属于这一类**：函数名与调用位置（design.md
   §8.1 顶层伪代码 `response_surface(grid_task_id, store, out_dir="artifacts/
   grid/plots")`，与 `report.*` 系列一样只把 `Path` 传给下游、不接收
   `ArtifactStore`）与 `report.plot_response_surface()` 是同一件事在
   `reference` 与 `report` 两处的重复登记（design.md §6.12 末尾"`response_
   surface` 与 dense_grid 任务的关联规则"一节与 §6.10 的 `plot_response_
   surface()`——两者的差异只在于 `report.plot_response_surface()` 内部按
   哈希匹配去找最近一次 `dense_grid` 任务后再调用本函数，或是等价复用同一
   段绘图逻辑，这属于任务 26.3 的职责、不在本次范围内）。用调用方给定的
   目录直接写文件，是这个"图是终端产物、不参与仿真结果的可追踪引用体系"
   定位下最简单、且与字面签名逐字符一致的实现。
3. 因此本函数体内部只做 `out_dir.mkdir(parents=True, exist_ok=True)` 后
   `fig.savefig(out_dir / "<name>.png")`，不调用 `store` 的任何写入方法、
   不经过 `ArtifactStore`——`store` 参数只用于只读查询 144 个网格点各自的
   候选与指标数据。

### 查询方式：直接对 `store.connection` 发起只读 SQL

`Store` 未提供"按 `task_id` + `origin` 取候选及其全部 `MetricResult`"的
专用方法（`query_worst_case()` 是"任一 Evaluation 场景缺失即不可行"的
worst-case 聚合查询，语义完全不同：它对每个候选只返回一个聚合后的标量，
且只在该候选**全部** Evaluation 场景都齐备时才出现在结果里——这恰恰与
R23.3"未取得有效指标的网格点标注为无效"相反，若改用 `query_worst_case()`
本函数将无法区分"该网格点确实所有场景都失败"与"该网格点只是没有 144 个点
之外的候选"，也无法取得单个网格点的坐标信息）。因此本函数遵循
`controller/preflight.py` 的 `_evaluate_baseline_gate_rows()` /
`_collect_frozen_baseline_snapshot()` 已确立的既定模式（`Store` 未覆盖的
只读查询直接经 `store.connection.execute(...)` 发起，不判定可行性、只读取
既有列的原始值），按候选逐个查询：

```sql
SELECT candidate_id, parameters_si FROM candidates
 WHERE task_id = ? AND origin = 'dense_grid'
```

再对每个候选的 `parameters_si`（取 `rcomp`/`ccomp`）与对应 `metric_id`
（逐候选按 `run_id IN (SELECT run_id FROM runs WHERE task_id=? AND
candidate_id=?)` 关联 `metric_results`，取 `MAX(value) WHERE valid=1`
——同一候选在 Evaluation 集的多个场景行上都会产出该 `metric_id` 的
`MetricResult`，本函数取其中有效值的最大值，与 `WORST_CASE_SQL` 对主目标
的聚合口径一致，供响应面在同一候选场景值不唯一时也能取得一个确定的标量；
若该候选在全部场景上均无有效值，则该候选在该指标上判为无效网格点）。

### 无效点：不插值、不填默认值，直接置为 `numpy.nan` 并遮罩

R23.3 原文"未取得有效指标的网格点标注为无效，不以插值或默认值填充"——本
实现把无效点的响应面矩阵元素置为 `numpy.nan`（不是 `0.0`、不是相邻点的
插值结果），并用 `numpy.ma.masked_invalid()` 遮罩后交给
`matplotlib.axes.Axes.imshow()` 渲染：`imshow()` 对遮罩元素按当前
`colormap` 的 `set_bad()` 颜色（本实现选择中性灰 `"lightgray"`）单独着色，
与有效数值的颜色映射完全脱离色标（colorbar）范围——这就是模块顶部
`response_surface()` 字面契约要求的"视觉上可区分"：无效点不会被误读成
"仿真出的一个偏低数值"，因为它的颜色不落在数值色标的任何一点上。

判定一个网格点"无效"的条件：该候选在该指标上没有任何 `valid=1` 的
`MetricResult` 行（可能是候选完全未出现在 `candidates` 表里——理论上不该
发生，因为 `dense_grid_scan()` 已保证 144 点全部落库，但若调用方传入的
`task_id` 对应的扫描因 `BudgetExhaustedError` 提前中断，会有候选缺失
`runs`/`metric_results` 行，这属于"该点尚未跑到"，同样按无效处理，不视为
异常）。

### 恰好 144 个网格坐标：坐标轴来自 `constraints_cfg`，不是数据库里出现过的候选集合

绘图的 x/y 轴刻度取 `expand_all_ticks(constraints_cfg)` 的 `rcomp`/`ccomp`
两个档位元组（与 `_build_grid_candidates()` 完全同一路径，见上）——不是
"数据库里查到多少个候选就画多少格"。这保证即使 `task_id` 对应的扫描因预算
耗尽只完成了部分候选，响应面仍然画出完整的 12×12 网格、缺失的格子按上一节
的无效路径遮罩，而不是产出一张格数不足 144、坐标轴与 `dense_grid_scan()`
的档位定义不对应的图。数据库查到的每个候选按其 `parameters_si.rcomp`/
`ccomp` 数值（经 `format_tick_literal()` 规范化文本后）与坐标轴刻度文本
精确匹配定位到网格里的哪一格（两者共用同一份 `.12g` 格式化规则，见
`config/ticks.py`"为什么返回字符串"一节，本函数复用该文本匹配、不用浮点数
容差比较，避免浮点误差导致匹配偏移一格）；数据库中若出现一个不匹配任何
坐标轴刻度文本的候选（不应该发生，因 `origin='dense_grid'` 的候选只可能
来自 `_build_grid_candidates()`），本函数忽略该候选、不计入任何格子，也
不因此报错——这类候选属于"不属于本次 12×12 网格定义"的数据，其存在本身
不应阻塞响应面图的产出。

### 零候选时的处理：`task_id` 下不存在任何 `origin='dense_grid'` 候选即报错

`requirements.md` R23.3 要求"返回非空的图文件路径列表"，任务描述同样要求
"只在真正零候选时失败/产出空列表"。本函数在完成 SQL 查询后，若该 `task_id`
下 `origin='dense_grid'` 候选数为 0，直接抛出 `DenseGridResponseSurfaceError`
（本文件新增异常类型），不产出任何文件、不创建 `out_dir`——"该 `task_id`
从未跑过 dense grid 扫描"（`task_id` 拼写错误、传入了一个 `optimize`
任务的 `task_id`、或该任务在写入第一个候选之前就已终止）与"扫描已执行、
只是 144 点里有很多无效点"是两类性质完全不同的情形，前者是调用方用法
错误，理应尽早失败并给出可诊断的消息，不应该被"总是产出两张全灰图"的宽容
路径悄悄掩盖；后者（部分/大量无效点）则按上一节的遮罩路径处理、正常产出
非空的两文件列表，不视为异常。144 点里只有极少数（甚至只有 1 个）有效
指标值时，本函数仍然只产出两张图（不因为"数据太稀疏"而拒绝产图），因为
每张图本身就是"用遮罩标注了绝大部分点为无效"这一事实的诚实呈现。

### 输出：恰好两个文件，命名固定

`out_dir / "response_surface_primary.png"`（`objective.primary.metric_id`
指标的响应面）与 `out_dir / "response_surface_phase_margin.png"`
（`phase_margin` 指标的响应面，`phase_margin` 是否在 `active_metrics` 中
不影响是否产出这个文件，见上"是否绘制第二张图"一节）。返回值为这两个
`Path` 按上述固定顺序组成的列表——不是"用 glob 扫描 `out_dir` 里实际写出
的文件"这种间接方式，因为本函数确定性地只产出这两个文件，直接构造返回值
更简单也更不容易与实际写盘内容脱节。

## `tiered_grid_scan()`（任务 21.3）的实现：分层参考扫描回退

`requirements.md` R23.4/R23.5、`tasks.md` 任务 21.3、`design.md` §6.12/§13、
门禁 G-M3-1。范围：预算/墙钟超审批上限时改跑的 6×6=36 点回退扫描。

### 触发判据的归属：不在 `tiered_grid_scan()` 内部、也不在 `dense_grid_scan()`
### 内部，而是调用方在两者之间选择时使用的一个可复用估算函数

`dense_grid_scan()`（任务 21.1）落地时已确立的原则原样适用于本次："`dense_
grid_scan()` 只管『被调用时就把 144 点跑一遍』，是否应该被调用……由调用方
在调用前根据成本估算自行决定"（见本文件模块 docstring 顶部"21.1 的范围
边界"一节）。`tiered_grid_scan()` 对称地只管"被调用时就把 36 点跑一遍"，
不在内部判断"当前是否处于预算超限状态、因此应该是我而不是 `dense_grid_
scan()` 被调用"——这个判断需要**先假设跑 144 点**去估算成本，而
`tiered_grid_scan()` 自己的方法体里从不出现"跑 144 点"这件事，把这个判断
塞进本函数会让本函数依赖一个自己根本不执行的假设分支，语义上不自洽。

R23.4 给出的判据字面公式是"扫描点数 ×（Evaluation 行数，含 `require_
margin` 行的每候选额外启动数）超审批 `max_engine_starts`、或按 P-1 `single_
run_s` 折算墙钟超审批上限"——这与任务 21.4（Dense Grid 单元测试）描述、
`design.md` §12.2 成本估算表"Dense Grid 成本 | 12×12 × Evaluation 行数"
一句共同确定的公式完全一致，且与 `controller/preflight.py` 的
`check_budget_feasibility()`（`estimated_wallclock = probe_single_run_s ×
budget.max_engine_starts`）是同一类"先验成本估算、超限即报错/改路径"的
判断结构，只是这里比较的对象从"寻优任务预算"换成"Dense Grid 扫描审批
额度"、且多了一项 `require_margin` 行的额外启动数修正。

因此本次新增一个独立的、不属于任何一个扫描函数、纯计算不做任何 I/O 的
`estimate_dense_grid_engine_starts(scan_point_count, task_cfg, metrics_cfg)`
辅助函数，把 R23.4 的算式落成可复用代码，调用方（`run_task.py` 或未来的
CLI 编排层，均不在本次范围内）在决定"调 `dense_grid_scan()` 还是
`tiered_grid_scan()`"之前各用 `scan_point_count=144` 与
`probe_single_run_s × 估算值` 与两个审批上限比较，逻辑示例：

```python
estimated_starts = estimate_dense_grid_engine_starts(144, task_cfg, metrics_cfg)
estimated_wallclock = probe_single_run_s * estimated_starts
if (estimated_starts > gate_budget_max_engine_starts
        or estimated_wallclock > gate_budget_max_wallclock_hours * 3600):
    result = tiered_grid_scan(..., gate_budget_max_engine_starts=..., ...)
else:
    result = dense_grid_scan(..., gate_budget_max_engine_starts=..., ...)
```

`estimate_dense_grid_engine_starts()` 不读取 `probe_single_run_s`（探针实测
值，`controller/preflight.py` 的既定处理是把它当作无默认值的必填参数，见
该文件模块 docstring"12."一节）——墙钟折算是调用方在拿到 `estimated_
engine_starts` 之后自己乘一次 `probe_single_run_s` 的事，不需要为此再定义
第二个几乎不做计算的函数。这与 `check_budget_feasibility()` 把
`estimated_wallclock = probe_single_run_s × budget.max_engine_starts` 整个
算式收进同一个函数不同——那里 `budget.max_engine_starts` 本身就是
"预算工时数"这个语义、乘积直接就是最终判据；这里 `estimated_engine_starts`
是一个中间量（先求"这次扫描共需要多少次启动"，再乘 `probe_single_run_s`
得到墙钟），R23.4 的判据本身也是两个独立的比较（启动数比 vs 墙钟比），拆成
一个返回启动数的函数比把两个比较结果都塞进一个返回布尔值的函数更清楚地
暴露了中间量，供调用方按需只做其中一个比较（例如某些审批只关心启动数
上限、不关心墙钟）。

`estimate_dense_grid_engine_starts()` 内部只做纯算术：
`evaluation_rows(task_cfg)` 取 Evaluation 行（与 `dense_grid_scan()`/
`tiered_grid_scan()` 实际执行时读取的同一层场景行，见本文件模块 docstring
"场景行的 tier 语义"一节），每行贡献 1 次启动、`require_margin=True` 的行
再加 `metrics_cfg.margin_extraction.extra_engine_starts_per_candidate` 次
——这与 `sim/simulate.py` 的既定行为"`require_margin=true` 时每次非缓存
执行的 `engine_starts >= 1 + extra_engine_starts_per_candidate`"（任务
11.2）逐字对应，`scan_point_count` 参数供调用方传入 144（评估是否需要
回退）或 36（回退后确认新点集本身仍在预算内，若调用方需要这一步的话，
见下一节）传入同一函数即可，不需要为两个规模各写一份估算逻辑。

### `tiered_grid_scan()` 自身不重新做这层估算——它假定被调用即代表"已判定
### 该跑 36 点"，与 `dense_grid_scan()` 对 144 点的处理完全对称

`tiered_grid_scan()` 被调用后直接跑 36 点，**不**在内部再次调用
`estimate_dense_grid_engine_starts(36, ...)` 去确认 36 点是否仍然超预算——
如果确实超了，本函数与 `dense_grid_scan()` 处理"点数超预算"的既有机制是
同一个：`BudgetLedger.reserve()` 在预算不足时抛 `BudgetExhaustedError`（见
`controller/budget.py`），`run_tier()`/`grid_ledger` 会在真正触碰预算上限
的那一次 `reserve()` 调用处自然终止，不需要本函数提前用估算重新判断一遍。
这与 `dense_grid_scan()` 模块 docstring 顶部"若 `run_tier()` 在执行期间
因预算耗尽抛出 `BudgetExhaustedError`，本函数不捕获、原样向上传播"的既定
处理完全一致，本函数直接复用该处理路径，不新增分支。

### 点集构造：`_build_tiered_grid_candidates()`，复用 `expand_all_ticks()`
### 与 `_build_grid_candidates()` 同一路径，只在笛卡尔积之前多做一次偶数
### 索引子集截取

`_even_indexed_subset(ticks)`：给定 `expand_ticks()`/`expand_all_ticks()`
返回的任意长度档位文本元组，取索引 `0, 2, 4, ...`（`range(0, len(ticks), 2)`）
构成的子集——12 档输入恰好得到 6 档输出（索引 0/2/4/6/8/10）。之所以在
**字符串**（`format_tick_literal()` 规范化后的十进制文本，`expand_all_
ticks()` 的返回类型）层面截取、而不是先转 `float` 再截取：`_build_grid_
candidates()` 的既定顺序是"先展开档位文本、再逐项 `float()` 转换"（见该
函数文档"为什么返回字符串"一节引用的理由），偶数索引截取只是在这个既有
顺序中插入的一步"取子集"操作，操作对象是转换前还是转换后在数值上没有
差别（`float(ticks[i])` 与先转后取子集结果逐项相同），但沿用"先展开
文本再转换"的既定顺序可以让 `_build_tiered_grid_candidates()` 与 `_build_
grid_candidates()` 除了多一行子集截取之外逐行相同，差异最小、最容易
核对两者是否真的在同一份档位定义上取子集。

等比子集性质（任务描述与 `requirements.md` R23.4 已给出的数学事实，本函数
不重新证明，只依赖它）：设升序等比档位 `t_0, t_1, ..., t_11`、公比
`r = t_{i+1}/t_i` 对全部 `i` 相同（`config/schema.py` 的
`_check_ticks_geometric_and_tolerance` 已在配置加载期强制这一点）。偶数
索引子集 `t_0, t_2, t_4, ..., t_10` 相邻两项的比值为
`t_{2k+2}/t_{2k} = (t_{2k+2}/t_{2k+1}) × (t_{2k+1}/t_{2k}) = r × r = r²`，
对全部 `k` 相同——因此子集本身仍然是等比数列（公比为原公比的平方），
`tick_distance`（新颖度度量，按档位间隔计数）与响应面渲染依赖的"档位在
对数坐标下等距"这两条性质在子集上都继续成立，不需要任何额外校验代码：
`_check_ticks_geometric_and_tolerance` 已经保证了原 12 档等比，等比性质
在取偶数索引子集后被数学保留，不是本函数需要重新断言的运行期不变量。

`_build_tiered_grid_candidates(design_space)` 因此就是 `_build_grid_
candidates(design_space)` 的对应函数，函数体只有一行差异（对
`ticks_by_variable["rcomp"]`/`["ccomp"]` 先调用 `_even_indexed_subset()`
再 `float()` 转换）；两者独立定义（不是让 `_build_grid_candidates()` 接受
一个"是否取子集"的布尔参数），因为 21.1 已经落地且被 21.4 的测试直接
调用、`response_surface()` 文档也逐处引用了它的既定行为与顺序，往其中
插入一个新的可选分支属于对已落地代码的非必要改动（本文件遵循"外科手术式
最小改动"的既定风格，见 behavioral guidelines）；两个 36 行以内的纯函数
重复几行远比改造一个已被其他函数依赖的既有签名更安全。

### `task_kind`：新引入 `'tiered_grid'`；`Candidate.origin` 仍复用既有的
### `'dense_grid'` 取值，不新增枚举项

`store/ddl.sql` 的 `tasks.task_kind` 列**没有** `CHECK` 约束（该列注释
"`optimize | dense_grid | robustness | baseline_gate`"只是文档性说明，不是
枚举强制），本文件因此可以引入第三个 dense-grid 系任务的 `task_kind` 取值
`'tiered_grid'`，不需要任何 DDL 迁移——这与 `run_task.py`/`preflight.py`
已确立的"`stop_reason` 新增值不需要改 DDL，因为该列同样是自由文本"是同一
个论证结构（见本文件"stop_reason"一节）。`tiered_grid_scan()` 因此产出一
个独立可查询、可与 `task_kind='dense_grid'` 区分的 `tasks` 行，未来的报告
渲染层（任务 26.x）可以据此判断该用"近似参考"措辞模板还是"参考扫描"模板
（见下一节"结果标注"）。

反过来，`store/repo.py` 的 `Candidate.origin` **是** `CHECK (origin IN
('agent','baseline','dense_grid','manual'))` 约束住的封闭枚举——往其中插入
第五个取值 `'tiered_grid'` 需要修改 `ddl.sql` 并让既有依赖 `origin='dense_
grid'` 过滤的代码（`response_surface()` 的 `_fetch_dense_grid_candidates()`
SQL、`requirements.md` R23（Requirement 2）AC2 的原文"`origin` 属于
`{agent, dense_grid}` 的候选……（`dense_grid` 走 `mode='grid'`）"）同步改成
`origin IN ('dense_grid', 'tiered_grid')` 的写法。本函数不做这项改动：
`tiered_grid_scan()` 产出的候选在校验路径（`mode='grid'`）、预算隔离
机制、两级缓存键构造上与 `dense_grid_scan()` 产出的候选完全同构——它们
在"候选是如何被验证、如何被计价"这个维度上是同一类候选，`origin` 字段
表达的正是这个维度（R23 AC2 原文按 `origin` 分两支的判据本身就是"校验
路径"，不是"具体是哪个扫描函数产出的"），继续复用 `origin='dense_grid'`
不会丢失任何 CP-2 需要的信息；真正需要区分"这批候选来自完整扫描还是
分层回退扫描"的地方是 `tasks.task_kind`（按 `task_id` 一次性确定，任何
消费方只需要先查 `task_kind` 就知道该 `task_id` 下全部候选的来源），不需要
在 `candidates` 表的每一行上重复记录同一个任务级事实。因此本函数持续把
`origin="dense_grid"` 原样传给 `store.persist_candidate()`，不引入
`Literal` 扩展、不改 `ddl.sql`。

`stop_reason` 同样引入独立取值 `_TIERED_GRID_COMPLETED_STOP_REASON =
"tiered_grid_completed"`（而不是复用 `_DENSE_GRID_COMPLETED_STOP_REASON`）
——与引入独立 `task_kind` 同一动机：让"该任务是分层回退扫描"这一事实可以
仅凭 `tasks` 表的既有列（`task_kind` 与 `stop_reason` 双重体现，成本为零，
因为该列本就是自由文本）区分，不需要联表查询 `candidates.origin`。

### 内部结构复用：新增私有共享辅助函数 `_run_grid_scan()`，`dense_grid_
### scan()` 与 `tiered_grid_scan()` 均改为该函数的薄包装

`dense_grid_scan()`（任务 21.1）与本次新增的 `tiered_grid_scan()`
在"校验先于任何落库 → 独立 `tasks` 行 + 独立 `BudgetLedger` → 逐候选落库
→ 逐候选 `run_tier('evaluation', ...)` → `finish_task()` → 返回
`DenseGridResult`"这一整段流程上逐字相同，唯一差异是：(a) 候选点集的
构造函数（`_build_grid_candidates` vs `_build_tiered_grid_candidates`）、
(b) 落库的 `task_kind`（`'dense_grid'` vs `'tiered_grid'`）、(c) 收尾的
`stop_reason`（两个新引入的独立常量，见上一节）、(d) `gate_task_id` 为
`None` 时生成的默认 `task_id` 前缀（`dense_grid_` vs `tiered_grid_`，供
人工在数据库里按前缀快速区分两类任务，不依赖必须先查 `task_kind` 才能
辨认）。

按本文件遵循的"外科手术式最小改动"原则（behavioral guidelines 与
`dense_grid_scan()`/`response_surface()` 既有实现的一致做法），这里选择
"抽取一个共享私有辅助函数、两个公开函数都改为其薄包装"这一方案，而不是
（a）把整段逻辑复制一遍（~100 行重复，且两份未来若有 bug fix 需要同时改
两处，违反 DRY 到不合理的程度）或（b）让 `dense_grid_scan()` 接受一个
"要不要分层"的布尔/枚举参数在自身内部分支（那会改变 `dense_grid_scan()`
的公开签名与语义边界，且与"是否分层"这一决策本就不属于扫描函数自身职责
的既定原则——见上文"触发判据的归属"一节——相矛盾：如果分层与否是一个可以
传给 `dense_grid_scan()` 的参数，等于承认这个函数"知道"自己可能被要求
退化为分层扫描，这正是要避免的耦合）。

私有辅助函数 `_run_grid_scan()` 的签名比照 `dense_grid_scan()` 的 kwonly
参数集合，额外新增三个内部使用的定位参数（`raw_candidates` 取代由函数体
自行构造、`task_kind` 与 `stop_reason` 取代硬编码字面量、`task_id_prefix`
取代硬编码前缀），函数体是原 `dense_grid_scan()` 函数体去掉
"`_build_grid_candidates()` 调用"与"`gate_task_id or f"dense_grid_
{uuid.uuid4().hex}"`"两行硬编码后的逐字保留，`dense_grid_scan()` 与
`tiered_grid_scan()` 各自只保留"构造 `raw_candidates`（各自的点集函数）
→ 调 `_run_grid_scan()`"两步，公开签名、文档字符串与对外行为在
`dense_grid_scan()` 一侧不发生任何变化（对已落地代码的改动范围严格限定在
"把函数体挪进一个新的私有函数并转发调用"，不触碰任何判断逻辑、SQL、
参数校验顺序）。

### 结果标注为"近似参考"、不建立 Regret 体系：本函数的职责边界

R23.4 要求"结果标注为近似参考，结论用语只取自模板已定义集合（不含『全局
最优』及等价表述）"、R23.5 要求"不基于 Dense Grid 建立 Regret 体系或搜索
效率对照"。这两条约束的可观测形式已经有明确归属：`requirements.md`
Requirement 17 的 AC11、`tasks.md` 任务 17.4 的
`tests/unit/test_report_wording.py` 第 2、3 条断言（不出现 `regret` /
`simulations-to-target` / `搜索效率` / `收敛速度`、且 `runs` 派生的过程量
日志字段不进入结论段渲染上下文键）；"结论用语只取自模板已定义集合"这件事
本身发生在报告渲染层（`report` 包，任务 26.x，`design.md` §16 T16 与 T22
分列 Dense Grid 与报告为两个不同任务）——`tiered_grid_scan()` 是 `reference`
包里的一个数据采集函数，不渲染任何文本、不拼接任何结论语句，它没有、也
不应该有一条"检查即将写出的字符串是否落在禁用词表之外"的代码路径（那条
检查已经在 17.4 的静态测试里，针对的是 `report` 模板本身，不是针对本函数
的调用点）。

因此本函数在"结果标注"这件事上唯一能做、且已经做到的是：让返回的
`DenseGridResult.task_id` 对应的 `tasks` 行带有 `task_kind='tiered_grid'`
这一可查询、机器可读的标记（见上一节）。未来的报告渲染层按 `task_kind`
取值选择渲染模板分支（`'dense_grid'` → 常规参考扫描措辞，`'tiered_grid'`
→ 近似参考措辞），这是一个"数据携带足够信息供下游正确渲染"的设计，不是
"本函数自己校验措辞合法性"的设计——与 `DenseGridResult` 不需要为
"是否近似"专设一个布尔字段（见下一节）是同一个判断：区分信息已经存在于
调用方一定知道的事实（"我调的是哪个函数"→写入哪个 `task_kind`）里，不需要
在数据形状上再重复表达一遍。

**不基于 Dense Grid 建立 Regret 体系或搜索效率对照**（R23.5）在本函数
实现层面等价于一句空话式的确认："本函数确实没有这样的代码"——`_run_grid_
scan()`/`tiered_grid_scan()` 全文不计算、不存储、不返回任何"与 Dense Grid
144 点比较"的量（不读取任何 `task_kind='dense_grid'` 的历史任务、不计算
"36 点结果与 144 点结果的差异"、不产出"36 点比 144 点节省了多少启动次数"
这类指标）。这与"过程量隔离"（`runs` 派生的日志字段不进结论段渲染上下文
键，L23-4）是同一族约束在不同层的体现：本函数所在的层（`reference`）没有
渲染上下文这个概念，它的"不做"体现为"这段代码里不存在"，可观测的静态
断言仍然落在 17.4；本节只是记录"本函数确认自己不是那类需要被 17.4 捕获
的违规代码"这一事实，供未来审阅者快速核对。

### 返回类型：复用 `DenseGridResult`，不新增字段、不新增类型

`DenseGridResult`（`task_id: str`, `points: tuple[DenseGridPoint, ...]`）
的两个字段对 36 点结果同样适用：`task_id` 是"这次扫描对应哪条 `tasks`
行"，`points` 是"每个候选各自的坐标与 `TierResult`"——形状与"这是 144 点
还是 36 点扫描"完全无关，36 点只是 `points` 元组长度不同（36 而不是 144），
不需要任何结构性区分。是否"近似参考"这一事实（上一节已论证）由 `task_id`
对应的 `tasks.task_kind` 承载，不需要在 `DenseGridResult` 上加一个
`is_tiered: bool` 或类似字段——加了这样一个字段会造成两份彼此独立、需要
保持同步的"这是不是分层扫描"的真相来源（内存里的这个布尔值 vs 数据库里
的 `task_kind` 列），而调用方本来就必然知道自己调的是 `dense_grid_scan()`
还是 `tiered_grid_scan()`（这是两个不同名字的函数，不是同一个函数按参数
分支），从调用点本身就能得到这个区分，不需要从返回值里再读一遍。这与
21.1 落地时"`DenseGridResult` 不是 `response_surface()` 的必需输入、只是
补充信息"一节的论证同源：本文件新增的 dataclass 只在"确有必要携带调用方
无法从别处得到的信息"时才扩展形状，"是否近似"不满足这个条件。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

import matplotlib

matplotlib.use("Agg")  # 无 GUI 后端：本模块只落盘 PNG，不显示交互窗口
import matplotlib.pyplot as plt
import numpy as np

from poweragent.config.hashing import constraints_hash as _constraints_hash_of
from poweragent.config.hashing import format_tick_literal
from poweragent.config.hashing import metrics_hash as _metrics_hash_of
from poweragent.config.schema import (
    ConstraintsConfig,
    DesignSpace,
    MetricsConfig,
    ModelConfig,
    TaskConfig,
)
from poweragent.config.ticks import expand_all_ticks
from poweragent.controller.budget import BudgetLedger
from poweragent.controller.run_task import TierResult, run_tier
from poweragent.controller.scenario import compute_scenario_set_hash, evaluation_rows
from poweragent.controller.scenario import ScenarioSpec as CtrlScenarioSpec
from poweragent.sim.engine import MatlabSession
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Candidate, ScenarioSpec as RepoScenarioSpec, Store

__all__ = [
    "DenseGridValidationError",
    "ValidationOutcomeLike",
    "ValidateFn",
    "DenseGridPoint",
    "DenseGridResult",
    "dense_grid_scan",
    "DenseGridResponseSurfaceError",
    "response_surface",
    "estimate_dense_grid_engine_starts",
    "tiered_grid_scan",
]

# 与 `controller/run_task.py`/`controller/preflight.py` 引入
# `baseline_gate_passed`/`checkpoint1_rejected` 同一先例：`tasks.stop_reason`
# 恰四值的封闭性约束只限定于 `should_stop()` 判定停止这一条路径，Dense Grid
# 从不经过 `should_stop()`，见模块 docstring"stop_reason"一节。
_DENSE_GRID_COMPLETED_STOP_REASON = "dense_grid_completed"
# `tiered_grid_scan()`（任务 21.3）的独立 stop_reason，见模块 docstring
# "`task_kind`"一节：与 `dense_grid_completed` 同一先例、不复用同一常量，
# 使调用方仅凭 `tasks` 表既有列即可区分两类扫描，不需要联表查询
# `candidates.origin`。
_TIERED_GRID_COMPLETED_STOP_REASON = "tiered_grid_completed"


class DenseGridValidationError(RuntimeError):
    """`validate_fn` 对 144 个网格点的校验结果不满足"全部 `accepted`、
    `rejected` 为空"这一前提时抛出（见模块 docstring"校验先于任何落库"
    一节）。抛出前不创建任何 `tasks`/`candidates` 行、不启动任何仿真。"""


class ValidationOutcomeLike(Protocol):
    """`validate_fn` 返回值的最小契约：与 `design.md` §6.7 的
    `ValidationOutcome` 形状兼容的最小子集（本文件只读取 `accepted` 与
    `rejected` 两个属性，`notes` 在 `mode='grid'` 下恒为空，本文件不读取）。
    不从尚不存在的 `agent/validate.py` 导入具体类型（见模块 docstring）。
    """

    accepted: Sequence[Candidate]
    rejected: Sequence[tuple[Mapping[str, float], str]]


# `validate_fn` 的调用签名：`validate_fn(raw, *, design_space, tested, mode)`，
# 与 `design.md` §6.7/§7.2 的 `validate()` 字面签名逐参数对应（见模块
# docstring）。`Callable[..., ValidationOutcomeLike]` 是 kwonly 参数在
# `Callable` 类型标注下的既有局限（标准库 `typing` 无法精确表达 kwonly 参数
# 签名），真实的调用约定见上方 Protocol 与模块 docstring 的示例代码块。
ValidateFn = Callable[..., ValidationOutcomeLike]


@dataclass(frozen=True, slots=True)
class DenseGridPoint:
    """`DenseGridResult.points` 的单个元素：一个网格点候选的仿真结果。

    - `candidate_id` / `rcomp` / `ccomp`：该候选的身份与坐标（`rcomp`/`ccomp`
      为浮点数值，供 `response_surface()`——若选择直接消费本字段而非重新
      查库——按坐标定位响应面上的格点）。
    - `tier_result`：`run_tier()` 对该候选在 Evaluation 集上执行的完整
      返回值。`tier_result.passed=False` 表示该候选未能取得完整的有效
      Evaluation 证据集（见模块 docstring"单候选失败不终止整个扫描"一节），
      `response_surface()`（任务 21.2）应据此判定该网格点是否"标注为
      无效"（R23.3），本字段不自行做这一判定（判定逻辑属于 21.2 的职责，
      需要结合具体绘制的是哪个指标）。
    """

    candidate_id: str
    rcomp: float
    ccomp: float
    tier_result: TierResult


@dataclass(frozen=True, slots=True)
class DenseGridResult:
    """`dense_grid_scan()` 的返回值（本文件新增，扩展 `design.md` §6.12
    字面签名"返回 `task_id: str`"，见模块 docstring 最后一节）。

    - `task_id`：新建的 `dense_grid` 任务标识，与 `design.md` 字面签名的
      返回值完全兼容——`response_surface(task_id, store, out_dir)`
      （任务 21.2）未来接线时只需要这一个字段。
    - `points`：144 个 `DenseGridPoint` 的元组，顺序为
      `_build_grid_candidates()` 的笛卡尔积展开顺序（rcomp 外层、ccomp
      内层，见该函数文档）。
    """

    task_id: str
    points: tuple[DenseGridPoint, ...]


def _to_repo_scenario_spec(spec: CtrlScenarioSpec) -> RepoScenarioSpec:
    """`controller.scenario.ScenarioSpec`（本地临时类型）→
    `store.repo.ScenarioSpec`（`run_tier()` 实际要求的跨模块契约类型）的
    显式 1:1 字段拷贝。

    与 `controller/run_task.py` 的私有 `_to_repo_scenario()`、
    `controller/preflight.py` 的私有 `_to_repo_scenario_spec()` 内容等价但
    独立定义——跨模块引用另一个模块的下划线前缀私有名称违反该前缀本身传达的
    "仅供本模块内部使用"约定，见 `controller/run_task.py` 模块 docstring
    "`scenario_rows` 的类型"一节已确立的处理原则。
    """
    return RepoScenarioSpec(
        scenario_id=spec.scenario_id,
        tier=spec.tier,
        model_variant=spec.model_variant,
        require_margin=spec.require_margin,
        vin_v=spec.vin_v,
        temp_c=spec.temp_c,
        load_start_a=spec.load_start_a,
        load_end_a=spec.load_end_a,
        slew_a_per_us=spec.slew_a_per_us,
        spec_version=spec.spec_version,
    )


def _build_grid_candidates(
    design_space: DesignSpace,
) -> tuple[dict[str, float], ...]:
    """`design_space.variables.rcomp.ticks` × `ccomp.ticks` 的笛卡尔积
    （`requirements.md` R23.2；`design.md` §6.12），展开为 144 个
    `{"rcomp": <float>, "ccomp": <float>}` 字典。

    `config.ticks.expand_all_ticks()`（任务 5.9）返回的是 `.12g` 规范化的
    十进制**字符串**元组（该模块文档"为什么返回字符串"一节：保证 prompt
    字面量与校验档位值是同一组文本）。本函数经 `float()` 转换为数值——网格
    点集的数值用途（作为 `Candidate.parameters_si`、作为仿真参数注入、作为
    `response_surface()` 的坐标轴取值）需要真正的浮点数，不是字符串；
    `validate_fn` 内部若需要按字符串重新比对档位命中，可以自行对传入的浮点
    值再调用 `format_tick_literal()`，与 `agent.validate` 真实实现（任务
    24.3）对键集合/合法域/档位检查的既定做法一致——`snap_to_tick()`（design.md
    §6.7）本身接受 `float` 而非字符串，这确认了传给 `validate_fn` 的候选
    取值应为浮点数，不是本函数自行发明的选择。

    展开顺序：rcomp 外层循环、ccomp 内层循环（"矩阵读法"：先固定行再遍历
    列），与 `expand_ticks()`/`expand_all_ticks()` 各自返回的升序档位顺序
    一致。这个顺序本身不影响 CP-2/R23.2 的正确性（validate 与后续落库均不
    依赖候选被处理的相对顺序），只是为 `DenseGridResult.points` 与未来
    `response_surface()` 若直接消费该顺序时提供一个确定、可预期的排列。
    """
    ticks_by_variable = expand_all_ticks(
        # `expand_all_ticks()` 接受 `ConstraintsConfig`，但只读取其
        # `design_space` 字段；本函数的调用方（`dense_grid_scan()`）已经
        # 持有完整的 `ConstraintsConfig`，为了不在本函数签名里重复引入一个
        # 只用得到一半的形参类型，这里改造一个只含 `design_space` 属性、
        # 供 `expand_all_ticks()` 读取的最小适配对象。
        _MinimalConstraintsAdapter(design_space)
    )
    rcomp_ticks = tuple(float(v) for v in ticks_by_variable["rcomp"])
    ccomp_ticks = tuple(float(v) for v in ticks_by_variable["ccomp"])

    return tuple(
        {"rcomp": rcomp, "ccomp": ccomp}
        for rcomp, ccomp in product(rcomp_ticks, ccomp_ticks)
    )


@dataclass(frozen=True, slots=True)
class _MinimalConstraintsAdapter:
    """`expand_all_ticks(constraints_cfg: ConstraintsConfig)` 只读取
    `constraints_cfg.design_space` 一个字段（见该函数实现）；本类是一个
    只暴露这一个属性的最小适配对象，供 `_build_grid_candidates()` 在只有
    `DesignSpace` 而不便于（也不需要）构造一个完整 `ConstraintsConfig`
    副本时复用 `expand_all_ticks()`，不重复实现该函数内部的字段展开逻辑。
    """

    design_space: DesignSpace


def _even_indexed_subset(ticks: Sequence[str]) -> tuple[str, ...]:
    """给定 `expand_ticks()`/`expand_all_ticks()` 返回的档位文本序列，取
    偶数索引子集（`0, 2, 4, ...`）——12 档输入得到 6 档输出（`tasks.md`
    任务 21.3；`requirements.md` R23.4；`design.md` §6.12/§13 L23-2）。

    等比档位下该子集仍是等比数列（公比为原公比的平方），因此
    `tick_distance` 与响应面的对数等距性质在子集上都保持不变；本函数不
    重新校验这一性质，它由 `config/schema.py` 的
    `_check_ticks_geometric_and_tolerance`（任务 5.8）在配置加载期对原始
    12 档的等比性已经强制、并被数学关系保留到子集上（见本文件模块
    docstring"点集构造"一节）。
    """
    return tuple(ticks[i] for i in range(0, len(ticks), 2))


def _build_tiered_grid_candidates(
    design_space: DesignSpace,
) -> tuple[dict[str, float], ...]:
    """`design_space.variables.rcomp.ticks` 与 `ccomp.ticks` 各自偶数索引
    子集（12 档 → 6 档）的笛卡尔积，6×6 = 36 个 `{"rcomp": <float>,
    "ccomp": <float>}` 字典（`tasks.md` 任务 21.3；`requirements.md`
    R23.4）。

    与 `_build_grid_candidates()` 逐行对应，唯一差异是在 `float()` 转换
    之前对两个变量各自的档位文本先经 `_even_indexed_subset()` 截取子集
    （见本文件模块 docstring"点集构造"一节：两个函数保持最小差异，便于
    核对两者是否取自同一份档位定义）。展开顺序同样为 rcomp 外层、ccomp
    内层。
    """
    ticks_by_variable = expand_all_ticks(_MinimalConstraintsAdapter(design_space))
    rcomp_ticks = tuple(
        float(v) for v in _even_indexed_subset(ticks_by_variable["rcomp"])
    )
    ccomp_ticks = tuple(
        float(v) for v in _even_indexed_subset(ticks_by_variable["ccomp"])
    )

    return tuple(
        {"rcomp": rcomp, "ccomp": ccomp}
        for rcomp, ccomp in product(rcomp_ticks, ccomp_ticks)
    )


def estimate_dense_grid_engine_starts(
    scan_point_count: int,
    task_cfg: TaskConfig,
    metrics_cfg: MetricsConfig,
) -> int:
    """`R23.4` 判据前半部分"扫描点数 ×（Evaluation 行数，含 `require_margin`
    行的每候选额外启动数）"的纯算术实现，不做任何 I/O、不比较任何预算上限
    （见本文件模块 docstring"触发判据的归属"一节：调用方在 `dense_grid_
    scan()`/`tiered_grid_scan()` 之间选择时使用本函数，本函数自身不属于
    也不调用两者中的任何一个）。

    公式：对 `evaluation_rows(task_cfg)` 的每一行贡献 1 次启动，
    `require_margin=True` 的行再加
    `metrics_cfg.margin_extraction.extra_engine_starts_per_candidate` 次
    （与 `sim/simulate.py` 对 `require_margin=true` 场景的既定行为
    `engine_starts >= 1 + extra_engine_starts_per_candidate` 逐字对应，
    任务 11.2），全部求和后乘以 `scan_point_count`。

    调用方把 `scan_point_count=144` 传入以判断是否需要改跑
    `tiered_grid_scan()`（`estimated_wallclock = probe_single_run_s ×
    本函数返回值`，与 `gate_budget_max_engine_starts`/
    `gate_budget_max_wallclock_hours × 3600` 比较，见模块 docstring 的
    调用示例），或把 `scan_point_count=36` 传入以确认回退后的点集本身
    仍在预算内（该确认步骤是可选的：真正的预算上限判定始终由
    `BudgetLedger.reserve()` 在执行期兜底，见模块 docstring 对应一节）。
    """
    per_candidate_starts = 0
    for row in evaluation_rows(task_cfg):
        per_candidate_starts += 1
        if row.require_margin:
            per_candidate_starts += (
                metrics_cfg.margin_extraction.extra_engine_starts_per_candidate
            )
    return scan_point_count * per_candidate_starts


def _run_grid_scan(
    raw_candidates: tuple[dict[str, float], ...],
    *,
    task_kind: str,
    stop_reason: str,
    task_id_prefix: str,
    model_cfg: ModelConfig,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
    task_cfg: TaskConfig,
    store: Store,
    session: MatlabSession,
    artifacts: ArtifactStore,
    execution_env_hash: str,
    frozen_fingerprint: str,
    frozen_model_package_hash: str,
    validate_fn: ValidateFn,
    gate_budget_max_engine_starts: int,
    gate_budget_max_wallclock_hours: float,
    gate_task_id: str | None,
    base_dir: str | Path,
) -> DenseGridResult:
    """`dense_grid_scan()`（144 点）与 `tiered_grid_scan()`（36 点）共用的
    私有实现（`tasks.md` 任务 21.1/21.3；见本文件模块 docstring"内部结构
    复用"一节：两个公开函数除点集构造与三个标识字面量外逐字相同，本函数
    承载共享部分，避免 ~100 行重复）。

    流程（与 21.1 落地时确立的既定顺序完全一致，此处不重复各步骤的判断
    依据，见模块顶部 docstring 对应章节）：
      1. 以 `mode="grid"` 调用 `validate_fn`；`rejected` 非空或
         `accepted` 数量不等于 `raw_candidates` 数量 ⟹
         `DenseGridValidationError`（不创建任何 `tasks`/`candidates` 行）。
      2. `store.create_task(task_kind=task_kind, ...)`，独立
         `BudgetLedger` 绑定该 `resolved_task_id`。
      3. 对 `accepted` 中的每个 `Candidate`，经
         `store.persist_candidate(origin='dense_grid')` 落库（`origin`
         值的选择见模块 docstring"`task_kind`"一节：两个扫描规模共用
         同一个 `origin` 取值）。
      4. `evaluation_rows(task_cfg)` 取得 Evaluation 场景行，对每个候选
         调用一次 `run_tier(candidate, 'evaluation', ...)`，单候选失败
         不终止扫描，`BudgetExhaustedError` 原样传播。
      5. `store.finish_task(resolved_task_id, stop_reason=stop_reason,
         cause=None)`。
      6. 返回 `DenseGridResult(task_id=resolved_task_id, points=...)`。
    """
    outcome = validate_fn(
        raw_candidates,
        design_space=constraints_cfg.design_space,
        tested=(),
        mode="grid",
    )

    if outcome.rejected:
        raise DenseGridValidationError(
            f"{task_kind}: expected all "
            f"{len(raw_candidates)} grid points to be accepted by "
            f"validate_fn(mode='grid'), but {len(outcome.rejected)} were "
            f"rejected: {outcome.rejected!r}"
        )
    if len(outcome.accepted) != len(raw_candidates):
        raise DenseGridValidationError(
            f"{task_kind}: validate_fn returned "
            f"{len(outcome.accepted)} accepted candidates, expected exactly "
            f"{len(raw_candidates)} (one per grid point)"
        )

    resolved_task_id = gate_task_id or f"{task_id_prefix}_{uuid.uuid4().hex}"

    constraints_hash_value = _constraints_hash_of(constraints_cfg.model_dump(mode="json"))
    metrics_hash_value = _metrics_hash_of(metrics_cfg.model_dump(mode="json"))
    scenario_set_hash_value = compute_scenario_set_hash(task_cfg)

    store.create_task(
        task_id=resolved_task_id,
        simulation_only=task_cfg.simulation_only,
        task_kind=task_kind,
        model_package_hash=frozen_model_package_hash,
        metrics_hash=metrics_hash_value,
        constraints_hash=constraints_hash_value,
        scenario_set_hash=scenario_set_hash_value,
        execution_env_hash=execution_env_hash,
        calibration_hash="",
        budget_max_starts=gate_budget_max_engine_starts,
    )

    grid_ledger = BudgetLedger(
        store,
        resolved_task_id,
        gate_budget_max_engine_starts,
        gate_budget_max_wallclock_hours * 3600.0,
    )

    for candidate in outcome.accepted:
        store.persist_candidate(
            candidate, task_id=resolved_task_id, origin="dense_grid"
        )

    evaluation_scenarios = tuple(
        _to_repo_scenario_spec(spec) for spec in evaluation_rows(task_cfg)
    )

    points: list[DenseGridPoint] = []
    for candidate in outcome.accepted:
        tier_result = run_tier(
            candidate,
            "evaluation",
            evaluation_scenarios,
            model_cfg=model_cfg,
            metrics_cfg=metrics_cfg,
            constraints_cfg=constraints_cfg,
            session=session,
            store=store,
            artifacts=artifacts,
            budget_ledger=grid_ledger,
            task_id=resolved_task_id,
            execution_env_hash=execution_env_hash,
            frozen_fingerprint=frozen_fingerprint,
            frozen_model_package_hash=frozen_model_package_hash,
            base_dir=base_dir,
        )
        points.append(
            DenseGridPoint(
                candidate_id=candidate.candidate_id,
                rcomp=float(candidate.parameters_si["rcomp"]),
                ccomp=float(candidate.parameters_si["ccomp"]),
                tier_result=tier_result,
            )
        )

    store.finish_task(
        resolved_task_id,
        stop_reason=stop_reason,
        cause=None,
    )

    return DenseGridResult(task_id=resolved_task_id, points=tuple(points))


def dense_grid_scan(
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
    validate_fn: ValidateFn,
    gate_budget_max_engine_starts: int,
    gate_budget_max_wallclock_hours: float,
    gate_task_id: str | None = None,
    base_dir: str | Path = ".",
) -> DenseGridResult:
    """一次性 12×12 参考扫描（`design.md` §6.12；`requirements.md` R23.1,
    R23.2；属性 CP-2；门禁 G-M3-1）。

    签名相对 `design.md` §6.12 字面签名（`dense_grid_scan(task_cfg, *, store,
    session) -> str`）的全部必要扩展、`validate_fn` 的软依赖处理、场景 tier
    语义、预算隔离、`stop_reason` 新值与返回类型扩展，见本文件模块顶部
    docstring 对应章节，此处不重复。

    本函数（任务 21.1 落地）与 `tiered_grid_scan()`（任务 21.3 新增）共用
    的流程细节收在私有辅助函数 `_run_grid_scan()` 中（见模块 docstring
    "内部结构复用"一节）；本函数只负责构造 144 点原始候选并转发调用，
    公开签名、参数校验顺序与对外行为相对 21.1 落地时**没有任何变化**。
    """
    raw_candidates = _build_grid_candidates(constraints_cfg.design_space)
    return _run_grid_scan(
        raw_candidates,
        task_kind="dense_grid",
        stop_reason=_DENSE_GRID_COMPLETED_STOP_REASON,
        task_id_prefix="dense_grid",
        model_cfg=model_cfg,
        metrics_cfg=metrics_cfg,
        constraints_cfg=constraints_cfg,
        task_cfg=task_cfg,
        store=store,
        session=session,
        artifacts=artifacts,
        execution_env_hash=execution_env_hash,
        frozen_fingerprint=frozen_fingerprint,
        frozen_model_package_hash=frozen_model_package_hash,
        validate_fn=validate_fn,
        gate_budget_max_engine_starts=gate_budget_max_engine_starts,
        gate_budget_max_wallclock_hours=gate_budget_max_wallclock_hours,
        gate_task_id=gate_task_id,
        base_dir=base_dir,
    )


def tiered_grid_scan(
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
    validate_fn: ValidateFn,
    gate_budget_max_engine_starts: int,
    gate_budget_max_wallclock_hours: float,
    gate_task_id: str | None = None,
    base_dir: str | Path = ".",
) -> DenseGridResult:
    """分层参考扫描回退：`ticks` 偶数索引子集（12 档 → 6 档）的笛卡尔积，
    6×6 = 36 点（`design.md` §6.12/§13；`requirements.md` R23.4, R23.5；
    `tasks.md` 任务 21.3；门禁 G-M3-1）。

    签名与 `dense_grid_scan()` 逐参数相同（同一组 kwonly 参数，见模块
    docstring"`tiered_grid_scan()`……的实现"一节"内部结构复用"小节：两个
    函数在参数集合上保持一致，便于调用方在两者之间切换时不需要改动除
    函数名之外的任何调用代码）。是否应该调用本函数而非
    `dense_grid_scan()`，由调用方使用 `estimate_dense_grid_engine_starts()`
    自行判断（本函数不做该判断，见模块 docstring"触发判据的归属"一节）；
    本函数被调用后无条件执行 36 点扫描，不重新验证这一决定。

    流程与 `dense_grid_scan()` 完全相同（共用 `_run_grid_scan()`），唯一
    差异：
      - 点集为 `_build_tiered_grid_candidates(constraints_cfg.design_space)`
        的 36 个候选，而非 144 个。
      - 落库的 `tasks.task_kind` 为 `'tiered_grid'`，`origin` 仍为
        `'dense_grid'`（见模块 docstring"`task_kind`"一节）。
      - 收尾的 `stop_reason` 为 `'tiered_grid_completed'`。
      - `gate_task_id` 为 `None` 时生成的默认 `task_id` 前缀为
        `tiered_grid`。

    "结果标注为近似参考"（R23.4）与"不建立 Regret 体系"（R23.5）在本函数
    的落实范围与理由，见模块 docstring"结果标注……本函数的职责边界"一节
    ——本函数只保证 `tasks.task_kind='tiered_grid'` 这一机器可读标记存在，
    不渲染、不校验任何结论文本。
    """
    raw_candidates = _build_tiered_grid_candidates(constraints_cfg.design_space)
    return _run_grid_scan(
        raw_candidates,
        task_kind="tiered_grid",
        stop_reason=_TIERED_GRID_COMPLETED_STOP_REASON,
        task_id_prefix="tiered_grid",
        model_cfg=model_cfg,
        metrics_cfg=metrics_cfg,
        constraints_cfg=constraints_cfg,
        task_cfg=task_cfg,
        store=store,
        session=session,
        artifacts=artifacts,
        execution_env_hash=execution_env_hash,
        frozen_fingerprint=frozen_fingerprint,
        frozen_model_package_hash=frozen_model_package_hash,
        validate_fn=validate_fn,
        gate_budget_max_engine_starts=gate_budget_max_engine_starts,
        gate_budget_max_wallclock_hours=gate_budget_max_wallclock_hours,
        gate_task_id=gate_task_id,
        base_dir=base_dir,
    )


class DenseGridResponseSurfaceError(RuntimeError):
    """`response_surface()` 在给定 `task_id` 下找不到任何 `origin='dense_grid'`
    候选时抛出（见模块 docstring"零候选时的处理"一节）。抛出前不创建
    `out_dir`、不写入任何文件。"""


_RESPONSE_SURFACE_INVALID_COLOR = "lightgray"
_RESPONSE_SURFACE_FILENAMES: Mapping[str, str] = {
    "primary": "response_surface_primary.png",
    "phase_margin": "response_surface_phase_margin.png",
}


def _fetch_dense_grid_candidates(
    store: Store, task_id: str
) -> list[tuple[str, float, float]]:
    """查询该 `task_id` 下 `origin='dense_grid'` 的候选，返回
    `(candidate_id, rcomp, ccomp)` 三元组列表（见模块 docstring"查询方式"
    一节）。`parameters_si` 落库为 `canonical_json` 规范化文本，本函数
    经 `json.loads()` 解析取 `rcomp`/`ccomp` 两键。
    """
    rows = store.connection.execute(
        "SELECT candidate_id, parameters_si FROM candidates "
        "WHERE task_id = ? AND origin = 'dense_grid'",
        (task_id,),
    ).fetchall()
    result: list[tuple[str, float, float]] = []
    for candidate_id, parameters_si_json in rows:
        params = json.loads(parameters_si_json)
        result.append((candidate_id, float(params["rcomp"]), float(params["ccomp"])))
    return result


def _fetch_max_valid_metric(store: Store, task_id: str, candidate_id: str, metric_id: str) -> float | None:
    """该候选在 `task_id` 下、`metric_id` 上全部 `runs` 行中有效
    `MetricResult.value` 的最大值；不存在任何有效值时返回 `None`（见模块
    docstring"查询方式"一节的聚合口径，与 `WORST_CASE_SQL` 对主目标的取法
    一致：取有效值的最大值）。
    """
    row = store.connection.execute(
        "SELECT MAX(m.value) FROM metric_results m "
        "JOIN runs r ON r.run_id = m.run_id "
        "WHERE r.task_id = ? AND r.candidate_id = ? AND m.metric_id = ? "
        "AND m.valid = 1",
        (task_id, candidate_id, metric_id),
    ).fetchone()
    return None if row is None else row[0]


def _plot_response_surface_grid(
    *,
    rcomp_ticks: Sequence[str],
    ccomp_ticks: Sequence[str],
    grid: np.ndarray,
    metric_id: str,
    out_path: Path,
) -> None:
    """把 `grid`（形状 `(len(rcomp_ticks), len(ccomp_ticks))`，无效点为
    `numpy.nan`）渲染为一张响应面图并保存到 `out_path`（见模块 docstring
    "无效点"一节）。"""
    masked = np.ma.masked_invalid(grid)
    cmap = matplotlib.colormaps["viridis"].copy()
    cmap.set_bad(color=_RESPONSE_SURFACE_INVALID_COLOR)

    fig, ax = plt.subplots(figsize=(8, 6))
    im = ax.imshow(masked, origin="lower", aspect="auto", cmap=cmap)
    ax.set_xticks(range(len(ccomp_ticks)))
    ax.set_xticklabels(ccomp_ticks, rotation=90, fontsize=6)
    ax.set_yticks(range(len(rcomp_ticks)))
    ax.set_yticklabels(rcomp_ticks, fontsize=6)
    ax.set_xlabel("ccomp (F)")
    ax.set_ylabel("rcomp (ohm)")
    ax.set_title(f"Dense Grid response surface: {metric_id}\n"
                 f"(grey = invalid / no data, not interpolated)")
    fig.colorbar(im, ax=ax, label=metric_id)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def response_surface(
    task_id: str,
    store: Store,
    out_dir: Path,
    *,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
) -> list[Path]:
    """对 `objective.primary` 聚合值与 `phase_margin` 各输出一张以 rcomp/
    ccomp 档位为两轴的响应面图（`design.md` §6.12；`requirements.md` R23.3；
    `tasks.md` 任务 21.2）。

    签名相对 `design.md` §6.12 字面签名（`response_surface(task_id, store,
    out_dir) -> list[Path]`）的扩展（`metrics_cfg`/`constraints_cfg` 两个
    kwonly 参数）、`out_dir` 直接落盘而不经 `ArtifactStore` 的判断依据、
    查询方式、无效点的遮罩处理、坐标轴与零候选报错的选择，见本文件模块
    顶部 docstring"`response_surface()`（任务 21.2）的实现"一节，此处不
    重复。

    流程：
      1. `_fetch_dense_grid_candidates()` 查询该 `task_id` 下全部
         `origin='dense_grid'` 候选；为空 ⟹ `DenseGridResponseSurfaceError`。
      2. `expand_all_ticks(constraints_cfg)` 取 rcomp/ccomp 档位文本坐标轴
         （与 `_build_grid_candidates()` 同一路径）。
      3. 对每个候选，把其 `(rcomp, ccomp)` 经 `format_tick_literal()` 规范化
         后与坐标轴文本匹配定位到网格格子；匹配不到的候选忽略。
      4. 对 `objective.primary.metric_id` 与 `"phase_margin"` 两个指标各自
         构造一个 `(len(rcomp_ticks), len(ccomp_ticks))` 的 `float` 网格
         （初始全为 `numpy.nan`），为每个匹配到格子的候选查询
         `_fetch_max_valid_metric()`；有效值填入对应格子，`None`（无效/
         缺失）保留 `numpy.nan`。
      5. `out_dir.mkdir(parents=True, exist_ok=True)`，两个网格各渲染一张
         PNG，返回两个 `Path` 组成的列表（固定顺序：primary 在前）。
    """
    dense_grid_candidates = _fetch_dense_grid_candidates(store, task_id)
    if not dense_grid_candidates:
        raise DenseGridResponseSurfaceError(
            f"response_surface: task_id={task_id!r} 下不存在任何 "
            "origin='dense_grid' 的候选，无法绘制响应面图（该 task_id 可能"
            "拼写错误、指向了非 dense_grid 任务，或该任务在写入任何候选之前"
            "就已终止）"
        )

    ticks_by_variable = expand_all_ticks(constraints_cfg)
    rcomp_ticks = ticks_by_variable["rcomp"]
    ccomp_ticks = ticks_by_variable["ccomp"]
    rcomp_index = {text: i for i, text in enumerate(rcomp_ticks)}
    ccomp_index = {text: i for i, text in enumerate(ccomp_ticks)}

    # 候选 → 网格格子的定位（文本精确匹配，见模块 docstring"恰好 144 个网格
    # 坐标"一节）；匹配不到的候选忽略、不计入任何格子。
    located: list[tuple[str, int, int]] = []
    for candidate_id, rcomp, ccomp in dense_grid_candidates:
        r_idx = rcomp_index.get(format_tick_literal(rcomp))
        c_idx = ccomp_index.get(format_tick_literal(ccomp))
        if r_idx is not None and c_idx is not None:
            located.append((candidate_id, r_idx, c_idx))

    primary_metric_id = metrics_cfg.objective.primary.metric_id
    grid_shape = (len(rcomp_ticks), len(ccomp_ticks))

    metric_ids: Mapping[str, str] = {"primary": primary_metric_id, "phase_margin": "phase_margin"}
    result_paths: list[Path] = []

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for key in ("primary", "phase_margin"):
        metric_id = metric_ids[key]
        grid = np.full(grid_shape, np.nan, dtype=float)
        for candidate_id, r_idx, c_idx in located:
            value = _fetch_max_valid_metric(store, task_id, candidate_id, metric_id)
            if value is not None:
                grid[r_idx, c_idx] = value

        out_path = out_dir / _RESPONSE_SURFACE_FILENAMES[key]
        _plot_response_surface_grid(
            rcomp_ticks=rcomp_ticks,
            ccomp_ticks=ccomp_ticks,
            grid=grid,
            metric_id=metric_id,
            out_path=out_path,
        )
        result_paths.append(out_path)

    return result_paths
