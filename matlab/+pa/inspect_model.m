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
    % 与 apply_params.m 同一处理：block_path 的根段是占位根，须替换为实际模型名
    % 后才是合法的绝对 Block Path（见 private/resolve_block_path.m）。校验的必须
    % 是替换后的路径——否则本函数会对一个 apply_params.m 永远不会使用的路径报
    % 存在性通过或失败，两种方向都是错的。
    block_path = resolve_block_path(model_name, ...
        cfg.io_contract.injectable_params.(name).block_path);
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
% 仿真尚未运行，无法从 logsout 反查；改为检查模型内已开启信号记录的**输出端口**，
% 其在仿真后进入 logsout 时使用的名称即 DataLoggingName。
%
% 遍历 port 而不是 line（R2024a 实测修正）：
%   本函数早先版本遍历 'Type','line' 并读 get_param(line,'DataLogging')。R2024a 上
%   line 对象**没有** DataLogging 参数（实测 set_param/get_param 均报「line 没有名为
%   'DataLogging' 的参数」），信号记录是**端口**属性。而那个版本的循环体套着
%   try/catch，异常被静默吞掉，函数因此恒返回空 cell——后果不是报错而是
%   「所有 output_signals 都被判为缺失」，进而 pa:ContractError、preflight 失败。
%   静默失效比报错更难定位，此处记录该修正以免回退。
%
% DataLoggingName 的读法：
%   端口的 DataLoggingNameMode 为 'Custom' 时 DataLoggingName 是自定义名；为
%   'SignalName' 时 logsout 用信号线名。两种模式下 get_param(port,'DataLoggingName')
%   都返回最终生效的名称，因此不需要按模式分支——但名称为空（信号线未命名且未
%   自定义）时该端口无法在 logsout 里被按名取回，直接跳过而不是收进清单：收进去
%   会让一个取不到的信号看起来校验通过了。
names = {};
ports = find_system(model_name, 'FindAll', 'on', 'LookUnderMasks', 'all', ...
    'Type', 'port', 'PortType', 'outport');
for i = 1:numel(ports)
    try
        if strcmp(get_param(ports(i), 'DataLogging'), 'on')
            nm = get_param(ports(i), 'DataLoggingName');
            if ~isempty(nm)
                names{end+1} = char(nm); %#ok<AGROW>
            end
        end
    catch
        % 个别端口类型可能不具备 DataLogging 参数，跳过该端口。
        % 注意：这个 catch 不能掩盖「全部端口都没有该参数」这种系统性问题——
        % 那种情况下 names 为空，调用方会把全部 output_signals 报为缺失并抛
        % pa:ContractError，正是期望的行为。
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
