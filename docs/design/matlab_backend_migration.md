# Simulink 后端接入

把仿真主链路接到 MATLAB/Simulink 的实施记录。已完成并端到端验证。

**路线：双后端并列。** MATLAB/Simulink 作高保真后端，Python 后端保留为 CI 基线与
独立交叉验证。三条理由：

1. CI 环境没有 MATLAB。完全替换会让 199 项测试掉大半，而"每次 push 全量执行"是
   README 的主张之一。
2. Python 后端保留下来就是 MATLAB 结果的独立交叉验证，比原有的"平均/开关双模型
   互核"更有力——那两个模型是同一份代码算的。
3. README 里三个已复算结果（144 点参考扫描、LLM 与随机对照、约束集缺陷的发现）
   都是 Python 后端产出的。双后端并列下它们继续成立。

**许可证不是选型理由。** 2026-09-10 实测顺序 3 次独立 engine 与并发 2 个 engine
全部成功签出并跑完 `sim()`（`license('inuse')` 里出现 `simulink`，是真实占用席位）。
`.matlab_verify/build_log.txt` 里的 `-4,132` 与 README「并发常满」反映的是过去某一
时刻的状态，不能用来推断当前可用性。

## 一、架构上的切换点

`sim/simulate.py` 与后端之间只有 `session.call(fn, *args, nargout=)` 一个交互面，
`MatlabSession` 与 `PythonSession` 同形。因此"换后端"是换一个传给 `simulate()` 的
对象，`simulate()` 本身一行没改。三个脚本各加了一个 `--backend {python,matlab}`
（默认 `python`），实现是一个字典查表：

```python
SESSION_CLASSES = {"python": PythonSession, "matlab": MatlabSession}
...
with SESSION_CLASSES[args.backend](base_dir=REPO_ROOT) as session:
```

不引入工厂函数或注册表。`sim/backends/__init__.py` 已明确"两个扩展点是函数边界而非
抽象层"；在 `sim` 包里加 `open_session()` 还会让 `import poweragent.sim` 连带 import
`PythonSession`（依赖 scipy），破坏 `engine.py` 特意保持的"无 MATLAB、无 scipy 也能
import"性质。

默认值 `python` 不是"哪个更好"的判断，而是"不改变既有结论"：`artifacts/` 下已入库的
记录都是 Python 后端产出的，而 `execution_env_hash` 现在含 `sim_backend`，两个后端
的结果按设计不互相命中缓存。

## 二、环境

```
version    : 24.1.0.2537033 (R2024a)
matlabroot : C:\Program Files\MATLAB\R2024a
license('checkout','Simulink')                -> 1
license('checkout','Simulink_Control_Design') -> 1

engine 启动 : 16~24 s
平均模型仿真: 约 0.5 s（1 ms @ MaxStep 0.2 us, ode23tb）
linearize   : 约 6 s
```

**开关模型仿真：首次 29 s，之后 0.5~1 s。** 同一 engine 会话内连续调用 12 次
（每次换一个 `rcomp` 档位）实测：

```
 #            rcomp   墙钟_s   simulate_once.m 报的 elapsed_ms
 1        1519.9111    29.39                            29101
 2        2310.1297     3.23                             1507
 3        3511.1917     1.71                             1572
 4        5336.6992     0.71                              649
 5        8111.3083     0.90                              861
 6~12                                             440~925
────────────────────────────────────────────────────────────
12 次合计                41.2 s
```

首次的 29 秒几乎全是 Simulink 首次编译，不是仿真本身。这一点最初被误判了：早期
每次测量都新起一个 engine 会话，于是反复看到 31~40 s，据此把开关模型的单次成本
当成了 30 秒量级，并在几处文档里做出了偏悲观的估算（"参考扫描要 2.5 小时"、
"`fast_restart` 收益值得优先补"）。真实的稳态成本比那低 30~60 倍。

这个误判本来是可以更早发现的——`runs.elapsed_ms` 列存在且 `SimulationResult`
一直带着这个值，但 `close_run_ok()` 漏写了它（见第四节缺口 15），所以库里查不到
单次耗时，只能靠外部计时，而外部计时又恰好每次都在新会话里做。

