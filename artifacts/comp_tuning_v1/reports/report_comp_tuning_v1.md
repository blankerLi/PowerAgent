# 寻优报告 · `comp_tuning_v1`

## 一、结论边界声明

本报告的结论限定于冻结的仿真参考模型。所载数值全部来自该模型在冻结场景集上的运行记录，用于回答「在这个假设电路上，搜索方法是否有效」，不构成器件选型或制造依据。

本任务未输出工程推荐，原因为三项：校准参数置信区间为空；Robustness 第三维未执行；结论限定于冻结仿真参考模型。

- 任务类型：`optimize`
- 停止原因：`target_reached`


在冻结场景集的评价层上，存在满足全部硬约束且跑完整个评价集的候选。其中主目标
（`settling_time`）取值最优的一个为 `417a617a5718f754`，其参数为
`ccomp` = 1e-10 · `rcomp` = 2.848e+04。

该候选在评价集上的 worst-case 主目标取值为 5.2，任务写下的达标线为 10。

采纳与否是 Checkpoint 3 的人工决定，本报告只提供依据，不给出采纳结论。

## 二、任务与配置哈希

| 项 | 值 |
| --- | --- |
| `task_id` | `comp_tuning_v1` |
| 轨道 | 仿真轨（`simulation_only = true`） |
| `task_kind` | `optimize` |
| `model_package_hash` | `f0b1f40f335356bb72ccebd4a972d90b5be84baac3051a75d731eff8d94ec78f` |
| `metrics_hash` | `126caea466c9a2aef14a551a22215264f2757bdf9efcc87345be89a0da246532` |
| `constraints_hash` | `b781b2ccdb90345fe08ea3fee09f0eabc1e7ebfd69f9e8d8b4d6b5f49511ab34` |
| `scenario_set_hash` | `776797c3955248236c76583f1925025ea09c7d307ad5a0438bfde438600247d9` |
| `execution_env_hash` | `754b0d8858c25cdcc987dbddda3c25ea5bfafb9ea53a2c44472ad7f6282255b6` |
| `calibration_hash` | `不可用` |
| 预算上限 | 200 次引擎启动 |
| 起始时间 | 2026-09-09T09:51:38Z |
| 结束时间 | 2026-09-09T09:59:23Z |


### 冻结场景集

| `scenario_id` | 层 | 模型变体 | 采集裕量 | `spec_version` |
| --- | --- | --- | --- | --- |
| `eval_vin_min_step_max` | evaluation | switching | 是 | `1` |
| `scr_nom` | screening | averaged | 否 | `1` |

## 三、Baseline 前值基线

不可用：本任务的库中没有可匹配的前值基线记录。`baselines.gate_key` 是四个
配置哈希的组合值，由门禁流程在冻结时算出并写入；渲染层不重算该组合值，也不用某条
恰好存在的基线充数。

## 四、Top 1/2/3

主目标：`settling_time`（方向 `minimize`），
取值为评价集上的 worst-case。名次取自 `eval.aggregate` 的三段定序。

### 第 1 名

- `candidate_id`：`417a617a5718f754`
- 参数：`ccomp` = 1e-10 · `rcomp` = 2.848e+04
- worst-case `settling_time`：5.2
- 次目标：`phase_margin` = 73.02

逐场景指标：

| `scenario_id` | `metric_id` | 取值 | 有效 | 无效原因 |
| --- | --- | --- | --- | --- |
| `eval_vin_min_step_max` | `gain_margin` | 9.186 | 是 | — |
| `eval_vin_min_step_max` | `obs.vout_max` | 0.8024 | 是 | — |
| `eval_vin_min_step_max` | `obs.vout_min` | 0.7843 | 是 | — |
| `eval_vin_min_step_max` | `output_ripple` | 0.00138 | 是 | — |
| `eval_vin_min_step_max` | `overshoot` | 0.00238 | 是 | — |
| `eval_vin_min_step_max` | `phase_margin` | 73.02 | 是 | — |
| `eval_vin_min_step_max` | `phase_peak_current` | 46.9 | 是 | — |
| `eval_vin_min_step_max` | `settling_time` | 5.2 | 是 | — |
| `eval_vin_min_step_max` | `undershoot` | 0.01569 | 是 | — |

### 第 2 名

- `candidate_id`：`534a080c8e465d60`
- 参数：`ccomp` = 1.52e-10 · `rcomp` = 2.848e+04
- worst-case `settling_time`：6.8
- 次目标：`phase_margin` = 79.63

逐场景指标：

