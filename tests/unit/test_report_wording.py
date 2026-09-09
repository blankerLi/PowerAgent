"""报告用语的负向约束（requirements.md R17 AC11 的三条断言）。

这些断言测的是"报告不会说什么"。正向断言（某个数值渲染对不对）由渲染测试与真实
渲染覆盖；这里只管越界的表述有没有渲染路径。

为什么词表落在测试里而不是配置里
--------------------------------
禁用词是断言数据，不是运行期配置。做成 `configs/*.yaml` 会给出一条"改配置绕过门禁"
的路径——而这三条约束的意义恰恰在于不能被绕过。design.md §6.10 对同类问题的取向是
"构造保证，非事后校验"：模板里不存在那些表述的渲染路径，而不是渲染完再过滤一遍。

扫描对象是模板文件与渲染产物，不是整个仓库。源码注释里出现"搜索效率"这个词是在
讨论为什么不用它，那与报告说了什么无关（`reference/dense_grid.py` 也记了同样的
边界）。
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from poweragent.report.context import PROCESS_METRIC_KEYS, UNAVAILABLE, ReportContext
from poweragent.report.render import (
    CONCLUSION_TEMPLATE,
    MAIN_TEMPLATE,
    TEMPLATE_DIR,
    build_environment,
    render_conclusion,
    render_markdown,
)

# --------------------------------------------------------------------------
# 词表。三组各有独立的来源条款，分开列而不是合成一个大集合——某一组失败时，
# 报错信息要能指出违反的是哪一条需求。
# --------------------------------------------------------------------------

# R23 AC6：M5 结果以描述性表述呈现，这 8 个词构成唯一封闭的禁用表。
STATISTICAL_TERMS = (
    "显著", "显著性", "非劣", "非劣性", "p 值", "假设检验", "统计功效", "统计上",
)

# R1 AC6：`simulation_only=true` 时模板不含断言实物可行性的条目或渲染分支。
# 这四类表述在两个分支里都不许出现——报告是否断言实物可行，不该取决于走了哪个分支。
PHYSICAL_CLAIM_TERMS = ("可打板", "实物可行", "实物预测准确", "打板建议")

# R17 AC11 第 2 条 / R23 AC5：项目不把搜索效率当指标，报告里不出现这类表述。
# 依据见 requirements.md 术语表对 Dense Grid 的界定（"不作为搜索效率 Oracle"）：
# 单次跑的效率数字没有统计意义，用它给设计结论背书等于用噪声支撑结论。
EFFICIENCY_TERMS = ("regret", "simulations-to-target", "搜索效率", "收敛速度")


_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _template_texts() -> dict[str, str]:
    """两个模板的可渲染文本，已剥去 Jinja2 注释。

    扫模板原文而不只扫渲染产物：一个禁用词可能藏在当前数据走不到的分支里（例如
    工程轨分支），只看产物永远发现不了。

    但注释要剥掉。`{# ... #}` 不进入任何渲染产物，而模板顶部的注释正是在说明"为什么
    这里不写实物可行性的措辞"——把这种说明本身判成违规，会逼着注释绕开它要讨论的
    那个词，注释也就说不清话了。禁的是报告对读者说什么，不是模板对维护者说什么。
    """
    return {
        name: _JINJA_COMMENT.sub("", (TEMPLATE_DIR / name).read_text(encoding="utf-8"))
        for name in (MAIN_TEMPLATE, CONCLUSION_TEMPLATE)
    }


def _minimal_context(*, has_feasible: bool = True) -> ReportContext:
    """手工构造一份上下文。

    不从数据库构造：这三条断言与"数据库里有什么"无关，它们只关心模板与上下文的
    结构。手工构造让测试不依赖任何 fixture 库，也让"结论段拿不到过程量"这件事
    可以被精确地摆出来——`telemetry` 里放了过程量，`conclusion` 里没放。
    """
    best = {
        "rank": 1,
        "available": True,
        "candidate_id": "cand_test_0001",
        "parameters": {"rcomp": 28480.3586844, "ccomp": 1e-10},
        "worst_case_value": 5.2,
        "objective_metric_id": "settling_time",
        "secondary_values": {"phase_margin": 73.02},
        "metrics": [
            {
                "scenario_id": "eval_vin_min_step_max",
                "metric_id": "settling_time",
                "value": 5.2,
                "valid": True,
                "invalid_reason": None,
            }
        ],
        "constraints": [
            {
                "scenario_id": "eval_vin_min_step_max",
                "feasible": True,
                "violations": [],
            }
        ],
    }
    return ReportContext(
        task_id="task_test",
        conclusion={
            "simulation_only": True,
            "task_kind": "optimize",
            "stop_reason": "target_reached",
            "in_progress": False,
            "cause": None,
            "has_feasible_candidate": has_feasible,
            "best_candidate_id": best["candidate_id"] if has_feasible else None,
            "best_parameters": best["parameters"] if has_feasible else {},
            "best_objective": best["worst_case_value"] if has_feasible else None,
            "objective_metric_id": "settling_time" if has_feasible else None,
            "objective_target": 10.0,
        },
        task={
            "simulation_only": True,
            "task_kind": "optimize",
            "model_package_hash": "h_model",
            "metrics_hash": "h_metrics",
            "constraints_hash": "h_constraints",
            "scenario_set_hash": "h_scenarios",
            "execution_env_hash": "h_env",
            "calibration_hash": UNAVAILABLE,
            "budget_max_starts": 200,
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:10:00Z",
            "stop_reason": "target_reached",
            "cause": None,
        },
        config_binding={
            "metrics_match": True,
            "constraints_match": True,
            "all_match": True,
            "current_metrics_hash": "h_metrics",
            "current_constraints_hash": "h_constraints",
        },
        baseline={"available": False},
        scenarios=[
            {
                "scenario_id": "eval_vin_min_step_max",
                "tier": "evaluation",
                "model_variant": "switching",
                "require_margin": True,
                "spec": {},
                "spec_version": "1",
            }
        ],
        top_candidates=(
            [best, {"rank": 2, "available": False}, {"rank": 3, "available": False}]
            if has_feasible
            else [{"rank": n, "available": False} for n in (1, 2, 3)]
        ),
        hard_constraints=[
            {
                "name": "phase_margin_min",
                "value": 45.0,
                "observable": "phase_margin",
                "sense": "lower",
                "applies_to_tier": ["evaluation"],
                "unit": "deg",
                "threshold_source": "approval_record",
            }
        ],
        comparison={
            "baseline": {"available": False},
            "best": best if has_feasible else None,
            "objective_metric_id": "settling_time",
            "objective_direction": "minimize",
            "objective_target": 10.0,
        },
        telemetry={
            "interventions": {"cp1": 0, "cp2": 0, "cp3": 0, "other": 0},
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:10:00Z",
            "llm": {"available": True, "calls": 3, "tokens": 56002, "by_outcome": {"ok": 3}},
            "runs": {"total": 29, "done": 29, "failed": 0, "cache_hits": 0},
            "budget_used": 43,
            "budget_max": 200,
            "budget_granted": 0,
            "rejection_counts": {},
            # 三个过程量。它们在这里是合规的——"过程埋点摘要"段就是为埋点存在的。
            "first_feasible_round": 0,
            "feasible_rate": 0.9333,
            "duplicate_rate": 0.0,
        },
        no_feasible=(
            None
            if has_feasible
            else {
                "legal_domain": [],
                "legal_domain_available": False,
                "hard_constraints": [],
                "scenario_set_hash": "h_scenarios",
                "scenarios": [],
                "rejection_counts": {"off_tick": 4},
                "budget_used": 43,
                "budget_max": 200,
            }
        ),
    )


@pytest.fixture(scope="module")
def rendered_feasible() -> str:
    return render_markdown(_minimal_context(has_feasible=True))


@pytest.fixture(scope="module")
def rendered_infeasible() -> str:
    """无可行候选的分支也要扫：它渲染的是另一段文本，可能藏着另一批措辞。"""
    return render_markdown(_minimal_context(has_feasible=False))


# --------------------------------------------------------------------------
# 断言 1：禁用词表扫描
# --------------------------------------------------------------------------


@pytest.mark.parametrize("term", STATISTICAL_TERMS)
def test_templates_and_output_avoid_statistical_terms(
    term: str, rendered_feasible: str, rendered_infeasible: str
) -> None:
    """统计推断类措辞不出现（R23 AC6）。

    项目没有做假设检验，也没有多次重复实验的样本；用「显著」一类词描述单次跑的
    结果会把一个描述性观察说成一个统计结论。
    """
    for name, text in _template_texts().items():
        assert term not in text, f"{name} 含禁用词 {term!r}"
    assert term not in rendered_feasible, f"渲染产物（有可行候选）含禁用词 {term!r}"
    assert term not in rendered_infeasible, f"渲染产物（无可行候选）含禁用词 {term!r}"


@pytest.mark.parametrize("term", PHYSICAL_CLAIM_TERMS)
def test_templates_have_no_physical_feasibility_claim(
    term: str, rendered_feasible: str, rendered_infeasible: str
) -> None:
    """实物可行性表述在模板中没有渲染路径（R1 AC6）。

    扫模板原文是这条断言的关键：`simulation_only=false` 的工程轨分支在当前配置下
    渲染不出来，只扫产物的话那个分支里写什么都不会被发现。
    """
    for name, text in _template_texts().items():
        assert term not in text, f"{name} 含实物可行性表述 {term!r}"
    assert term not in rendered_feasible
    assert term not in rendered_infeasible


@pytest.mark.parametrize("term", EFFICIENCY_TERMS)
def test_templates_avoid_search_efficiency_framing(
    term: str, rendered_feasible: str, rendered_infeasible: str
) -> None:
    """搜索效率类表述不出现（R17 AC11 第 2 条 / R23 AC5）。

    参考扫描的用途限于响应面可视化与解质量对照。把"用了多少次仿真"写成结论，需要
    多次重复跑取分布才有意义，而本项目没有那个设计。
    """
    lowered_templates = {
        name: text.lower() for name, text in _template_texts().items()
    }
    needle = term.lower()
    for name, text in lowered_templates.items():
        assert needle not in text, f"{name} 含效率类表述 {term!r}"
    assert needle not in rendered_feasible.lower()
    assert needle not in rendered_infeasible.lower()


# --------------------------------------------------------------------------
# 断言 3：过程量不进入结论段的渲染上下文
# --------------------------------------------------------------------------


def test_process_metrics_are_absent_from_conclusion_context() -> None:
    """结论段上下文的键集合与三个过程量不相交（R17 AC11 第 3 条）。

    这是那条约束最直接的形式。它成立的前提是结论段的上下文确实是一个独立映射——
    如果整个 `ReportContext` 被摊平交给模板，这条断言过了也没有意义。下一条测试
    验证那个前提。
    """
    context = _minimal_context()

    assert PROCESS_METRIC_KEYS.isdisjoint(context.conclusion.keys()), (
        f"结论段上下文含过程量键: "
        f"{PROCESS_METRIC_KEYS & set(context.conclusion.keys())}"
    )
    # 同时确认这些键确实存在于埋点段——它们不是被删掉了，而是被放到了该在的地方。
    assert PROCESS_METRIC_KEYS <= set(context.telemetry.keys())


def test_conclusion_template_cannot_reach_process_metrics() -> None:
    """结论段的渲染命名空间里根本没有过程量，写了就报错。

    这条测试保护的是上一条的前提。`render_conclusion()` 只把 `context.conclusion`
    展开给模板，配合 `StrictUndefined`，模板里写 `{{ feasible_rate }}` 会抛
    `UndefinedError` 而不是静默渲染成空字符串。

    用一个临时模板来验证，而不是去改真模板：要断言的是"那个命名空间里取不到"这个
    机制，不是"真模板恰好没写"。
    """
    from jinja2 import DictLoader, StrictUndefined, UndefinedError
    from jinja2 import Environment as JinjaEnvironment

    context = _minimal_context()
    probe = JinjaEnvironment(
        loader=DictLoader({CONCLUSION_TEMPLATE: "{{ feasible_rate }}"}),
        undefined=StrictUndefined,
    )
    probe.filters["fmt"] = build_environment().filters["fmt"]

    with pytest.raises(UndefinedError):
        render_conclusion(context, probe)


def test_process_metrics_do_appear_in_the_telemetry_section(
    rendered_feasible: str,
) -> None:
    """过程量出现在报告的埋点段里。

    禁的是让它们给结论背书，不是禁止记录。若这条断言失败，说明为了通过前两条把
    埋点整个删掉了——那是把约束读成了"不许记录"。
    """
    assert "首次可行解轮次" in rendered_feasible
    assert "可行候选率" in rendered_feasible
    assert "重复率" in rendered_feasible


# --------------------------------------------------------------------------
# 结构性要求：两个占位段渲染为空段（R18 AC4 末句）
# --------------------------------------------------------------------------


def test_placeholder_sections_render_empty(rendered_feasible: str) -> None:
    """`推荐理由` 与 `限制说明` 两段在 `render_report()` 的输出中为空段。

    它们是留给人写的。渲染层填进去的任何文字都会变成"报告自己给出的结论"，而
    R18 AC6 明确报告不渲染项目级结论语句。
    """
    lines = rendered_feasible.splitlines()
    for heading in ("推荐理由", "限制说明"):
        index = next(
            (i for i, line in enumerate(lines) if line.startswith("## ") and heading in line),
            None,
        )
        assert index is not None, f"缺少 `{heading}` 段"
        body = [line.strip() for line in lines[index + 1 :] if line.strip()]
        # 段落之后若还有内容，只能是下一个标题。
        assert not body or body[0].startswith("## "), (
            f"`{heading}` 段不为空，首个非空内容: {body[0]!r}"
        )


def test_conclusion_section_states_the_poc_boundary(rendered_feasible: str) -> None:
    """PoC 轨的结论边界声明与"未输出工程推荐"的三条原因都渲染出来（R1 AC6/AC11）。"""
    assert "结论限定于冻结的仿真参考模型" in rendered_feasible
    assert "本任务未输出工程推荐" in rendered_feasible
    for reason in ("校准参数置信区间为空", "Robustness 第三维未执行"):
        assert reason in rendered_feasible


def test_in_progress_task_is_rendered_not_refused() -> None:
    """`stop_reason` 为空时照样渲染，顶部标注非终态（R18 AC10）。

    AC10 明确"不为该情形提供拒绝渲染的分支"：任务跑一半时看报告是常见需求，
    拒绝渲染只会让人去直接读数据库。
    """
    context = _minimal_context()
    # `dataclasses.replace` 而不是 `ReportContext(**context.__dict__)`：
    # `ReportContext` 是 `slots=True` 的 frozen dataclass，没有 `__dict__`。
    in_progress = dataclasses.replace(
        context,
        conclusion={
            **context.conclusion,
            "in_progress": True,
            "stop_reason": UNAVAILABLE,
        },
    )
    text = render_markdown(in_progress)
    assert "任务进行中" in text and "非终态" in text
