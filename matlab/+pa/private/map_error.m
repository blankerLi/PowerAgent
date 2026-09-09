function status = map_error(cfg, elapsed_s, caught_err, vout, iphase)
% MATLAB 异常/仿真结果 → status 五值枚举（{ok,diverged,solver_error,timeout,
% engine_transient}）的唯一映射。每次失败只输出一个枚举值，不返回自由文本。
%
%   STATUS = MAP_ERROR(CFG, ELAPSED_S, CAUGHT_ERR, VOUT, IPHASE)
%
%   输入（签名与 simulate_once.m 的唯一调用点已假定的契约一致，本文件不得
%   改变该签名）：
%     cfg         - 已解码的 model_cfg（struct）。本函数只读取两个判据来源
%                   字段：cfg.runtime.max_wallclock_per_run_s（timeout 判据，
%                   design.md §11.3）与
%                   cfg.io_contract.divergence_guard.{vout_abs_max,iphase_abs_max}
%                   （diverged 判据）。两者均为运行期传入的实测/配置数值，
%                   本函数不硬编码、不猜测其取值。
%     elapsed_s   - 本次调用的墙钟耗时（秒），由 simulate_once.m 用 tic/toc
%                   测得。
%     caught_err  - sim() 或其后处理阶段捕获的 MException；未抛出异常时为
%                   []（空）。
%     vout        - 仿真产出的 Vout 采样数组；异常导致未能产出时为 []。
%     iphase      - 仿真产出的 Iphase 采样数组（可能为多相，任意维度均可，
%                   本函数只关心其中是否存在 Inf/NaN 与其绝对值上界）；
%                   异常导致未能产出时为 []。
%
%   返回 STATUS 恒为下列五个字符串之一：'ok' | 'diverged' | 'solver_error'
%   | 'timeout' | 'engine_transient'。
%
% -----------------------------------------------------------------------
% 不变量（simulate_once.m 已保证，本文件据此省略防御性分支——见下方例外）：
%   simulate_once.m 的 catch 块在 caught_err 赋值之前已对
%   err.identifier=='pa:ContractError' 做 rethrow（design.md §11.3 /
%   Requirement 5 AC8：Block Path 或信号名不存在、白名单外键注入均属系统性
%   契约违反，不进入本函数的五值枚举分类）。因此本函数收到的 caught_err
%   理论上永不为 ContractError。下方仍保留一条防御性 rethrow 作为安全网
%   （若未来调用点变化、或本函数被其他调用方复用），不作为主路径逻辑。
%
% -----------------------------------------------------------------------
% 分类优先级（design.md 未显式规定多个条件同时成立时的优先级；本文件采用
% 以下顺序，并在此处显式登记，供后续核对/调整）：
%   1) timeout   — 一次运行既已超过墙钟上限，视为最严重的运行期信号，
%                  优先于其余判据给出结论（超时的仿真其输出信号即便看起来
%                  未发散，也不构成可信证据）。
%   2) diverged  — 其次判定输出信号是否已确证物理上不成立（Inf/NaN 或越
%                  安全界），因为这是仅次于超时的、有确凿数值证据的失败。
%   3) solver_error  — 若前两者均不成立而存在捕获异常，且异常特征匹配
%                  求解器不收敛/步长下限的已知模式。
%   4) engine_transient — 若异常特征匹配 Engine 连接/许可证瞬时问题模式。
%   5) ok        — 无捕获异常且前述判据均不成立。
%   6) 兜底 — 存在捕获异常但不匹配 3)/4) 的已知模式时，见下方"未分类异常
%                  的兜底策略"。
%
% -----------------------------------------------------------------------
% timeout 判据来源（design.md §11.3 / Requirement 5 AC7 显式要求）：
%   恰为 cfg.runtime.max_wallclock_per_run_s（单次运行墙钟上限，单位 s），
%   不取 cfg.io_contract.solver.stop_time（后者是仿真时间，不是墙钟）。
%   本函数不读取、不引用 stop_time 字段。
%
% -----------------------------------------------------------------------
% diverged 判据来源（design.md §11.3 / Requirement 5 AC7 显式要求）：
%   恰为 Vout 或 Iphase 出现 Inf/NaN、或其绝对值越过
%   cfg.io_contract.divergence_guard.{vout_abs_max,iphase_abs_max}，不取
%   constraints.yaml 的 hard_constraints（安全界是"这次仿真已不成立"，硬
%   约束是"候选不满足工程要求"，两者不可合并，design.md §4.2）。本函数不
%   读取、不引用任何 hard_constraints 字段——事实上 cfg（model_cfg）中也
%   不存在该节，constraints.yaml 是完全独立的配置文件，从未传入本函数。
%   vout/iphase 为空数组时跳过对应检查（不报错），不阻断另一信号的判定。
%
% -----------------------------------------------------------------------
% solver_error / engine_transient 的识别方式（design.md 只给出判据的语义
% 描述"求解器不收敛或步长触及下限"与"Engine 连接中断或许可证瞬时不可用"，
% 未给出具体 MATLAB 异常标识符；本文件在无 MATLAB 环境可供实测的条件下，
% 采用以下最佳努力（best-effort）启发式，明确标注为待 MATLAB Contract 测试
% 层（任务 2.5）用真实 MATLAB 异常核对/调整的项）：
%
%   solver_error 命中条件（identifier 或 message 命中任一子串，不区分大小写）：
%     identifier 含 'MinStepSizeViolation'（Simulink 步长下限相关标识符
%       的常见命名片段，如 'Simulink:Engine:MinStepSizeViolation'）
%     identifier 含 'SolverConvergence'（求解器收敛失败相关标识符的常见
%       命名片段）
%     message   含 'minimum step size'（步长下限，英文措辞常见于 Simulink
%       求解器报错文本）
%     message   含 'failed to converge' 或 'did not converge'（不收敛的
%       常见措辞）
%
%   engine_transient 命中条件（identifier 或 message 命中任一子串）：
%     identifier 以 'MATLAB:license:' 起始（许可证相关标识符的标准前缀）
%     identifier 含 'Engine' 且同时含 'Connection'/'Terminated'/'Lost'
%       之一（Engine 连接类问题的常见标识符组合）
%     message   含 'license' 且含 'checkout' 或 'unavailable'（许可证瞬时
%       不可用的常见措辞）
%     message   含 'engine' 且含 'terminated' 或 'connection'（Engine 连接
%       中断的常见措辞）
%
%   两类模式互不重叠时的判定顺序：solver_error 先判、engine_transient 后判
%   （见上方优先级列表第 3/4 项）；若某异常同时匹配两类模式（理论上不应
%   发生，因两组关键词已刻意不重叠），取 solver_error（求解器问题优先按
%   candidate_rejected 处理，成本更低、无需重试）。
%
% -----------------------------------------------------------------------
% 未分类异常的兜底策略（design.md 未规定；本文件的显式选择，标注为需真实
% MATLAB 环境验证的项，任务 2.5）：
%   caught_err 非空但其 identifier/message 不匹配上述任一已知模式时，
%   默认归为 'solver_error' 而非 'engine_transient'。理由：'engine_transient'
%   会触发 controller 侧的自动重试（design.md §8.4，最多重试
%   budget.max_attempts_per_scenario-1 次，每次重试按完整 budget_units 计入
%   预算），把未知异常误判为 engine_transient 会消耗额外预算重跑一个可能
%   注定失败的候选；'solver_error' 归入 candidate_rejected，不重试、成本
%   更低，是更保守的选择。该兜底分支预计随 MATLAB Contract 测试层（任务
%   2.5）积累真实异常样本后逐步收窄。
%
% -----------------------------------------------------------------------
% 本函数不做的事（design.md §11.3 显式要求）：
%   不返回自由文本；不在 vout/iphase 均为空且无 caught_err 时默认为
%   'diverged'（该情形归为 'ok'——没有证据表明仿真失败，见 local_is_diverged
%   的空数组处理）；不读取 constraints.yaml 的任何字段；不读取
%   io_contract.solver.stop_time。