| `scenario_id` | `metric_id` | 取值 | 有效 | 无效原因 |
| --- | --- | --- | --- | --- |
| `eval_vin_min_step_max` | `gain_margin` | 9.188 | 是 | — |
| `eval_vin_min_step_max` | `obs.vout_max` | 0.8015 | 是 | — |
| `eval_vin_min_step_max` | `obs.vout_min` | 0.7836 | 是 | — |
| `eval_vin_min_step_max` | `output_ripple` | 0.001282 | 是 | — |
| `eval_vin_min_step_max` | `overshoot` | 0.001511 | 是 | — |
| `eval_vin_min_step_max` | `phase_margin` | 79.63 | 是 | — |
| `eval_vin_min_step_max` | `phase_peak_current` | 46.01 | 是 | — |
| `eval_vin_min_step_max` | `settling_time` | 6.8 | 是 | — |
| `eval_vin_min_step_max` | `undershoot` | 0.01639 | 是 | — |

### 第 3 名

- `candidate_id`：`e72e690368257305`
- 参数：`ccomp` = 2.31e-10 · `rcomp` = 2.848e+04
- worst-case `settling_time`：9.6
- 次目标：`phase_margin` = 86.03

逐场景指标：

| `scenario_id` | `metric_id` | 取值 | 有效 | 无效原因 |
| --- | --- | --- | --- | --- |
| `eval_vin_min_step_max` | `gain_margin` | 9.189 | 是 | — |
| `eval_vin_min_step_max` | `obs.vout_max` | 0.8012 | 是 | — |
| `eval_vin_min_step_max` | `obs.vout_min` | 0.784 | 是 | — |
| `eval_vin_min_step_max` | `output_ripple` | 0.001063 | 是 | — |
| `eval_vin_min_step_max` | `overshoot` | 0.00114 | 是 | — |
| `eval_vin_min_step_max` | `phase_margin` | 86.03 | 是 | — |
| `eval_vin_min_step_max` | `phase_peak_current` | 45.72 | 是 | — |
| `eval_vin_min_step_max` | `settling_time` | 9.6 | 是 | — |
| `eval_vin_min_step_max` | `undershoot` | 0.01604 | 是 | — |


## 五、硬约束逐条结果

| 约束 | 阈值 | 单位 | 方向 | 观测量 | 适用层 | 阈值来源 |
| --- | --- | --- | --- | --- | --- | --- |
| `vout_min` | 0.75 | V | lower | `obs.vout_min` | screening, evaluation | — |
| `vout_max` | 0.85 | V | upper | `obs.vout_max` | screening, evaluation | — |
| `peak_current_max` | 55 | A | upper | `phase_peak_current` | screening, evaluation | approval_record |
| `phase_margin_min` | 45 | deg | lower | `phase_margin` | evaluation | approval_record |
| `gain_margin_min` | 6 | dB | lower | `gain_margin` | evaluation | approval_record |

各 Top 候选的逐场景判定：

| 名次 | `scenario_id` | 可行 | 违反项 |
| --- | --- | --- | --- |
| 1 | `eval_vin_min_step_max` | 是 | — |
| 2 | `eval_vin_min_step_max` | 是 | — |
| 3 | `eval_vin_min_step_max` | 是 | — |

## 六、寻优前后对比

前值基线不可用（见第三节），因此本节只列寻优结果一侧，不构造差值——缺了基线还给出
「改进了多少」，那个数字没有被减去的对象。

| 项 | 寻优结果 |
| --- | --- |
| `candidate_id` | `417a617a5718f754` |
| worst-case `settling_time` | 5.2 |
| 达标线 | 10 |

## 七、关键波形图

### 最佳候选 `417a617a5718f754` 在评价层的时域波形

![最佳候选 `417a617a5718f754` 在评价层的时域波形](../plots/waveform_run_12af3127811f423baba3d3a00c30580a.png)

### 参考扫描响应面

不可用：LookupError: plot_response_surface: 库中没有与 task_id='comp_tuning_v1' 四个哈希全同的 dense grid 任务


## 八、过程埋点摘要

| 项 | 值 |
| --- | --- |
| 起始时间 | 2026-09-09T09:51:38Z |
| 结束时间 | 2026-09-09T09:59:23Z |
| 引擎启动 | 43 / 200 |
| `runs` 行数 | 共 29 · 完成 29 · 失败 0 · 缓存命中 0 |
| LLM 调用 | 3 次 · 5.6e+04 token · 结果分布 {'ok': 3} |
| 人工介入 | `cp1` = 0 · `cp2` = 0 · `cp3` = 0 · `other` = 0 |
| 被拒候选 | 0 |

以下三项按 R23 AC7 记为 `runs` 表的派生日志字段，不作为本项目的结论指标；它们不出现
在第一节的渲染上下文中。

| 日志字段 | 值 |
| --- | --- |
| 首次可行解轮次 | 0 |
| 可行候选率 | 0.9333 |
| 重复率 | 0 |


## 九、推荐理由

## 十、限制说明
