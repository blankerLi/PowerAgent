function res = simulate_once(model_cfg_json, params_json, scenario_json)
% 构造运行时参数注入 → 运行一次仿真 → 落盘全量 logsout；返回值只含
% status / waveform_path / elapsed_ms / engine_starts，不含波形采样点本体。
%
%   RES = SIMULATE_ONCE(MODEL_CFG_JSON, PARAMS_JSON, SCENARIO_JSON)
%
%   三个输入均为 JSON 字符串：
%     model_cfg_json - 解码后应含 model_path（已由 Python 侧按 scenario 的
%                      model_variant 解析出的入口文件路径，与 inspect_model.m
%                      的假设一致）、io_contract.injectable_params（注入白名单，
%                      转交 apply_params.m）、io_contract.output_signals（
%                      vout/iphase 的 logsout 名称，供发散判据取值）、
%                      io_contract.divergence_guard（vout_abs_max/iphase_abs_max，
%                      map_error.m 的 diverged 判据来源）、runtime.max_wallclock_per_run_s
%                      （map_error.m 的 timeout 判据来源）。可选 output_dir 字段
%                      指定波形落盘目录（见 local_resolve_waveform_path）。
%     params_json    - 待注入的设计变量取值，如 {"rcomp": 11000.0, "ccomp": 2.3e-9}；
%                      键集合须是 model_cfg_json 的 injectable_params 白名单的子集，
%                      白名单校验本身由 apply_params.m 负责（见下方软依赖说明）。
%     scenario_json  - 场景条件，含 vin_v / temp_c / load_start_a / load_end_a /
%                      slew_a_per_us（task.yaml 场景行既有字段名，design.md §4.1）。
%                      可选 run_id 字段用于命名落盘的波形文件。
%
%   返回 RES 为 struct，字段固定四项：
%     status        - {'ok','diverged','solver_error','timeout','engine_transient'} 之一
%     waveform_path - status='ok' 时为已落盘 MAT 文件的路径；否则为空字符串
%     elapsed_ms    - 本次调用的墙钟耗时（毫秒，由 tic/toc 测得）
%     engine_starts - 本次调用中真实触发 sim() 的次数（正常路径恰为 1；
%                     apply_params 或场景注入阶段失败时为 0，因为尚未启动仿真）
%
% -----------------------------------------------------------------------
% 软依赖说明（本文件依赖两个尚未落地的任务，均已在下方以约定签名调用）：
%
%   1) matlab/+pa/private/apply_params.m（任务 2.2，撰写本文件时尚未实现）
%      约定签名：apply_params(model_name, injectable_params_whitelist, values)
%        - injectable_params_whitelist：取自 model_cfg 的
%          io_contract.injectable_params（struct，键为设计变量名，
%          值含 block_path/param/unit）
%        - values：取自 params_json 解码结果（struct，键为设计变量名）
%        - 全系统唯一参数注入实现；白名单外的键出现时不注入任何键、不启动
%          仿真、抛出标识符为 'pa:ContractError' 的 MException（design.md
%          任务 2.2 / Requirement 5 AC10）。
%      本文件对该契约的处理：'pa:ContractError' 不经 map_error 分类，原样
%      向上抛出（见下方第 3 点）。
%
%   2) matlab/+pa/private/map_error.m（任务 2.3，撰写本文件时尚未实现）
%      约定签名：status = map_error(cfg, elapsed_s, caught_err, vout, iphase)
%        - cfg：已解码的 model_cfg（含 runtime.max_wallclock_per_run_s 与
%          io_contract.divergence_guard 两个判据来源字段，design.md §11.3）
%        - elapsed_s：本次调用的墙钟耗时（秒）
%        - caught_err：sim() 或其后处理阶段抛出的 MException；未抛出时为 []
%        - vout/iphase：仿真产出的输出信号数组；因异常未能产出时为 []
%      map_error 是 MATLAB 异常 → status 五值枚举的唯一映射（design.md
%      §11.3），本文件不内联实现该分类逻辑（不重复 2.3 的注册表角色），
%      只负责测量 elapsed_s 与提取 vout/iphase 后转交判定。
%
% -----------------------------------------------------------------------
% ContractError 的传播方式（design.md §11.3 / Requirement 5 AC8）：
%   Block Path 或信号名不存在、或白名单外键注入，两者均属系统性契约违反，
%   不经 map_error 的五值枚举分类，而是原样向上抛出，由 Python 侧 sim 层
%   捕获后触发 stop_and_ask_human(cause='model_systemic_error')。因此本文件
%   在 catch 块中显式检查 err.identifier=='pa:ContractError' 并 rethrow。
%
% -----------------------------------------------------------------------
% 场景条件注入方式（design.md 只说明"模型工作区"操作在 MATLAB 侧完成，
% 未给出场景字段到模型的具体映射方式，此处为本文件的解释，供后续核对/调整）：
%   将 scenario_json 中的 vin_v / temp_c / load_start_a / load_end_a /
%   slew_a_per_us 五个电气量以同名变量写入模型工作区（Simulink.ModelWorkspace，
%   经 get_param(model_name,'ModelWorkspace') 取得），模型内部的 Vin 源、
%   负载阶跃块与温度相关查表约定通过读取这些工作区变量获得场景条件。
%   未出现在 scenario_json 中的字段不做任何写入。rcomp/ccomp 两个设计变量
%   不走此路径，全部委托给 apply_params.m（design.md 任务 2.2 明确其为
%   "全系统唯一参数注入实现"，本文件不重复其 set_param 注入逻辑，也不再
%   额外构造 Simulink.SimulationInput 对象做同一件事）。
%
% -----------------------------------------------------------------------
% 波形落盘路径的解析方式（design.md 未在三个 JSON 入参中显式定义"波形应
% 写到哪个路径"，Python 侧 store.artifacts.ArtifactStore.stage() 才拥有
% 产物落盘目录的最终约定，见下方 local_resolve_waveform_path 的详细说明）：
%   本文件只产出一个可写路径并把全量 logsout 落盘到该路径（经 collect_signals.m），
%   实际的原子移动、sha256 往返校验与最终归档目录由 Python 侧
%   store.artifacts.ArtifactStore 负责（design.md §6.9），本文件返回的
%   waveform_path 只是一个"本次调用产出的临时/工作路径"引用。
%
% -----------------------------------------------------------------------
% 计时/预算判据的取值来源（不在本文件硬编码任何阈值，任务 8 明确要求）：
%   elapsed_s 只测量并传给 map_error，timeout 阈值（runtime.max_wallclock_per_run_s）
%   与 diverged 阈值（io_contract.divergence_guard.*）均由 map_error.m 从
%   cfg 中读取判定，本文件不做任何比较。