用真实 `checkout` 而非 `license('test',...)`——后者只查许可证清单，签不出也返回 1，
`.matlab_verify/check_licenses.py` 用的是后者，所以它的输出与 `build_log.txt` 的失败
并不矛盾。

`matlabengine==24.1.2`（与 R2024a 匹配）需装进项目环境。

## 三、模型资产

`models/buck4ph_averaged.slx` 与 `models/buck4ph_switching.slx` 由
`matlab/build_slx_model.m` 以编程方式生成，`scripts/build_slx_models.py` 驱动：

```bash
python scripts/build_slx_models.py                     # 两个变体
python scripts/build_slx_models.py --variant averaged
```

纯 Simulink 基本块（Gain / Constant / Product / Divide / Sum / Integrator /
Transport Delay / Saturation / Memory / Switch / Relational / Logical / Mux /
Math Function / Clock / Bias），不依赖 Simscape。

**为什么脚本生成而不是手工搭图**：`.slx` 是二进制，不可 diff、不可在评审里逐行看。
而这个模型是"被仿真的对象"，它的每处参数与连线都是结论成立的前提。进版本控制的是
生成脚本，`.slx` 退化为可重新生成的派生产物——参数来源、连线理由、求解器配置全部
可审查。`.slx` 仍独立计入 `model_package_hash`：由脚本生成是当前的工作方式，不是
文件系统强制的约束，有人手工改了它哈希必须变。

**"可重新生成"这句话需要一个前提，而那个前提最初不成立**：`.slx` 的原始字节对重复
生成不确定（同一 spec 生成 4 次得到 4 个不同 sha256），因此计入闭包的必须是它的
**规范化字节**而不是原始字节，否则"重新生成同一个模型"会被门禁判成"模型变了"。
这是缺口 14，详见第四节。修好之后：同一 spec 重复生成 → `model_package_hash` 一致；
改任一模型定义（块参数、块名、连线、求解器）→ 哈希必变。

**参数源唯一**：`models/buck4ph_*.yaml` 既是 Python 后端的入口，也是生成 `.slx` 时
读取的唯一参数源，两个后端的电路参数不可能漂移。为此 `model_package.<variant>` 新增
了 `slx_entry` 字段（可选，缺失时 MATLAB 后端显式报错而不回退到 `entry`），两个入口
字段都计入依赖闭包。

**一处刻意的建模差异**：平均模型的采样保持用 Transport Delay（真延迟），而 Python
平均模型的时域用一阶滞后近似。Python 侧那个近似的理由写在 `sim/backends/margin.py`
里——纯延迟会把 ODE 变成延迟微分方程，scipy 侧要重做求解器与可复现性。Simulink 原生
支持带延迟的连续求解，这个约束不存在，于是同一个模型的时域与频域用同一个延迟表达，
不需要 Python 侧那处时域/频域分裂。

## 四、发现并修复的十六个缺口

前五个读代码发现，其余是真实 MATLAB 上跑出来的。每一个都会让整条链路失败、静默
给出错误结果、让门禁失去区分能力，或让关键的成本数据查不到。最后两个（15、16）
与 MATLAB 无关，是在核查一次真实寻优的输出时顺带查出来的既有缺口。

**1. `block_path` 根段与模型名冲突。** `apply_params.m` 的 `set_param` 要求绝对路径
第一段等于模型名，而 `model.yaml` 的 `injectable_params` 只有一份、根段写死
`buck4ph`，实际模型名是 `buck4ph_averaged` / `buck4ph_switching`。
→ 新增 `matlab/+pa/private/resolve_block_path.m` 替换根段（只按第一个 `/` 切分：
Simulink 用 `//` 转义块名里的斜杠，`strsplit` 会切错）。`apply_params.m` 与
`inspect_model.m` 各改一行。

**2. 波形格式对不上，且 `-v7.3` 读不了。** `collect_signals.m` 落盘的是变量名
`logsout` 的 `Simulink.SimulationData.Dataset` 对象且用 `-v7.3`（HDF5）；
`scipy.io.loadmat` 既不支持 v7.3，也无法把类对象还原成
`eval/metrics.py::_load_waveform()` 期望的 `{signal: {time, data}}` 形状。
→ 重写为按 `io_contract.output_signals` 逐信号取 `Values.Time`/`Values.Data`、
用 `save(..., '-struct', 'payload', '-v7')` 展开为顶层变量。`eval/metrics.py` 与
`waveform_io.py` 一行没改，两个后端共享同一个加载器。

