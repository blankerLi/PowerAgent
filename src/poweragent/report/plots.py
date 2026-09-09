"""报告用图。只画库里已有的数据。

出图失败不是渲染失败：一张图缺了，报告里那一格显示"不可用"加原因，其余照常渲染
（R18 AC7/AC8）。整节凭空消失读报告的人不会注意到，一行"不可用"会。因此本模块的
函数在拿不到数据时返回带原因的标记，而不是抛异常打断整份报告。

`matplotlib` 用 Agg 后端：报告渲染可能发生在没有显示环境的机器上（CI、远程会话），
默认后端会尝试连接窗口系统并失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from poweragent.config.schema import ConstraintsConfig, MetricsConfig  # noqa: E402
from poweragent.eval.metrics import _load_waveform  # noqa: E402
from poweragent.store.repo import Store  # noqa: E402

__all__ = ["plot_waveform", "plot_response_surface", "collect_plots"]

# 波形图要画的信号，按 `models/*.yaml` 的 `io_contract` 命名。取输出电压与相电流
# 两条：前者是全部时域指标的来源，后者承载 `peak_current_max` 的判定。
_VOUT_SIGNAL = "Vout"
_IPHASE_SIGNAL = "Iphase"


def plot_waveform(run_id: str, store: Store, out: Path) -> Path:
    """画一次运行的输出电压与相电流（design.md §6.10）。

    阶跃时刻以竖线标出：时域指标的窗口全部以它为原点，没有这条线就看不出恢复时间
    是从哪里开始算的。
    """
    row = store.connection.execute(
        "SELECT waveform_ref FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None or not row[0]:
        raise FileNotFoundError(f"plot_waveform: run_id={run_id!r} 没有波形产物引用")

    waveform = _load_waveform(row[0])
    out.parent.mkdir(parents=True, exist_ok=True)

    panels = [name for name in (_VOUT_SIGNAL, _IPHASE_SIGNAL) if name in waveform.signals]
    if not panels:
        raise KeyError(
            f"plot_waveform: run_id={run_id!r} 的波形不含 "
            f"{_VOUT_SIGNAL!r} 或 {_IPHASE_SIGNAL!r}"
        )

    fig, axes = plt.subplots(len(panels), 1, figsize=(8, 3 * len(panels)), sharex=True)
    if len(panels) == 1:
        axes = [axes]

    for axis, name in zip(axes, panels):
        time_s, data = waveform.signals[name]
        time_us = time_s * 1e6
        if data.ndim == 1:
            axis.plot(time_us, data, linewidth=1.0)
        else:
            # 相电流是每相一列。逐相画出来而不是先求和或取最大：相间不均流本身
            # 就是要看的现象，聚合掉之后看不出来。
            for phase in range(data.shape[1]):
                axis.plot(time_us, data[:, phase], linewidth=0.8, label=f"phase {phase}")
            axis.legend(fontsize="x-small", ncol=data.shape[1])
        axis.set_ylabel(name)
        axis.grid(True, alpha=0.3)
        if waveform.step_trigger_s is not None:
            axis.axvline(
                waveform.step_trigger_s * 1e6,
                color="red",
                linestyle="--",
                linewidth=0.8,
            )

    axes[-1].set_xlabel("time (us)")
    axes[0].set_title(f"run {run_id}" + ("  (dashed = load step)" if waveform.step_trigger_s is not None else ""))
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def _find_matching_dense_grid_task(store: Store, task_id: str) -> str | None:
    """找与本任务四个哈希全同的 dense grid 任务（R18 AC8）。

    四个哈希必须全同才能并列展示：响应面是"在这套模型、这套指标、这套约束、这套
    场景集下，设计域长什么样"。任一哈希不同，图上的点与本任务的候选就不是同一个
    口径下的量，放在一起会诱导出错误的比较。

    找不到返回 `None`，由调用方标为不可用——不退化到"随便找一个 dense grid 任务"。
    """
    row = store.connection.execute(
        "SELECT model_package_hash, metrics_hash, constraints_hash, scenario_set_hash "
        "FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    match = store.connection.execute(
        "SELECT task_id FROM tasks "
        "WHERE task_kind IN ('dense_grid', 'tiered_grid') "
        "  AND model_package_hash=? AND metrics_hash=? "
        "  AND constraints_hash=? AND scenario_set_hash=? "
        "ORDER BY started_at DESC LIMIT 1",
        row,
    ).fetchone()
    return match[0] if match else None


def plot_response_surface(
    task_id: str,
    store: Store,
    out: Path,
    *,
    metrics_cfg: MetricsConfig | None = None,
    constraints_cfg: ConstraintsConfig | None = None,
) -> Path:
    """响应面图（design.md §6.10）。

    复用 `reference.dense_grid.response_surface()` 而不是另画一套：那个函数已经处理
    了无效点的遮罩（不插值、不填默认值）与对数等距的坐标轴。报告再实现一遍会得到
    两份可能不一致的"同一张图"。

    位置参数保持 design.md 的 `(task_id, store, out)` 三参形式。两个配置是可选
    kwonly：`response_surface()` 需要它们才能知道主目标是哪个指标、档位是哪些。
    给出时直接用；不给出时才回退到从 `configs/` 载入。

    回退路径依赖当前工作目录，因此调用方**应当**把已有的配置传进来。`collect_plots()`
    就是这么做的——它手上本来就有这两份配置，让本函数再去磁盘上找一遍，等于让渲染
    结果取决于进程在哪个目录里启动。
    """
    from poweragent.reference.dense_grid import response_surface

    if metrics_cfg is None or constraints_cfg is None:
        from poweragent.config.loader import load_all

        from poweragent.report.render import DEFAULT_CONFIG_DIR

        bundle = load_all(DEFAULT_CONFIG_DIR)
        metrics_cfg = metrics_cfg or bundle.metrics
        constraints_cfg = constraints_cfg or bundle.constraints

    grid_task_id = _find_matching_dense_grid_task(store, task_id)
    if grid_task_id is None:
        raise LookupError(
            f"plot_response_surface: 库中没有与 task_id={task_id!r} 四个哈希全同的 "
            f"dense grid 任务"
        )

    paths = response_surface(
        grid_task_id,
        store,
        out,
        metrics_cfg=metrics_cfg,
        constraints_cfg=constraints_cfg,
    )
    return paths[0]


def _pick_representative_run(
    store: Store, task_id: str, candidate_id: str | None
) -> str | None:
    """选出要画波形的那次运行：**给定候选**在评价层上的成功运行。

    `candidate_id` 必须由调用方给出（报告的 Top 1），不能省略。早先的实现只按
    `ORDER BY started_at LIMIT 1` 取"最早的可行评价层运行"，于是图画的是某个碰巧
    最早跑完的候选，而报告第四节列的是最优候选的指标——两者对不上，读者却会把图
    当成最优解的波形。这类错位不会报错：图能出、数也能列，只是它们说的不是同一个
    设计。

    取评价层而不是筛选层：结论建立在评价集上，波形也应当是结论所依据的那次运行，
    而不是筛选阶段用平均模型跑的近似。
    """
    if candidate_id is None:
        return None
    row = store.connection.execute(
        "SELECT r.run_id FROM runs r "
        "JOIN scenario_set s ON s.task_id=r.task_id AND s.scenario_id=r.scenario_id "
        "JOIN constraint_results cr ON cr.run_id=r.run_id "
        "WHERE r.task_id=? AND r.candidate_id=? AND s.tier='evaluation' "
        "      AND r.status='done' AND cr.feasible=1 AND r.waveform_ref IS NOT NULL "
        "ORDER BY r.scenario_id LIMIT 1",
        (task_id, candidate_id),
    ).fetchone()
    return row[0] if row else None


def collect_plots(
    task_id: str,
    store: Store,
    out_dir: Path,
    *,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
    relative_to: Path,
    best_candidate_id: str | None,
) -> list[dict[str, Any]]:
    """产出报告第七节需要的图，返回每张图的标题、相对路径与可用性。

    每一项都带 `available` 与失败时的 `reason`：一张图出不来的原因（没有波形产物、
    库里没有匹配的参考扫描）本身就是读报告的人需要知道的事实，吞掉它等于让报告
    对自己的缺失保持沉默。

    路径以 `relative_to` 为基准写成相对路径，报告连同 `artifacts/<task_id>/` 一起
    被复制到别处时图仍能显示。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    plots: list[dict[str, Any]] = []

    title = f"最佳候选 `{best_candidate_id}` 在评价层的时域波形"
    run_id = _pick_representative_run(store, task_id, best_candidate_id)
    if run_id is None:
        plots.append(
            {
                "title": "最佳候选在评价层的时域波形",
                "available": False,
                "reason": (
                    "没有可行候选"
                    if best_candidate_id is None
                    else f"候选 {best_candidate_id} 没有留下波形产物的可行评价层运行"
                ),
            }
        )
    else:
        target = out_dir / f"waveform_{run_id}.png"
        try:
            plot_waveform(run_id, store, target)
        except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
            plots.append(
                {
                    "title": title,
                    "available": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            plots.append(
                {
                    "title": title,
                    "available": True,
                    "path": _relative(target, relative_to),
                }
            )

    surface_dir = out_dir / "response_surface"
    try:
        surface = plot_response_surface(
            task_id,
            store,
            surface_dir,
            metrics_cfg=metrics_cfg,
            constraints_cfg=constraints_cfg,
        )
    except (LookupError, OSError, ValueError) as exc:
        plots.append(
            {
                "title": "参考扫描响应面",
                "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )
    else:
        plots.append(
            {
                "title": "参考扫描响应面",
                "available": True,
                "path": _relative(surface, relative_to),
            }
        )

    return plots


def _relative(path: Path, base: Path) -> str:
    """相对路径，且统一用 `/` 分隔。

    Markdown 的图片引用在 Windows 的 `\\` 分隔下不显示，而这份报告要能在任何平台上
    被读。`os.path.relpath` 而非 `Path.relative_to`：后者要求 `path` 在 `base` 之下，
    而图目录是报告目录的兄弟。
    """
    import os

    return os.path.relpath(path, base).replace("\\", "/")
