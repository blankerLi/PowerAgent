function info = inspect_model(model_cfg_json)
% 校验 Block Path 与信号名存在性，返回 I/O 契约实际状态
%
%   info = INSPECT_MODEL(MODEL_CFG_JSON)
%
%   输入 MODEL_CFG_JSON 为 JSON 字符串，解码后应含：
%     model_path                                          - 待校验模型变体的入口文件路径（.slx）
%     io_contract.injectable_params.<name>.block_path      - 各设计变量对应的 Block Path
%     io_contract.output_signals.<name>.logsout_name        - 各输出信号在 logsout 中的名称
%   （字段布局对应 model.yaml 的 io_contract 节，见 design.md §4.2）
%
%   只读方式加载模型（未加载时 load_system，不触发保存、不修改磁盘文件），
%   逐项校验 injectable_params 的 block_path 与 output_signals 的 logsout_name
%   在该模型中是否存在，两类检查均通过时返回 struct，字段：
%     status           - 固定为 'ok'
%     model_path        - 回填的模型路径
%     checked_params    - 已校验通过的 injectable_params 名称列表（cell）
%     checked_signals   - 已校验通过的 output_signals 名称列表（cell）
%
%   任一声明的 Block Path 或 logsout_name 在模型中不存在时，抛出 MException，
%   标识符固定为 'pa:ContractError'（+pa 包内 ContractError 的统一标识符约定）。
%   ContractError 不属于 map_error.m 的 status 五值枚举（design.md §11.3），
%   因此不经 map_error.m 转换，而是原样向上传播，由 Python 侧 sim 层捕获后触发
%   stop_and_ask_human(cause='model_systemic_error')（Requirement 5 AC8）。

cfg = jsondecode(model_cfg_json);

model_path = cfg.model_path;
model_name = local_model_name(model_path);

if ~bdIsLoaded(model_name)
    load_system(model_path);
end

param_names = {};
if isfield(cfg, 'io_contract') && isfield(cfg.io_contract, 'injectable_params')
    param_names = fieldnames(cfg.io_contract.injectable_params);
end

missing_params = {};
for i = 1:numel(param_names)
    name = param_names{i};
    block_path = cfg.io_contract.injectable_params.(name).block_path;
    if ~local_block_exists(block_path)
        missing_params{end+1} = sprintf('%s:%s', name, block_path); %#ok<AGROW>
    end
end

signal_names = {};
if isfield(cfg, 'io_contract') && isfield(cfg.io_contract, 'output_signals')
    signal_names = fieldnames(cfg.io_contract.output_signals);
end

logged_names = local_logged_signal_names(model_name);
missing_signals = {};
for i = 1:numel(signal_names)
    name = signal_names{i};
    logsout_name = cfg.io_contract.output_signals.(name).logsout_name;
    if ~ismember(logsout_name, logged_names)
        missing_signals{end+1} = sprintf('%s:%s', name, logsout_name); %#ok<AGROW>
    end
end

if ~isempty(missing_params) || ~isempty(missing_signals)
    detail = local_format_missing(missing_params, missing_signals);
    error('pa:ContractError', ...
        'io_contract 声明的 Block Path 或输出信号在模型中不存在: %s', detail);
end

info = struct();
info.status = 'ok';
info.model_path = model_path;
info.checked_params = param_names;
info.checked_signals = signal_names;

end

function name = local_model_name(model_path)
[~, name] = fileparts(model_path);
end

function tf = local_block_exists(block_path)
% getSimulinkBlockHandle 的第二参数为 true 时，Block 不存在不报错而返回 -1
h = getSimulinkBlockHandle(block_path, true);
tf = (h ~= -1);
end

function names = local_logged_signal_names(model_name)
% 仿真尚未运行，无法从 logsout 反查；改为检查模型内已配置为 "Log Signal" 的
% 信号线（DataLogging='on'），其在仿真后进入 logsout 时使用的名称即
% DataLoggingName（未自定义时取信号线 Name）。
names = {};
lines = find_system(model_name, 'FindAll', 'on', 'LookUnderMasks', 'all', 'Type', 'line');
for i = 1:numel(lines)
    try
        if strcmp(get_param(lines(i), 'DataLogging'), 'on')
            nm = get_param(lines(i), 'DataLoggingName');
            if isempty(nm)
                nm = get_param(lines(i), 'Name');
            end
            if ~isempty(nm)
                names{end+1} = nm; %#ok<AGROW>
            end
        end
    catch
        % 并非全部 line 均具备 DataLogging 参数（如虚拟连接线），跳过
    end
end
end

function s = local_format_missing(missing_params, missing_signals)
parts = {};
if ~isempty(missing_params)
    parts{end+1} = ['injectable_params[' strjoin(missing_params, ', ') ']'];
end
if ~isempty(missing_signals)
    parts{end+1} = ['output_signals[' strjoin(missing_signals, ', ') ']'];
end
s = strjoin(parts, '; ');
end