**3. `step_trigger_s` 在 MATLAB 通路里无来源。** `simulate_once.m` 的场景注入白名单
没有阶跃时刻，而 `settling_time`/`overshoot`/`undershoot` 的窗口都是
`[step_trigger, step_trigger_plus_500us]`。
→ Python 侧 `_build_scenario_payload()` 按 `STEP_TRIGGER_FRACTION * stop_time` 算出
后经 `scenario_json` 传入，MATLAB 侧写入模型工作区供负载阶跃块读取，
`collect_signals.m` 再写进波形 MAT。三处用同一个数。

**4. 频响产物存了一个读不了又没人用的类对象。** `save(..., 'margin_data', 'linsys')`
里 `linsys` 是 `ss` 类对象，而下游只读 `margin_data`。
→ 只存 `margin_data`，显式 `-v7`。

**5. `execution_env_hash` 不含后端标识。** 原五个字段在两个后端下完全相同，而它是
`simulation_key` 的七字段之一——同一个 `runs.db` 里 Python 后端跑过的候选会被判为
缓存命中，MATLAB 一次都不跑，记录看起来完全正常。
→ 加第六字段 `sim_backend`，取自会话对象的 `backend_id` 类属性（读事实不读声明，
这样"配置与事实不符"这种失败模式不存在，也就不需要额外一条校验去防它）。

**6. `inspect_model.m` 的信号反查在 R2024a 上静默失效。** 它遍历 `Type='line'` 并读
`get_param(line, 'DataLogging')`，而 R2024a 的 line 对象没有这个参数（信号记录是
**端口**属性）。异常被循环体内的 `try/catch` 吞掉，函数恒返回空 cell → 所有
`output_signals` 判为缺失 → `pa:ContractError` → preflight 失败。
→ 改为遍历 `Type='port', PortType='outport'`。生成模型时须设
`DataLoggingNameMode='Custom'`。

**7. `MatlabSession.__enter__()` 没有 addpath，`pa.*` 一个都调不到。** engine 启动后
cwd 是启动进程的工作目录，`matlab/+pa` 不在搜索路径上，`feval('pa.inspect_model')`
报"函数或变量无法识别"。
→ `MatlabSession` 接 `base_dir` 并在 `__enter__` 里 `addpath`；`+pa` 目录缺失时立即
失败并 `quit` 引擎（否则泄漏一个 MATLAB 进程并继续占用许可证席位）。
附带一个坑：`exist('pa.inspect_model')` 在函数完全可用时也返回 0（不识别包限定名），
可达性检查要用 `which`。

**8. `linearize(model, options)` 两参数形式不被支持。** R2024a 报"下标为 1 的索引超出
范围"（`linearize.m:355`）——它把第二个实参当成 io/op 而不是 options。
→ 显式 `getlinio(model_name)` 后用三参数 `linearize(model_name, io, lin_opt)`。
同时 `UseExactDelayModel='on'` 是必需的：关掉时 `hasdelay=false`，Transport Delay 被
整个丢弃（Pade 默认 0 阶），PM 从 73.02° 变成 80.37°、GM 直接为 0。

**9. `allmargin` 的 `GainMargin` 首元素恒为 0。** 真实输出是向量
`[0, 2.8794, 2.8914, ...]`，首元素对应 `GMFrequency=0`，是两个积分器在 DC 处的伪穿越
（`|T|→∞` 故 `1/|T|→0`）。`_pick_worst_margin_value(prefer_min_abs=True)` 会选中它，
紧接着被 `gain_margin_linear <= 0` 判为提取失败——**每一个**候选都失败。Python 侧频率
网格从 1 Hz 起，结构性地看不到这一项。
→ 新增 `_pick_worst_gain_margin()`，按正值筛选后取最小。

**10. 裕量采集用了错的模型变体。** `run_linear_analysis.m` 的文件头写明"信任
model_path 已由 Python 侧按 primary_method 解析"，而 `simulate()` 传的是**场景变体**
的 `model_cfg_json`——需要采集裕量的评价场景用开关模型，于是
`linear_analysis_on_averaged` 会在开关模型上 `linearize`（右端项在每个开关点不连续，
结果没有环路裕量的意义）。这个缺陷被 Python 后端掩盖：`PythonSession` 内部自己改用
了 `averaged.entry`。
→ 新增 `_resolve_margin_model_path()`，按 `primary_method` 解析，放回它该在的位置。

