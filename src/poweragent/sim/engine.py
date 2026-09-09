"""poweragent/sim/engine.py

MATLAB Engine 生命周期管理与调用白名单（design.md §6.3.1；`tasks.md` T5 / 任务 7.1；
需求追溯 R5.1, R5.2, R5.3；属性 CP-8）。

`sim` 是 PowerAgent 系统中唯一 import MATLAB Engine 的包；本模块是该包内唯一实际
`import matlab.engine` 的位置（design.md §2.1 / §14）。

延迟导入（lazy import）的理由
------------------------------
`import matlab.engine` 被放在 `__enter__()` 内部而不是模块顶层。MATLAB Engine for
Python 是否已安装取决于目标机器上的 MATLAB 安装与 `matlabengine` 包配置，在很多
开发与 CI 环境（尤其是无 MATLAB 许可证的环境，见 design.md §12「Schema/Unit 与两个
Metric Fixture 层不依赖 MATLAB，可在无许可证环境跑」）中该包并不存在。如果本模块在
模块顶层 `import matlab.engine`，那么任何 `import poweragent.sim.engine`（包括通过
`poweragent.sim` 包的 `__init__.py` 或其他模块的传递 import）都会在没有安装 MATLAB
Engine 的机器上直接 `ImportError`，即使调用方从未真正需要启动 MATLAB（例如只是运行
Schema/Unit 层测试、或者只是在无 MATLAB 环境里做静态导入边界检查）。把 `import
matlab.engine` 推迟到 `MatlabSession.__enter__()` 内部，使"能否 import 这个模块"与
"能否启动一个真实的 MATLAB Engine 会话"两件事解耦：前者在任何环境都成立，后者只在
真正调用 `with MatlabSession():` 时才被要求成立。

`call()` 的白名单校验与调用机制
--------------------------------
`ALLOWED_MATLAB_FUNCTIONS` 与 `call()` 的函数名比较使用 Python 的 `in`（集合成员
判定），这本身就是逐字符完全相等比较：不做大小写归一化、不做前后空白裁剪、不做前缀
或子串匹配。任何不满足逐字符相等的输入（大小写不同、含前后空白、附带参数的语句
字符串等）一律落入"不在白名单内"的分支，在触碰 `self._engine` 之前即被拒绝。

白名单命中后，实际调用走 `self._engine.feval(fn, *args, nargout=nargout)`，而不是
`getattr(self._engine, fn)(...)` 之类的属性访问式调用。原因是 MATLAB Engine for
Python 的属性访问调用方式对 MATLAB 的 `+package` 命名空间（本系统里的 `+pa` 包）支持
并不可靠——`engine.pa.simulate_once(...)` 这种链式属性访问并不是 MATLAB Engine API
文档承诺支持的调用形式。`feval(name, *args, nargout=...)` 则是官方文档明确支持的
"按函数名字符串调用"机制，对包限定名（如 `"pa.simulate_once"`）与普通函数名同样有效，
是本系统里调用任意 `pa.*` 白名单函数的唯一实际调用路径。

单例的实现方式："约定单例"而非"强制单例"
------------------------------------------
design.md §6.3.1 给出的 `MatlabSession` 接口只有 `__enter__` / `__exit__` / `call`
三个方法，没有出现 `__new__` 覆写、模块级单例变量或任何"单例模式"机制。design.md
对"进程内单例"的原文表述是"进程内单例，串行使用"——这描述的是*使用方式*（全进程只
应该有一个活跃的 MATLAB Engine 会话，因为 MATLAB Engine 本身不支持同进程内多会话
并发的确定性行为），而不是要求 `MatlabSession` 类本身内置一套单例强制机制（如
`__new__` 拦截多次实例化、模块级全局实例、线程锁等）。

因此本模块把 `MatlabSession` 实现为一个普通类：它的"单例性"是一条**使用约定**，由
调用方（`controller` 在一次 `run_task()` 执行期间只构造并进入一个 `MatlabSession`
实例、把它传给需要它的函数）来保证，而不是由类自身的机制强制。这与 design.md §0.5
判据 1「不引入可配置的灵活性来回避决策」及项目对不必要机器化的一贯态度一致：
"只调用一次"本身是一个调用约定，不是需要用元类、模块级全局或锁来"解决"的问题；
给一个只需要被调用一次的类加装单例强制机制，属于为了绕开一次编排决策而添加与
design.md 未要求的额外机器化，本模块不引入。
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

# 白名单：`MatlabSession.call()` 只接受与下列函数名逐字符完全相等（区分大小写、
# 无前后空白容忍）的字符串；不做前缀匹配或模糊匹配，也不接受附带参数或语句的字符串
# （design.md §6.3.1；需求 R5.2）。
ALLOWED_MATLAB_FUNCTIONS: frozenset[str] = frozenset({
    "pa.inspect_model",
    "pa.simulate_once",
    "pa.simulate_batch",
    "pa.export_observables",
    "pa.run_linear_analysis",
})


class MatlabCallNotAllowedError(ValueError):
    """`MatlabSession.call()` 收到白名单外的函数名或任意 MATLAB 语句字符串时抛出。

    该异常在向 MATLAB Engine 发出本次调用**之前**抛出：不发生 Simulink 启动、不增加
    `engine_starts`，且同一 `MatlabSession` 实例对后续白名单调用保持可用
    （需求 R5.3；属性 CP-8）。
    """


class MatlabSession:
    """MATLAB Engine 生命周期管理；进程内单例（按调用约定），串行使用。

    用法：

        with MatlabSession() as session:
            session.call("pa.simulate_once", params_json, scenario_json, nargout=1)

    "进程内单例"是一条使用约定（见模块顶部说明），由调用方保证一次 `run_task()`
    执行期间只构造并进入一个实例；本类自身不内置单例强制机制。
    """

    def __init__(self) -> None:
        self._engine: Any | None = None

    def __enter__(self) -> "MatlabSession":
        """启动 MATLAB Engine 会话并持有其句柄；返回 `self`。

        `import matlab.engine` 延迟到此处执行（见模块顶部"延迟导入"说明），使本模块
        在未安装 MATLAB Engine for Python 的环境中仍可被安全 import。
        """
        import matlab.engine  # noqa: PLC0415 -- 有意延迟导入，见模块文档

        self._engine = matlab.engine.start_matlab()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """清理关闭 Engine 会话；不吞任何异常。

        `with` 块内抛出的异常按标准上下文管理器语义原样向外传播：本方法不返回
        真值，因此不会抑制异常。
        """
        if self._engine is not None:
            self._engine.quit()
            self._engine = None

    def call(self, fn: str, /, *args: object, nargout: int = 1) -> object:
        """只允许调用 `pa.*` 白名单函数，拒绝任意 MATLAB 语句（需求 R5.1, R5.2, R5.3）。

        校验使用逐字符完全相等（`fn in ALLOWED_MATLAB_FUNCTIONS`）：不做大小写归一化、
        不裁剪前后空白、不做前缀或模糊匹配。白名单外的 `fn` 在触碰 `self._engine`
        之前即抛出 `MatlabCallNotAllowedError`，本会话对后续白名单调用保持可用。

        白名单命中后，实际调用走 `self._engine.feval(fn, *args, nargout=nargout)`
        ——MATLAB Engine API 里对包限定函数名（如 `"pa.simulate_once"`）可靠生效的
        调用方式（见模块顶部说明），而非属性访问式调用。
        """
        if fn not in ALLOWED_MATLAB_FUNCTIONS:
            raise MatlabCallNotAllowedError(
                f"MATLAB call rejected: {fn!r} is not in ALLOWED_MATLAB_FUNCTIONS "
                f"(allowed: {sorted(ALLOWED_MATLAB_FUNCTIONS)!r})"
            )
        if self._engine is None:
            raise RuntimeError(
                "MatlabSession is not active; use 'with MatlabSession() as session:' "
                "before calling session.call(...)"
            )
        return self._engine.feval(fn, *args, nargout=nargout)
