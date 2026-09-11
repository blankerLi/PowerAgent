function resolved = resolve_block_path(model_name, block_path)
% 把 io_contract 声明的 Block Path 的根段替换为实际加载的模型名
%
%   RESOLVED = RESOLVE_BLOCK_PATH(MODEL_NAME, BLOCK_PATH)
%
%   输入：
%     model_name  - 已加载模型的名称（char/string），由调用方从 model_path 的
%                    文件名取得（simulate_once.m / inspect_model.m 的
%                    local_model_name）。
%     block_path  - model.yaml 的 io_contract.injectable_params.<name>.block_path
%                    原样值，形如 'buck4ph/compensator/Rcomp'。
%
%   返回把第一段替换为 model_name 后的绝对 Block Path，例如
%   'buck4ph_switching/compensator/Rcomp'。
%
% -----------------------------------------------------------------------
% 为什么需要这一步（缺口，非风格问题）
%
%   set_param 与 getSimulinkBlockHandle 接受的是绝对 Block Path，其第一段必须
%   逐字符等于已加载的模型名。而 model.yaml 的 io_contract.injectable_params
%   只有**一份**、不按模型变体分列（design.md §4.2 的字段布局），其 block_path
%   根段写的是 'buck4ph'；实际的模型名由 model_package.<variant>.entry 的文件名
%   决定，是 'buck4ph_switching' 或 'buck4ph_averaged'，永远不等于 'buck4ph'。
%   不做这次替换，两个变体的注入与存在性校验都会失败：set_param 报无效对象、
%   getSimulinkBlockHandle 返回 -1 进而触发 pa:ContractError。
%
%   'buck4ph' 这个根段因此是一个**占位根**，而不是任何真实模型的名字；
%   model.yaml 对应字段的注释已说明这一点。
%
%   考虑过的另两条路都更贵：让 io_contract.injectable_params 按变体分列，改动会
%   扩散到 config/schema.py、controller/preflight.py 与本包的两个 .m；把两个变体
%   做成同一个 .slx 的 Variant Subsystem，则要求两者的 entry 是同一文件，
%   依赖闭包就无法区分「改了平均模型」与「改了开关模型」，model_package_hash
%   的失效语义随之瓦解。
%
% -----------------------------------------------------------------------
% 只按**第一个** '/' 切分，不用 strsplit
%
%   Simulink 的 Block Path 用 '//' 转义块名里的斜杠，因此
%   strsplit(block_path, '/') 会把含转义斜杠的块名切错。模型名本身不允许含
%   '/'，所以路径里第一个 '/' 必定是根段与其余部分的分隔符——只在这一处切分
%   即可，对下游任意层级、任意转义形式的子路径都原样保留。

model_name = char(model_name);
block_path = char(block_path);

sep = strfind(block_path, '/');
if isempty(sep)
    % block_path 只有根段（没有任何子路径）：整体就是模型名本身。
    resolved = model_name;
else
    resolved = [model_name block_path(sep(1):end)];
end

end