**11. `local_safe_field` 用 `isfield` 判 MException 的字段。** `isfield` 只对 struct
有效，对**对象**恒返回 false，而 `sim()`/`linearize()` 抛出的正是 MException。该函数
对真实异常永远返回空字符串，连带让 `local_is_engine_transient()` 的全部字符串判据
恒为假——许可证瞬时失败与引擎连接中断会被误判成 `solver_error`，而那两类本该走
`engine_transient` 的重试路径。`map_error.m` 与 `run_linear_analysis.m` 各有一份。
→ 两处同步改为动态字段访问 + `try/catch`。

**12. 载波相位判据踩在浮点边界上，整相 PWM 漏拍。** 周期起点判据是
`mod(t/Tsw - (k-1)/N, 1) * Tsw < dt`，理想起点恰好落在 `frac == 0` 边界上；浮点结果
是 `+1e-16` 还是 `-1e-16` 决定了 `mod` 给出 `1e-16` 还是 `1-1e-16`，后者使判据不成立、
该相该周期完全不导通、电流多下降一个周期、纹波峰峰翻倍。实测相 2 踩中：相电流峰峰
22.47 A 而其余三相 11~12.8 A，`Vout` 频谱随之出现 10~80 kHz 低频成分，
`output_ripple` 比 Python 后端大 4.5 倍（348%）。
→ 载波相位加**半个求解步**偏移，使起点落在判据内部、余量 0.5·dt（10 ns，比浮点误差
大 8 个数量级）；`t_on_min`/`t_on_max` 同样加 0.5·dt 抵消，比较的仍是真实导通时间。

**13. 场景注入白名单在两处副本间漂移。** `simulate_once.m` 与
`run_linear_analysis.m` 各有一份 `local_assign_scenario_to_workspace`，后者的文件头
把复制理由写成"两处都很小，不值得新增共享模块"并留了"如需调整需同步修改"。那句同步
要求实测没有被满足：加 `step_trigger_s` 时只改了前者，于是评价场景（时域跑 switching、
裕量跑 averaged）下 averaged 模型第一次被加载时工作区没有该变量，负载阶跃块的
`Bias='-step_trigger_s'` 无法求值，`linearize` 抛
`Simulink:Parameters:InvParamSetting`，而该异常被归类为"引擎没坏、只是这次没算出
裕量"——每个候选都记为 `extraction_failed`。这个失败模式只在两个变体分别承担时域与
频域时才出现，否则被前者的注入掩盖。
→ 提取为 `matlab/+pa/private/assign_scenario.m`，白名单只有一处。

**14. `.slx` 的字节哈希对重复生成不确定，门禁因此失去区分能力。** 用**完全相同**的
spec 连续生成 4 份 `buck4ph_averaged.slx`，4 份的 sha256 各不相同，文件大小也在
98700~98704 字节之间抖动。而 `model_package_hash()` 是直接
`digest.update(Path(path).read_bytes())` 的。差异散布在 6 个 OPC 条目里，全部与电路
无关：

```
metadata/coreProperties.xml             dcterms:created / dcterms:modified 时间戳
metadata/mwcorePropertiesExtension.xml  文档 uuid
simulink/blockdiagram.xml               ModelUUID + 每个块的 Field Name="UUID"
simulink/modelDictionary.xml            System / Interface 的 uuid
simulink/ScheduleCore.xml               UUID 引用图
simulink/ScheduleEditor.xml             UUID 引用图（元素顺序还会在两种排列间交替）
```

后果不只是"重跑生成脚本会误报"。这个哈希的用途是门禁——"模型变了，旧结论不能再用"
——而它现在测的是"这个二进制文件最近有没有被重新写过"。两件事被混在一起之后，门禁
就没有区分能力了：人会因为它频繁误报而习惯性地"重跑一下记录"或"再审批一次"，那时
真正的模型变更也会被同样地放过去。`configs/model.yaml` 的注释里早已把这条道理写在
闭包边界的裁定上——「若把开发中的求解器代码计入闭包，每改一行都要重新走冻结审批，
冻结机制会因为过度敏感而被绕过——那才是真正的失效」。字节哈希在这里犯的是同一个错。