% ---- 防御性安全网：理论上不会触发（见上方"不变量"说明） ----
if ~isempty(caught_err) && isfield(caught_err, 'identifier') ...
        && strcmp(caught_err.identifier, 'pa:ContractError')
    rethrow(caught_err);
end

% ---- 1) timeout：判据恰为 runtime.max_wallclock_per_run_s ----
if local_is_timeout(cfg, elapsed_s)
    status = 'timeout';
    return;
end

% ---- 2) diverged：判据恰为 Inf/NaN 或越 divergence_guard 界 ----
if local_is_diverged(cfg, vout, iphase)
    status = 'diverged';
    return;
end

% ---- 3)/4) 存在捕获异常时，按启发式模式匹配分类 ----
if ~isempty(caught_err)
    if local_is_solver_error(caught_err)
        status = 'solver_error';
        return;
    end
    if local_is_engine_transient(caught_err)
        status = 'engine_transient';
        return;
    end
    % ---- 6) 未分类异常的兜底：归为 solver_error（见上方说明） ----
    status = 'solver_error';
    return;
end

% ---- 5) 无捕获异常且前述判据均不成立 ----
status = 'ok';

end

% ========================================================================
function tf = local_is_timeout(cfg, elapsed_s)
% 判据恰为 cfg.runtime.max_wallclock_per_run_s；不取 io_contract.solver.stop_time。
tf = false;
if ~isfield(cfg, 'runtime') || ~isfield(cfg.runtime, 'max_wallclock_per_run_s')
    return;
