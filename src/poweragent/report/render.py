"""Jinja2 渲染报告。无 LLM 参与。

`render_report()` 的签名由 `cli.py` 的调用点锁定（`render_report(task_id, *, store,
out=None) -> Path`），因此"配置从哪来"这件事必须在本模块内解决——`build_context()`
需要三份配置（单位与阈值来源在 `metrics.yaml` / `constraints.yaml` 里，`tasks` 表
只存它们的哈希），而签名里没有配置参数。

分层是这样的：`render_report()` 是 CLI 边界，承担"默认从 `configs/` 读"这个约定；
`build_context()` 是纯函数，配置由调用方显式传入。测试因此可以给 `build_context()`
注入任意配置，而不必依赖进程的当前工作目录。

渲染前后 `llm_calls` 的行数不变（R18 AC1）：本模块不导入 `agent` 包的任何东西，也
不发起网络请求。这条性质靠"没有那条代码路径"保证，不靠运行期计数比对。

严格未定义
----------
Jinja2 环境用 `StrictUndefined`。模板里写错一个键，默认行为是渲染成空字符串——那会
产出一份看起来完整、某一格却悄悄空掉的报告，而报告的用途恰恰是让人据此做判断。
宁可渲染失败。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from poweragent.config.loader import load_all
from poweragent.report.context import (
    UNAVAILABLE,
    ReportContext,
    build_context,
    format_value,
)
from poweragent.store.repo import Store

__all__ = [
    "TEMPLATE_DIR",
    "DEFAULT_CONFIG_DIR",
    "build_environment",
    "render_conclusion",
    "render_markdown",
    "render_report",
]

# 模板目录由 R18 AC4 点名为 `templates/report.md.j2`，即仓库根下的 `templates/`，
# 不是包内资源。本文件位于 `<repo>/src/poweragent/report/render.py`，故上溯三级。
TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates"

# 与 `cli.py` 的 `cmd_run` 同一约定：配置在仓库根的 `configs/` 下，无 CLI 选项承载
# 可配置路径（design.md 全篇示例均用相对路径）。
DEFAULT_CONFIG_DIR = Path("configs")

MAIN_TEMPLATE = "report.md.j2"
CONCLUSION_TEMPLATE = "conclusion.md.j2"


def build_environment(template_dir: Path | None = None) -> Environment:
    """构造渲染环境。

    `keep_trailing_newline` 保住文件末尾的换行（Markdown 文件缺末行换行会让某些
    差异工具把最后一行标成变更）；`trim_blocks` 与 `lstrip_blocks` 让模板里的控制
    标记不在输出里留下空行，否则表格会被空行截断成两张表。
    """
    env = Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR), encoding="utf-8"),
        undefined=StrictUndefined,
        autoescape=False,  # 输出是 Markdown，不是 HTML；转义会破坏反引号与表格
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["fmt"] = format_value
    env.filters["kvline"] = _kvline
    return env


def _kvline(mapping: dict | None, sep: str = " · ") -> str:
    """把映射渲染成 `` `键` = 值 `` 的一行，键按字典序。

    存在的理由是模板可读性与换行的相互作用：行内写 `{% for %}...{% endfor %}` 时，
    `trim_blocks` 会吃掉 `{% endfor %}` 之后的换行，于是下一行被接到同一行末尾，
    Markdown 表格与列表随之错位。把这类拼接搬进过滤器，模板里就没有行内块标签。

    值经 `format_value` 输出，与正文其余数值同一格式（R18 AC5 要求两侧共用同一
    格式化函数）。
    """
    if not mapping:
        return "—"
    return sep.join(f"`{key}` = {format_value(value)}" for key, value in sorted(mapping.items()))


def render_conclusion(context: ReportContext, env: Environment | None = None) -> str:
    """单独渲染结论边界声明，命名空间只含 `context.conclusion`。

    这是 R17 AC11 第 3 条的实现要点：过程量不进入结论段上下文，不能靠"模板里不要写"
    这种约定来保证——Jinja2 的作用域是继承的，`{% include %}` 同样继承父作用域，
    所以只要结论段是主模板的一部分，它就能取到 `telemetry` 里的任何键。

    把结论段渲染成一个独立字符串再传回主模板，过程量就根本不在那次渲染的命名空间里。
    模板写了 `{{ feasible_rate }}` 会因 `StrictUndefined` 直接报错，而不是静默渲染
    成空白。
    """
    environment = env or build_environment()
    template = environment.get_template(CONCLUSION_TEMPLATE)
    return template.render(**context.conclusion).strip()


def render_markdown(
    context: ReportContext,
    *,
    plots: list[dict] | None = None,
    env: Environment | None = None,
) -> str:
    """把上下文渲染成 Markdown 文本。

    `plots` 由调用方传入而不是在这里生成：出图要写文件，而"渲染成文本"与"往磁盘上
    放东西"是两件应当能分开做的事——测试要断言文本内容时不必先产出几张 PNG。
    """
    environment = env or build_environment()
    template = environment.get_template(MAIN_TEMPLATE)
    return template.render(
        task_id=context.task_id,
        conclusion_section=render_conclusion(context, environment),
        task=context.task,
        config_binding=context.config_binding,
        baseline=context.baseline,
        scenarios=context.scenarios,
        top_candidates=context.top_candidates,
        hard_constraints=context.hard_constraints,
        comparison=context.comparison,
        telemetry=context.telemetry,
        no_feasible=context.no_feasible,
        plots=plots or [],
        unavailable=UNAVAILABLE,
    )


def render_report(
    task_id: str,
    *,
    store: Store,
    out: Path | None = None,
    config_dir: Path | None = None,
) -> Path:
    """渲染报告并落盘，返回文件路径（design.md §6.10；R18 AC1）。

    默认输出 `artifacts/<task_id>/reports/report_<task_id>.md`。图输出到同级的
    `artifacts/<task_id>/plots/`（AC8），以相对路径嵌入报告——用相对路径而不是绝对
    路径，报告连同 `artifacts/<task_id>/` 一起复制到别处仍然能显示图。

    `config_dir` 是相对 design.md 字面签名的一个附加 kwonly 参数，默认 `configs/`。
    加它是为了让测试能指向 fixture 配置；`cli.py` 的调用点不传，行为与签名一致。
    """
    bundle = load_all(config_dir or DEFAULT_CONFIG_DIR)

    context = build_context(
        task_id,
        store,
        task_cfg=bundle.task,
        metrics_cfg=bundle.metrics,
        constraints_cfg=bundle.constraints,
    )

    task_artifacts = Path("artifacts") / task_id
    report_path = out or task_artifacts / "reports" / f"report_{task_id}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # 图目录由 `task_id` 决定（`artifacts/<task_id>/plots/`，AC8），不从
    # `report_path` 反推。反推在默认路径下恰好正确，但 `out` 指向别处时会把图写到
    # 那个位置的上一级——比如 `out=artifacts/preview.md` 会让图落在仓库根。
    # 图是任务的产物，位置该由任务决定；报告可以放任何地方，用相对路径指向图。
    plots_dir = task_artifacts / "plots"

    # 图落在报告的兄弟目录 `plots/` 下。即使一张也出不来也要走这一步：`plots` 列表
    # 里的每一项都带 `available`，出不来的那张在报告里显示为"不可用"加原因，而不是
    # 整节消失——一节凭空消失读报告的人不会注意到，一行"不可用"会。
    from poweragent.report.plots import collect_plots

    plots = collect_plots(
        task_id,
        store,
        plots_dir,
        metrics_cfg=bundle.metrics,
        constraints_cfg=bundle.constraints,
        relative_to=report_path.parent,
        # 图必须画报告第四节列出的那个候选。不传的话 plots 层只能猜，而猜错时图与
        # 数据说的是两个不同的设计，且不会有任何报错。
        best_candidate_id=context.conclusion.get("best_candidate_id"),
    )

    report_path.write_text(
        render_markdown(context, plots=plots), encoding="utf-8", newline="\n"
    )
    return report_path