→ `.slx` 成员改取**规范化字节**（`sim/hashing.py` 的 `_canonical_slx_bytes()`）：
按条目名排序遍历 OPC 容器，排除文件元数据与编辑器界面状态条目，对余下条目把 UUID 与
ISO8601 时间戳替换为固定占位符，条目名与内容一并计入。排除清单按"是不是被仿真的
对象的一部分"划线，不是按"能不能让哈希稳定"划线：

| 排除 | 理由 |
| --- | --- |
| `metadata/*` | 创建/修改时间、作者、文档 UUID、Simulink 版本、缩略图 PNG。缩略图是从模型**渲染**出来的产物——模型真变了 `systems/*.xml` 必然先变，而 PNG 编码器输出不保证跨版本确定。Simulink 版本属于执行环境，已由 `execution_env_hash` 的 `matlab_release` 覆盖 |
| `simulink/ScheduleCore.xml` `ScheduleEditor.xml` | Schedule Editor 状态。本项目不使用该编辑器，内容是一张纯 UUID 引用图 |
| `simulink/windowsInfo.xml` | 模型窗口位置与大小，纯界面状态 |

**新增的未知条目默认计入**（是排除清单而非白名单）。这个方向是有意选的：Simulink
版本升级引入新条目时，行为是"可能误报"而不是"可能漏报"——误报会立刻被人发现并来查
这份清单，漏报不会。

模型定义落在 `simulink/systems/*.xml`（块、参数、连线）、`blockdiagram.xml`、
`configSet0.xml`（求解器）、`graphicalInterface.xml` 等条目里，这些在重复生成时
**本来就是确定的**——非确定性完全集中在被排除的那几个。

`.slx` 不是有效 OPC 包时抛 `SlxNotAnOpcPackageError`，不静默回退到原始字节哈希：
回退会让这个问题悄悄回来，而它的表现是门禁看起来在工作、实际已经失去区分能力。

**15. `runs.elapsed_ms` 从未入库（与后端无关的既有缺口）。**
`store.close_run_ok()` 只写 `waveform_ref` / `observable_ref` / `ended_at` 三列，
尽管它接收的是整个 `SimulationResult`，而其中 `elapsed_ms` 一直有值（MATLAB 侧
`simulate_once.m` 用 tic/toc 测，`PythonSession` 用 `time.perf_counter()` 测）。
实测三次真实运行 24/24、45/45、34/34 行全部为 `NULL`——两个后端都如此，说明它一直
这样。`run_task.py` 的注释里写着「它落的是仿真产物引用**与耗时**」，所以这是实现
漏了一列，不是设计选择。

这一列不是装饰性的过程量，它是"仿真有多贵"的唯一事实记录，缺了它有三处后果：
`preflight` 的 `check_budget_feasibility()` 拿不到实测的 `probe_single_run_s`
（`model.yaml` 里 `max_wallclock_per_run_s: 180` 那句「无 P-1 探针数据，按保守估计
取 3 分钟」就是这个缺口的直接产物）；README 主张的分层执行成本梯度无法从库里复算；
`find_cached_simulation()` 读回时的 `or 0` 兜底把 `NULL` 变成 0，让缺失看起来像
"这次仿真不花时间"。

**它还直接导致了本文档里几处偏悲观的耗时估算**（见第二节）：库里查不到单次耗时，
只能靠外部计时，而外部计时每次都在新 engine 会话里做，于是反复测到含首次编译的
29~40 s，把它当成了稳态成本。
→ `close_run_ok()` 补写该列；`tests/integration/test_run_task_smoke.py` 加断言
「非缓存命中的 done 行不得为 NULL，且至少一次为正」。

**16. Checkpoint 1 漏打印一条硬约束（与后端无关的既有缺口）。**
`_checkpoint1_summary()` 手写了一份约束名清单，只有四项——`gain_margin_min` 在它
作为第五条硬约束被加进 `constraints.yaml` 与 `config/schema.py` 时没有同步加进去。
后果是人在 Checkpoint 1 确认"我要搜的是这个任务"时看到的恰好是**修复前**的约束集，
而那条缺失的正是参考扫描暴露"约束集缺了一半稳定性判据"之后补上的那条。

