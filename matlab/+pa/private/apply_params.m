function apply_params(model_name, injectable_params, params_si)
% 全系统唯一参数注入实现：按 io_contract.injectable_params 白名单逐键 set_param 注入
%
%   APPLY_PARAMS(MODEL_NAME, INJECTABLE_PARAMS, PARAMS_SI)
%
%   输入：
%     model_name         - 已加载模型的名称（char/string）。调用方（simulate_once.m）
%                           负责确保该模型已加载（bdIsLoaded），本函数不加载、不保存、
%                           不修改磁盘上的模型文件。
%     injectable_params  - struct，键为设计变量名（如 rcomp/ccomp），每个字段为一个
%                           struct，含 block_path（Simulink Block Path，char；其根段
%                           是占位根，注入前经 resolve_block_path.m 替换为实际
%                           model_name，见该文件的文件头说明）、
%                           param（该 Block 的参数名，char）与 unit（SI 单位标识，
%                           仅供追溯、不做单位换算）。取自 model.yaml 的
%                           io_contract.injectable_params 白名单（design.md §4.2），
%                           由调用方解析后以 MATLAB struct 传入——JSON 解码边界在
%                           公开函数 simulate_once.m，本私有函数不做 jsondecode。
%     params_si           - struct，键为候选参数名，值为该参数在 SI 单位下的数值。
%                           取值已是 SI 单位（injectable_params 白名单中的 unit 字段
%                           只是声明调用方须提供的单位，本函数不做任何换算）。
%
%   全部键先做白名单存在性检查，检查全部通过后才逐键调用 set_param 注入；
%   只要 params_si 中出现任一不在 injectable_params 白名单内的键，本函数不对任何
%   键执行注入、不启动仿真，直接抛出 MException，标识符固定为 'pa:ContractError'
%   （+pa 包内 ContractError 的统一标识符约定，与 inspect_model.m 一致）。

param_names = fieldnames(params_si);
whitelist_names = fieldnames(injectable_params);

unknown_names = setdiff(param_names, whitelist_names);
if ~isempty(unknown_names)
    error('pa:ContractError', ...
        '参数注入键不在 io_contract.injectable_params 白名单内: %s', ...
        strjoin(unknown_names, ', '));
end

for i = 1:numel(param_names)
    name = param_names{i};
    spec = injectable_params.(name);
    value = params_si.(name);
    % block_path 的根段是占位根（model.yaml 只有一份 injectable_params，不按变体
    % 分列），须替换为实际加载的模型名后才是合法的绝对 Block Path。
    % 见 resolve_block_path.m 的文件头说明。
    resolved_path = resolve_block_path(model_name, spec.block_path);
    set_param(resolved_path, spec.param, local_format_value(value));
end

end

function s = local_format_value(value)
% Simulink 块的数值型对话框参数以字符串形式存储；按双精度可 round-trip 的
% 17 位有效数字格式化，避免十进制截断引入的注入偏差（候选值已在 Python 侧
% 归一到 SI 单位，本函数只负责把该数值转换为 set_param 可接受的字符串形式）。
s = sprintf('%.17g', value);
end