t_start = tic;
engine_starts = 0;

cfg = jsondecode(model_cfg_json);
params = jsondecode(params_json);
scenario = jsondecode(scenario_json);

model_path = cfg.model_path;
model_name = local_model_name(model_path);

sim_out = [];
caught_err = [];
vout = [];
iphase = [];

try
    if ~bdIsLoaded(model_name)
        load_system(model_path);
    end

    % ---- 设计变量注入：唯一委托给 apply_params.m（任务 2.2，软依赖） ----
    if isfield(cfg, 'io_contract') && isfield(cfg.io_contract, 'injectable_params')
        whitelist = cfg.io_contract.injectable_params;
    else
        whitelist = struct();
    end
    apply_params(model_name, whitelist, params);

    % ---- 场景条件注入：模型工作区变量赋值（见上方说明） ----
    local_assign_scenario_to_workspace(model_name, scenario);

    engine_starts = 1;
    sim_out = sim(model_name);

    logsout = local_get_logsout(sim_out);
    [vout, iphase] = local_extract_guard_signals(logsout, cfg);

catch err
    if strcmp(err.identifier, 'pa:ContractError')
        rethrow(err);
    end
    caught_err = err;
end

elapsed_s = toc(t_start);

% ---- 状态映射：唯一委托给 map_error.m（任务 2.3，软依赖） ----
status = map_error(cfg, elapsed_s, caught_err, vout, iphase);