判定链路不受影响（`eval/constraints.py` 直接从 `ConstraintsConfig` 读，不经过这里；
实测 12 个评价场景的 `gain_margin` 全部算出并判定，range 9.186~23.73 dB），所以这个
缺陷不会让任何结论出错——它只让人工确认环节看到的信息不完整，而那恰恰是最难靠其他
测试发现的一类问题。
→ 改为遍历 `HardConstraints` 的 pydantic 字段而不是手写清单，增删约束会自动反映；
测试断言的是"与 schema 字段集合**相等**"而不是"包含这五个名字"，后者在新增第六条
约束时仍会漏。

顺带给 `run_linear_analysis.m` 的返回值加了 `reason` 字段：它对"引擎没坏但没算出裕量"
的处理是 `status='ok'` + 空路径，此前不留任何痕迹，缺口 8 与 13 都只能靠在 MATLAB 里
逐步手工复现才定位到。Python 后端的 `PythonSession._run_linear_analysis()` 早已返回
同名字段，这是对齐而非新发明。

## 五、实测一致性

两个后端读同一份电路参数、解同一组方程，因此偏差应当只来自数值方法。

**裕量（两条完全独立的实现路径）**：MATLAB 的 `linearize`+`allmargin`（数值线性化）
vs Python 的 `open_loop_state_space`（手写状态空间 + 解析纯延迟），
`models/buck4ph_averaged` @ vin=10.8 V, T=85 °C：

| 候选 | 量 | MATLAB | Python | 差 |
| --- | --- | --- | --- | --- |
| baseline | PM | 83.46298693131308° | 83.463° | 0.0000° |
| baseline | GM | 16.697540773154675 dB | 16.698 dB | 0.0005 dB |
| grid_best | PM | 73.02229488633459° | 73.022° | 0.0003° |
| grid_best | GM | 9.185996646543819 dB | 9.186 dB | 0.0000 dB |

**时域，平均模型**（筛选场景 `scr_nom`，工程基线）：

| metric | Python | MATLAB | rel |
| --- | --- | --- | --- |
| settling_time | 46.8 µs | 46.7 µs | 0.21% |
| undershoot | 0.0309619 V | 0.0309549 V | 0.02% |
| phase_peak_current | 38.7994 A | 38.7988 A | 0.00% |
| obs.vout_min | 0.769038 V | 0.769045 V | 0.00% |

**时域，开关模型**（评价场景 `eval_vin_min_step_max`，工程基线）：

| metric | Python | MATLAB | rel |
| --- | --- | --- | --- |
| settling_time | 51.2 µs | 51.2 µs | 0.00% |
| phase_peak_current | 44.6719 A | 44.6702 A | 0.00% |
| obs.vout_min | 0.760766 V | 0.760968 V | 0.03% |
| undershoot | 0.0392342 V | 0.039032 V | 0.52% |
| overshoot | 0.00101956 V | 0.00108183 V | 6.11% |
| output_ripple | 0.00119866 V | 0.0012931 V | 7.88% |

稳态纹波峰峰值本身两侧只差 0.12%（13.7615 vs 13.7456 mV），相电流三角波峰峰差 1.42%
（11.9191 vs 12.0883 A）——电路一致。后两个指标偏差大是因为它们是 mV 级量，叠在峰峰
13.7 mV 的开关纹波上，而输出按 200 ns 抽取、纹波周期 500 ns，每周期只有 2.5 个采样点，
读到的峰值取决于采样落在三角波的哪个相位。

`tests/matlab/test_matlab_backend.py` 因此分两档容差：瞬态与峰值类 2%、纹波尺度类
15%。分档依据是误差机制不同，不是为了让测试通过。

## 六、测试与门禁

新增 `tests/matlab/test_matlab_backend.py`（8 项，`@pytest.mark.matlab`）：I/O 契约、
白名单拒绝、addpath 生效、波形往返、开关模型的纹波与相位交错、裕量与 Python 参考值
一致、`.slx` 重复生成得到同一个 `model_package_hash`、双后端一致性。

