function collect_signals(sim_out, waveform_path, cfg, scenario)
% 从一次已完成的 sim() 结果中按 I/O 契约取出信号，落盘为 eval 层可直接读取的 MAT
%
%   COLLECT_SIGNALS(SIM_OUT, WAVEFORM_PATH, CFG, SCENARIO)
%
%   SIM_OUT       - sim() 的返回值（Simulink.SimulationOutput），由 simulate_once.m
%                    在仿真成功（status='ok'）后传入。
%   WAVEFORM_PATH - 目标 MAT 文件的完整路径（由 simulate_once.m 的
%                    local_resolve_waveform_path 产出）。本函数只负责按该路径落盘，
%                    不做路径解析、不做原子移动——原子移动、sha256 往返校验与最终
%                    归档目录由 Python 侧 store.artifacts.ArtifactStore 负责
%                    （design.md §6.9）。
%   CFG           - 已解码的 model_cfg，本函数只读 io_contract.output_signals
%                    （每项的 logsout_name 既是 logsout 里的取回名，也是落盘 MAT
%                    里的顶层变量名）。
%   SCENARIO      - 已解码的 scenario，本函数只读可选字段 step_trigger_s。
%
% -----------------------------------------------------------------------
% 落盘格式由消费侧决定，不由生产侧发明
%
%   eval/metrics.py 的 _load_waveform() 用
%   scipy.io.loadmat(squeeze_me=True, struct_as_record=False) 读产物，并假定：
%     - 每个信号名是一个**顶层变量**，值为含 time / data 两个字段的结构
%     - step_trigger_s / sim_start_s / sim_end_s 是顶层标量变量
%     - 以 '__' 开头的键（MAT 自带 header）被跳过
%   sim/backends/waveform_io.py 已经确立了这条口径：让生产侧适配已有的消费侧契约，
%   两个后端共享同一个加载器，eval/metrics.py 一行不用改。本函数是 MATLAB 侧对
%   同一契约的实现。
%
%   本函数早先版本落盘的是 save(waveform_path, 'logsout', '-v7.3')，即变量名
%   'logsout' 的 Simulink.SimulationData.Dataset 对象。那份格式有两个独立的问题，
%   叠加后 Python 侧完全读不了：
%     1) '-v7.3' 是 HDF5 格式，scipy.io.loadmat **不支持**，直接抛异常；
%     2) 即使换成 '-v7'，Dataset 是 MATLAB 类对象，loadmat 读出来是不透明的
%        结构化数组，对不上上述形状。
%   因此本函数改为逐信号取出数值数组后按上述形状落盘，并显式指定 '-v7'。
%
% -----------------------------------------------------------------------
% 用 save 的 '-struct' 展开为顶层变量
%
%   payload 的每个字段被 save(..., '-struct', 'payload') 存成一个**独立的顶层
%   变量**，而不是存成一个名为 payload 的嵌套结构。这正是 _load_waveform() 遍历
%   raw.items() 时期望看到的布局；把它们塞进一层 payload 结构会让消费侧多一层
%   解包，而那层解包在 Python 后端的产物里并不存在，两个后端的产物就不再同形。
%
% -----------------------------------------------------------------------
% 信号缺失按 ContractError 处理，不静默跳过
%
%   io_contract 声明了某个 output_signal 而 logsout 里取不到它，属于系统性契约
%   违反（与 inspect_model.m 的判定对象是同一件事，只是发生在运行期而非
%   preflight）。本函数抛 'pa:ContractError'，与 inspect_model.m / apply_params.m
%   的统一标识符约定一致；simulate_once.m 对 collect_signals 的调用位于 try/catch
%   之外，因此该异常原样传播到 Python 侧，由 sim 层触发
%   stop_and_ask_human(cause='model_systemic_error')。
%
%   静默跳过是更坏的选择：产物会少一个信号，而 eval/metrics.py 只会把引用该信号
%   的指标标成 signal_missing，于是一个模型契约问题会被记成若干条指标无效，
%   真正的原因不出现在任何地方。

logsout = local_get_logsout(sim_out);

if ~isfield(cfg, 'io_contract') || ~isfield(cfg.io_contract, 'output_signals')
    error('pa:ContractError', ...
        'model_cfg 缺少 io_contract.output_signals，无法确定要落盘哪些信号');
end

signal_keys = fieldnames(cfg.io_contract.output_signals);
payload = struct();
first_time = [];

for i = 1:numel(signal_keys)
    key = signal_keys{i};
    logsout_name = char(cfg.io_contract.output_signals.(key).logsout_name);

    [t, d, found] = local_signal_series(logsout, logsout_name);
    if ~found
        error('pa:ContractError', ...
            'io_contract.output_signals.%s 声明的 logsout 信号不存在: %s', ...
            key, logsout_name);
    end

    % 变量名取 logsout_name（而非 io_contract 的键名）：metrics.yaml 的 signal
    % 字段引用的是 logsout_name（'Vout'/'Iout'/'Iphase'），两侧必须一致。
    payload.(logsout_name) = struct('time', t, 'data', d);

    if isempty(first_time)
        first_time = t;
    end
end

% sim_start_s / sim_end_s 取第一个信号时间轴的首末值，与 Python 后端
% save_run_waveform() 的取法一致（那里取 run.time_s[0] / run.time_s[-1]）。
if isempty(first_time)
    payload.sim_start_s = 0.0;
    payload.sim_end_s = 0.0;
else
    payload.sim_start_s = double(first_time(1));
    payload.sim_end_s = double(first_time(end));
end

% step_trigger_s 由 Python 侧按 STEP_TRIGGER_FRACTION * stop_time 算好后经
% scenario_json 传入（两个后端共用同一公式，否则两份波形在时间轴上对不齐，
% 双模型/双后端一致性核对无从进行）。缺失时不写该变量——_load_waveform() 会让
% Waveform.step_trigger_s 保持 None，引用 step_trigger 系窗口的指标落
% no_step_detected。不伪造一个触发时刻。
if isfield(scenario, 'step_trigger_s') && ~isempty(scenario.step_trigger_s)
    payload.step_trigger_s = double(scenario.step_trigger_s);
end

save(waveform_path, '-struct', 'payload', '-v7');

end

function logsout = local_get_logsout(sim_out)
% Simulink 默认信号记录集合变量名为 'logsout'（与 simulate_once.m 的
% local_get_logsout 同一假定；若模型配置了其他 SignalLoggingName，两处需同步）。
try
    logsout = sim_out.get('logsout');
catch
    logsout = sim_out.logsout;
end
end

function [t, d, found] = local_signal_series(logsout, name)
% 按名取回一个记录信号的 (Time, Data)。取不到时 found=false，由调用方决定如何
% 处理——本函数不抛异常，因为「缺失」的语义判定属于调用方（见文件头说明）。
t = [];
d = [];
found = false;
try
    el = logsout.getElement(name);
    t = double(el.Values.Time);
    d = double(el.Values.Data);
    found = true;
catch
    % getElement 对不存在的名称抛异常；保持 found=false。
end
end