if strcmp(status, 'ok')
    waveform_path = local_resolve_waveform_path(cfg, scenario);
    collect_signals(sim_out, waveform_path);
else
    waveform_path = '';
end

res = struct();
res.status = status;
res.waveform_path = waveform_path;
res.elapsed_ms = round(elapsed_s * 1000);
res.engine_starts = engine_starts;

end

function name = local_model_name(model_path)
[~, name] = fileparts(model_path);
end

function local_assign_scenario_to_workspace(model_name, scenario)
mws = get_param(model_name, 'ModelWorkspace');
scenario_fields = {'vin_v', 'temp_c', 'load_start_a', 'load_end_a', 'slew_a_per_us'};
for i = 1:numel(scenario_fields)
    f = scenario_fields{i};
    if isfield(scenario, f)
        assignin(mws, f, double(scenario.(f)));
    end
end
end

function logsout = local_get_logsout(sim_out)
% Simulink 默认信号记录集合变量名为 'logsout'（design.md 未显式声明该名称，
% 按惯例处理；若模型实际使用其他记录变量名，需同步调整本函数与
% collect_signals.m）。
try
    logsout = sim_out.get('logsout');
catch
    logsout = sim_out.logsout;
end
end

function [vout, iphase] = local_extract_guard_signals(logsout, cfg)
% 供 map_error.m 做 diverged 判据（Inf/NaN 或越 divergence_guard 界）使用的
% 原始信号数组；提取失败时返回空数组，交由 map_error 自行处理（例如判定
% 为 solver_error 或视信号缺失为不可判定）。
vout = [];
iphase = [];
if ~isfield(cfg, 'io_contract') || ~isfield(cfg.io_contract, 'output_signals')
    return;
end
sig = cfg.io_contract.output_signals;
if isfield(sig, 'vout')
    vout = local_signal_values(logsout, sig.vout.logsout_name);
end
if isfield(sig, 'iphase')
    iphase = local_signal_values(logsout, sig.iphase.logsout_name);
end
end

function v = local_signal_values(logsout, name)
try
    el = logsout.getElement(name);
    v = el.Values.Data;
catch
    v = [];
end
end

function p = local_resolve_waveform_path(cfg, scenario)
% 本文件的解释（design.md 未在三个 JSON 入参中显式定义波形落盘路径字段，
% 供 Python 侧 sim.simulate()（任务 7.2）落地时核对/覆盖）：
%   1) scenario_json 提供非空 run_id 字段时，取其值作为文件名主体，
%      与 store 的 run_id 对应，便于人工排查；
%   2) 否则取时间戳 + 随机数后缀，保证同一进程内不冲突；
%   3) 输出目录：model_cfg_json 提供非空 output_dir 字段则用之；否则取
%      fullfile(tempdir, 'poweragent_waveforms')（自动创建）。
%   本函数只产出一个可写路径，实际的原子落盘、跨机器 sha256 校验与最终
%   归档目录由 Python 侧 store.artifacts.ArtifactStore 负责（design.md §6.9）。
if isfield(scenario, 'run_id') && ~isempty(scenario.run_id)
    stem = char(scenario.run_id);
else
    stem = sprintf('run_%s_%06d', datestr(now, 'yyyymmddHHMMSSFFF'), randi(999999));
end

if isfield(cfg, 'output_dir') && ~isempty(cfg.output_dir)
    out_dir = cfg.output_dir;
else
    out_dir = fullfile(tempdir, 'poweragent_waveforms');
end
if ~exist(out_dir, 'dir')
    mkdir(out_dir);
end
p = fullfile(out_dir, [stem '_logsout.mat']);
end