end
limit_s = double(cfg.runtime.max_wallclock_per_run_s);
tf = elapsed_s > limit_s;
end

% ========================================================================
function tf = local_is_diverged(cfg, vout, iphase)
% 判据恰为 Vout/Iphase 的 Inf/NaN 或越 io_contract.divergence_guard 界；
% 不取 constraints.yaml 的 hard_constraints（该文件从未传入本函数）。
% 空数组（提取失败或未产出）时跳过对应检查，不报错、不阻断另一信号判定。
tf = false;

guard = struct();
if isfield(cfg, 'io_contract') && isfield(cfg.io_contract, 'divergence_guard')
    guard = cfg.io_contract.divergence_guard;
end

if ~isempty(vout)
    if any(~isfinite(vout(:)))
        tf = true;
        return;
    end
    if isfield(guard, 'vout_abs_max') && max(abs(vout(:))) > double(guard.vout_abs_max)
        tf = true;
        return;
    end
end

if ~isempty(iphase)
    if any(~isfinite(iphase(:)))
        tf = true;
        return;
    end
    if isfield(guard, 'iphase_abs_max') && max(abs(iphase(:))) > double(guard.iphase_abs_max)
        tf = true;
        return;
    end
end
end

% ========================================================================
function tf = local_is_solver_error(err)
% 最佳努力启发式，见文件头"solver_error / engine_transient 的识别方式"。
% 待 MATLAB Contract 测试层（任务 2.5）用真实异常样本核对/调整。
id_lower = lower(local_safe_field(err, 'identifier'));
msg_lower = lower(local_safe_field(err, 'message'));

tf = contains(id_lower, lower('MinStepSizeViolation')) ...
    || contains(id_lower, lower('SolverConvergence')) ...
    || contains(msg_lower, 'minimum step size') ...
    || contains(msg_lower, 'failed to converge') ...
    || contains(msg_lower, 'did not converge');
end

% ========================================================================
function tf = local_is_engine_transient(err)
% 最佳努力启发式，见文件头"solver_error / engine_transient 的识别方式"。
% 待 MATLAB Contract 测试层（任务 2.5）用真实异常样本核对/调整。
id_lower = lower(local_safe_field(err, 'identifier'));
msg_lower = lower(local_safe_field(err, 'message'));

license_id = startsWith(id_lower, lower('MATLAB:license:'));
engine_conn_id = contains(id_lower, 'engine') ...
    && (contains(id_lower, 'connection') || contains(id_lower, 'terminated') ...
        || contains(id_lower, 'lost'));
license_msg = contains(msg_lower, 'license') ...
    && (contains(msg_lower, 'checkout') || contains(msg_lower, 'unavailable'));
engine_conn_msg = contains(msg_lower, 'engine') ...
    && (contains(msg_lower, 'terminated') || contains(msg_lower, 'connection'));

tf = license_id || engine_conn_id || license_msg || engine_conn_msg;
end

% ========================================================================
function v = local_safe_field(err, field_name)
% err 为 MException 或等价 struct；字段缺失时返回空字符串，保证上层的
% lower()/contains() 调用不因缺字段而报错。
if isfield(err, field_name) && ~isempty(err.(field_name))
    v = char(err.(field_name));
else
    v = '';
end
end
