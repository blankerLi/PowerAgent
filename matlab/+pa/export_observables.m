function path = export_observables(run_ref, spec_json)
% 从一份已落盘的波形（simulate_once.m 产出的 logsout MAT 文件）中按
% ObservableSpec 抽取/聚合指定信号，把抽取结果另存为一个新的 MAT 文件，
% 返回该文件路径。
%
%   PATH = EXPORT_OBSERVABLES(RUN_REF, SPEC_JSON)
%
%   RUN_REF   - 指向此前 pa.simulate_once 落盘的波形 MAT 文件的路径字符串
%               （即 simulate_once.m 返回值中的 waveform_path，或 Python 侧
%               store.artifacts 归档后回填的等价路径）。该 MAT 文件内含
%               变量名 'logsout' 的 Simulink.SimulationData.Dataset 对象
%               （由 collect_signals.m 写入）。
%   SPEC_JSON - ObservableSpec 的 JSON 字符串，本文件对其字段的解释如下
%               （design.md §6.3.2/§6.4 只给出函数签名，未定义 ObservableSpec
%               的具体字段；本解释参照 metrics.yaml 中 constraint_observables
%               与各 MetricSpec 已确立的字段命名习惯：signal/window/
%               filter/aggregation/unit，供后续核对/调整）：
%
%     signal       (string,必填)  - 待抽取信号在 logsout 中的名称（即
%                                   model.yaml io_contract.output_signals
%                                   中该信号的 logsout_name，由 Python 侧
%                                   在构造 spec_json 前解析出来传入，本文件
%                                   不做 model.yaml 到 logsout 名称的映射）。
%     window       ([t0,t1],可选) - 绝对仿真时间窗口（单位 s，闭区间）。
%                                   metrics.yaml 中的窗口用 sim_start /
%                                   steady_start 等符号标签表示，这些符号到
%                                   绝对时刻的换算由 Python 侧
%                                   eval.metrics（已知阶跃触发时刻、稳态
%                                   窗口定义等）完成后再传入本函数；本函数
%                                   只接受已换算好的数值窗口。省略或为空时
%                                   取该信号的完整时间范围。
%     aggregation  (string,可选)  - {'min','max','mean','raw'} 之一，默认
%                                   'raw'。'min'/'max'/'mean' 对应
%                                   constraint_observables 已使用的聚合方式
%                                   （metrics.yaml 的 obs.vout_min/obs.vout_max
%                                   即 min/max 聚合的先例）；'raw' 返回窗口内
%                                   完整的 (time, data) 采样序列，供暂不需要
%                                   聚合、只需窗口内原始数据的调用方使用
%                                   （例如报告绘图）。
%     output_path  (string,可选)  - 目标 MAT 文件路径。省略时在 run_ref 同
%                                   目录下按 "<run_ref stem>.obs_<signal>_
%                                   <aggregation>.mat" 命名（见
%                                   local_default_output_path）。
%
%   返回的 MAT 文件内变量：
%     signal      - 抽取的信号名（回填 spec 输入值）
%     aggregation - 实际使用的聚合方式
%     window      - 实际使用的时间窗口 [t0, t1]（'raw' 时同样记录，便于追溯）
%     value       - 'min'/'max'/'mean' 时为标量；'raw' 时为空（数据见 time/data）
%     time        - 'raw' 时为窗口内时间向量；聚合模式下为空
%     data        - 'raw' 时为窗口内数据向量；聚合模式下为空
%
%   本函数不接受 model_cfg_json：export_observables 与具体模型/求解器设置
%   解耦，只对"已落盘的 logsout 数据"做窗口截取与聚合，因此不加载 .slx、
%   不启动仿真、不增加 engine_starts（design.md 任务 2.4 的 ALLOWED_MATLAB_
%   FUNCTIONS 白名单把 pa.export_observables 列为与 pa.simulate_once 平级的
%   独立可调用项，供 Python 侧在寻优期反复按需抽取不同观测量，而不必每次
%   都重新解析整份 logsout）。

spec = jsondecode(spec_json);

if ~isfield(spec, 'signal') || isempty(spec.signal)
    error('pa:ContractError', 'export_observables: spec_json 缺少必填字段 signal');
end
signal_name = spec.signal;

if isfield(spec, 'aggregation') && ~isempty(spec.aggregation)
    aggregation = spec.aggregation;
else
    aggregation = 'raw';
end

loaded = load(run_ref, 'logsout');
logsout = loaded.logsout;

try
    el = logsout.getElement(signal_name);
    full_time = el.Values.Time;
    full_data = el.Values.Data;
catch err
    error('pa:ContractError', ...
        'export_observables: 信号 %s 在 %s 的 logsout 中不存在: %s', ...
        signal_name, run_ref, err.message);
end

if isfield(spec, 'window') && ~isempty(spec.window)
    t0 = spec.window(1);
    t1 = spec.window(2);
else
    t0 = full_time(1);
    t1 = full_time(end);
end

mask = (full_time >= t0) & (full_time <= t1);
win_time = full_time(mask);
win_data = full_data(mask);

value = [];
out_time = [];
out_data = [];

switch aggregation
    case 'min'
        value = min(win_data);
    case 'max'
        value = max(win_data);
    case 'mean'
        value = mean(win_data);
    case 'raw'
        out_time = win_time;
        out_data = win_data;
    otherwise
        error('pa:ContractError', ...
            'export_observables: 不支持的 aggregation 取值: %s', aggregation);
end

if isfield(spec, 'output_path') && ~isempty(spec.output_path)
    path = spec.output_path;
else
    path = local_default_output_path(run_ref, signal_name, aggregation);
end

window = [t0, t1]; %#ok<NASGU>
signal = signal_name; %#ok<NASGU>
time = out_time; %#ok<NASGU>
data = out_data; %#ok<NASGU>
save(path, 'signal', 'aggregation', 'window', 'value', 'time', 'data');

end

function p = local_default_output_path(run_ref, signal_name, aggregation)
[dir_, stem, ~] = fileparts(run_ref);
safe_signal = regexprep(signal_name, '[^A-Za-z0-9_]', '_');
p = fullfile(dir_, sprintf('%s.obs_%s_%s.mat', stem, safe_signal, aggregation));
end