新增 `tests/unit/test_slx_hashing.py`（21 项，不需要 MATLAB）守护 `.slx` 规范化哈希的
两条性质：对文件元数据/UUID/时间戳/条目顺序/缩略图的改动**不**敏感，对块参数、块名、
连线端口、求解器类型、条目增删改名**必须**敏感。测试用 `zipfile` 构造条目结构与真实
产物一致的 OPC 包——这两条性质必须在没有 MATLAB 的环境里被守护，否则 CI 看不住它；
"真实 Simulink 重复保存确实会产生不同字节"这个前提则由 `tests/matlab/` 那一层验证。

`pyproject.toml` 的 `addopts` 把 `matlab` 与 `live_llm` 一并排除在默认路径外
（`-m "not live_llm and not matlab"`）。默认路径 **220 passed, 10 deselected,
46.6 s**，只依赖本仓库；跑 MATLAB 层要显式 `pytest -m matlab`（**8 passed, 71.8 s**，
含许可证签出、两次开关模型仿真与两次模型生成——昂贵的仿真由 module 级 fixture 跑一次
后多个测试共用）。
fixture 层另做逐项环境检查并在不满足时 skip，skip 原因具体到"哪个 slx_entry 没登记"。

两份门禁记录已重跑：`artifacts/dual_model_consistency.json` 内嵌的
`model_package_hash` 随闭包与哈希算法更新（改了 `matlab/+pa/` 下任一文件、或 `.slx`
里任一模型定义都会使它失效，但**重新生成同一个模型不会**）；
`artifacts/margin_cross_check.json` 的 sha256 未变（纯解析法，不依赖 MATLAB 侧文件），
`metrics.yaml` 无需改动。

## 七、剩余工作与已知限制

- **参考扫描仍在 Python 后端，但阻力比原先估计的小得多。** 144 点 × 评价场景数
  ≈ 288 次仿真。按稳态成本 0.5~1 s/次算，纯仿真时间是几分钟量级，不是最初估的
  2.5 小时——那个估算错在把首次编译的 29 s 当成了每次的成本。真正的顾虑只剩
  "整个过程要独占一个 Simulink 许可证席位"。搬过去是可行的，只是还没做。
- **`execution_mode` 的 `fast_restart` / `parsim` 仍无 MATLAB 侧实现**
  （`matlab/+pa/simulate_batch.m` 不存在）。**`fast_restart` 的收益比原先判断的
  小**：Simulink 自己的编译缓存已经把首次编译的开销吃掉了（同一会话内第 2 次调用
  就降到 3.2 s，第 4 次起 0.5~1 s），而 `fast_restart` 主要省的正是这部分。剩下的
  收益是省掉每次 `sim()` 的模型初始化，量级未测。倾向把 schema 枚举收窄到 `serial`
  一个值——留着两个无实现的取值比没有这个字段更糟，而现在也没有数据支持去补实现。
- **`freq_response_estimator_on_switching` 分支未验证。** 当前
  `primary_method = linear_analysis_on_averaged`，走不到它。该分支里 `frestimate` 的
  频率范围 `logspace(1, 6, 200)` 仍是占位值。
- **`runtime.max_wallclock_per_run_s = 180` 现在有实测数据可以收紧了。** 单次开关
  模型仿真首次 29 s、稳态 0.5~1 s。按注释里的 `ceil(3 × single_run_s)` 口径，取值
  应当覆盖首次编译（否则第一个候选就会被判超时），即 90 s 上下；但这条上限的语义是
  "超时即判卡死而非慢"，把它压到刚好覆盖首次编译会让任何一次异常抖动都变成误判。
  `close_run_ok()` 修好之后 `runs.elapsed_ms` 已经在落库，攒够几次真实运行的分布
  再定这个数比现在拍一个更好。
- **开关模型有一处已登记的初始相位差异。** Python 用 `frac < frac_prev` 检测载波
  回绕，第一步所有相都判为"非新周期"；Simulink 侧相 0 在 t=0 即开通。差异是一个开关
  周期内的初始相位，而阶跃触发前有约 50 个开关周期供初始瞬态衰减
  （`switching.py` 的模块 docstring 记录残余影响低于 0.5%）。
- **README 的两处陈述已过期**：「共享网络许可证且并发常满，主链路走 Python 仿真
  后端」与「MATLAB 交叉验证由平均/开关双模型互核这条替代路径承担」。前者与实测不符，
  后者现在有了更强的替代——真正的双后端交叉验证。
