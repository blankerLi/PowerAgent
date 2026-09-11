function res = run_linear_analysis(model_cfg_json, params_json, scenario_json, margin_cfg_json)
% 稳定性裕量的"采集"阶段：在已注入参数/场景的模型上运行线性化或频响估计，
% 把原始频响/裕量数据落盘为 MAT 文件；不在本文件内计算最终的
% phase_margin(deg)/gain_margin(dB) 数值——那是 eval.margin.extract_margin()
% （任务 11.3，Python 侧，不计预算）的职责，本文件只负责"采集"（计预算，
% design.md §6.5.2「裕量链路按采集与计算两段拆分」）。
%
%   RES = RUN_LINEAR_ANALYSIS(MODEL_CFG_JSON, PARAMS_JSON, SCENARIO_JSON, MARGIN_CFG_JSON)
%
%   四个输入均为 JSON 字符串：
%     model_cfg_json  - 解码后应含 model_path、io_contract.injectable_params
%                       （转交 apply_params.m）。本文件信任 model_path 已由
%                       Python 侧（任务 7.2）按 margin_cfg.primary_method 解析出
%                       正确的模型变体入口（'linear_analysis_on_averaged' 对应
%                       averaged 变体、'freq_response_estimator_on_switching'
%                       对应 switching 变体，design.md §8.5 的算法描述）；本文件
%                       自身不再按 primary_method 在 switching/averaged 两个模型
%                       文件之间做二次选择，只使用已解析好的 model_path。
%     params_json     - 待注入的设计变量取值，语义与 simulate_once.m 一致，
%                       全部委托给 apply_params.m。
%     scenario_json   - 场景条件，字段集合与 simulate_once.m 一致
%                       （vin_v/temp_c/load_start_a/load_end_a/slew_a_per_us）。
%     margin_cfg_json - 解码后应含 primary_method 字段，取值属于
%                       {linear_analysis_on_averaged, freq_response_estimator_on_switching}
%                       （metrics.yaml 的 margin_extraction.primary_method，
%                       design.md §4.3）。
%
%   返回 RES 为 struct，字段固定四项：
%     status             - 'ok' | 'engine_transient'（只两值，见下方说明，
%                           不复用 map_error.m 的五值枚举）
%     freq_response_path - status='ok' 且线性化/频响估计产出可用数据时为已
%                           落盘 MAT 文件路径；其余情况（engine_transient，
%                           或线性化/频响估计本身失败但未触发引擎级异常）为
%                           空字符串
%     method              - 原样回传输入的 primary_method 字符串，供 Python
%                           侧/落盘产物追溯本次实际采用的方法
%     engine_starts       - 本次调用中实际发起 linearize/frestimate 的次数
%                           （正常路径恰为 1；apply_params 或场景注入阶段失败
%                           时为 0，因为尚未触及线性化/频响估计调用，与
%                           simulate_once.m 的 engine_starts 记账口径一致）
%
% -----------------------------------------------------------------------
% 为什么本文件不复用 map_error.m 的五值枚举（设计判断，显式登记）：
%   map_error.m 的签名是 (cfg, elapsed_s, caught_err, vout, iphase)——
%   vout/iphase 是仿真的时域输出信号，线性化/频响估计调用没有这两个量，
%   强行传空数组给 map_error.m 只会让它的 diverged/timeout 判据全部失效，
%   变成一个只剩"看 caught_err 猜 solver_error/engine_transient"的空壳调用，
%   没有复用到任何实质逻辑。design.md §11.3 的错误映射表里，"linearize/
%   frestimate 失败"本身就是独立于五值枚举的一行："status 仍为 'ok'，但
%   observable_ref 为空"——这说明本文件的 status 语义原本就与 map_error.m
%   的 status 不是同一个枚举空间，不应该借用同一个函数名/同一套五值输出
%   来表达两件不同的事。因此本文件采用更窄的两值判定，直接对应该表格行：
%     - MATLAB Engine 连接中断/许可证瞬时不可用 → 'engine_transient'
%       （与 map_error.m 处理的是同一类现象，因此复用同一套识别启发式，
%       见下方"引擎瞬态异常识别"）。
%     - 其余任何异常（包括 linearize/allmargin/frestimate 因数值奇异、
%       模型未配置线性分析点等原因抛出的异常）→ status 仍为 'ok'，
%       freq_response_path 置空——这就是"引擎本身没坏，只是这次没算出
%       能用的裕量数据"，交由下游 eval.margin.extract_margin() 判定
%       valid=False/invalid_reason='extraction_failed'（design.md 表格
%       该行 + §8.5 算法 IF fr.status ≠ 'ok' 分支的镜像：fr.status 在
%       本文件里恒为 'ok' 或 'engine_transient' 两值之一，'ok' 但
%       freq_response_path 为空时，下游按"未产出可用频响"处理）。
%   落到代码结构上：apply_params/场景注入/linearize-allmargin/frestimate-
%   保存 MAT 全部包在同一个 try 块内，只有一个 catch；catch 内先剥离
%   ContractError（原样上抛，与 simulate_once.m 一致），再用引擎瞬态
%   启发式二分：命中则 status='engine_transient'，不命中则 status 保持
%   'ok'、freq_response_path 保持空——这正是任务描述要求的"把 Engine 级
%   异常与 linearize/frestimate 自身的失败分开处理"，不需要为此额外嵌套
%   第二层 try/catch。
%
% -----------------------------------------------------------------------
% 引擎瞬态异常识别（与 map_error.m 的 local_is_engine_transient 同一组
% 启发式，此处以最小形式重复而非提取公共私有函数）：
%   map_error.m 是任务 2.3 已落地、已过评审的文件；为本文件复用其中一个
%   判据分支而回头改写 map_error.m（哪怕只是把该分支提取成
%   matlab/+pa/private/ 下的共享辅助函数），属于把"新任务的副作用"加到
%   一个已落地文件头上，与 design.md §0.5 判据 2「能删就删，不能删才加」
%   及 Surgical Changes 原则冲突——两处调用点都很小、都是叶子级字符串
%   匹配逻辑，共享一个私有模块换来的收益（避免几行重复）不足以抵消
%   "触碰已评审文件"的成本。因此这里以最小形式原样复制该识别逻辑，两处
%   如需调整（例如任务 2.5 MATLAB Contract 测试层积累真实异常样本后）
%   需同步修改，此处显式记录该重复点供后续核对。
%
% -----------------------------------------------------------------------
% frestimate 的频率范围为占位默认值（design.md 未给出具体规格，本文件的
% 显式选择，标注为需真实模型/MATLAB 环境标定的项）：
%   `freq_response_estimator_on_switching` 分支需要一个频率向量；design.md
%   只说明"开关模型上 frestimate"，未给出输入/输出规格或频率范围。本文件
%   取 10 Hz ~ 1 MHz、200 点对数等分（覆盖典型 Buck 电流/电压环路带宽的
%   合理量级）作为占位默认值，标注为占位、需在真实模型可用后按实测环路
%   带宽重新标定（与 P-1/P-2 探针"需校准"类占位的处理方式一致）。
%
% -----------------------------------------------------------------------
% 为什么必须 UseExactDelayModel='on'（R2024a 实测，非风格选择）
%
%   平均模型里电流内环的采样保持用 Transport Delay 块表达（Td = 0.5/(N*fsw)）。
%   linearize 的**默认**行为是把 Transport Delay 按 Pade 近似处理，而 Pade 阶数取自
%   块的 PadeOrder 参数、默认为 0——即延迟被整个丢弃。实测同一个开环链：
%
%     UseExactDelayModel='off'（默认）: PM=80.37 deg, GM(linear)=0,  hasdelay=false
%     UseExactDelayModel='on'         : PM=73.0223 deg,              hasdelay=true
%                                        GM(linear)=[0 2.87938562613188 2.891 2.892 2.892]
%
%   Python 侧参考值（sim/backends/margin.py 的 margins_from_state_space，同一工作点
%   与同一组参数）是 PM=73.022 deg、GM(linear)=2.879385——与 'on' 的结果逐位一致，
%   与 'off' 的结果差 7 度且增益裕量完全错误。
%
%   为什么不能"将就"用 Pade：sim/backends/margin.py 的模块 docstring 已经论证过
%   同一件事的另一面——频域必须用**精确**纯延迟，否则相位下界只到 -180° 的渐近线、
%   永不穿越，allmargin 得不到有限的增益裕量，下游 eval/margin.py 会把整条裕量
%   （相位裕量与增益裕量在那里同步成败）判为 extraction_failed。时域求解可以用一阶
%   滞后近似（Simulink 原生支持带延迟的求解，这一点比 scipy 宽松），但频域不行。
%
% -----------------------------------------------------------------------
% 落盘只存 margin_data，且显式 '-v7'
%
%   本文件早先版本是 save(freq_response_path, 'margin_data', 'linsys')。'linsys' 是
%   MATLAB 的 ss 类对象，scipy.io.loadmat 读取类对象的行为不可靠；而下游
%   eval/margin.py 的 _extract_from_averaged_linearization() **只读 margin_data**，
%   从不碰 linsys。存一个读不了又没人用的变量，只会让整个文件的加载多一条失败路径。
%
%   '-v7' 必须显式指定：MATLAB 的 save 默认版本取自用户偏好设置，可能是 '-v7.3'
%   （HDF5），而 scipy.io.loadmat **不支持** v7.3，会直接抛异常。同一个理由见
%   private/collect_signals.m 的文件头。
%
% -----------------------------------------------------------------------
% 线性化/频响估计的输入输出点（analysis points）来源（design.md 未规定，
% 本文件的显式选择）：
%   本文件不从 io_contract 读取 Block Path 来构造线性分析的输入/输出点
%   （io_contract.injectable_params 只登记设计变量的 Block Path，
%   output_signals 只登记 logsout 信号名，两者都不是线性分析点）。本文件
%   假定目标模型已在模型内标注好开环输入/输出分析点。本项目由
%   matlab/build_slx_model.m 在生成模型时用 linio/setlinio 写入：verr 处
%   'openinput'（注入并断环）、vout 处 'output'，与 sim/backends/margin.py
%   的开环定义（"在误差信号处断环，输入 u = verr，输出 vout"）一致。
%   两个分支都显式 `getlinio(model_name)` 取回那组分析点后传入，不依赖
%   `linearize(model_name)` 的隐式行为——一旦要传 linearizeOptions，两参数
%   形式 `linearize(model, options)` 就不被支持（见上一节的实测记录）。
%   若模型未标注任何分析点，两者都会抛出异常，落入上方
%   "status 仍为 'ok'，freq_response_path 为空"的分支。

engine_starts = 0;
freq_response_path = '';
status = 'ok';
reason = '';

cfg = jsondecode(model_cfg_json);
params = jsondecode(params_json);
scenario = jsondecode(scenario_json);
margin_cfg = jsondecode(margin_cfg_json);

primary_method = margin_cfg.primary_method;

model_path = cfg.model_path;
model_name = local_model_name(model_path);

try
    if ~bdIsLoaded(model_name)
        load_system(model_path);
    end

    % ---- 设计变量注入：唯一委托给 apply_params.m（与 simulate_once.m 一致） ----
    if isfield(cfg, 'io_contract') && isfield(cfg.io_contract, 'injectable_params')
        whitelist = cfg.io_contract.injectable_params;
    else
        whitelist = struct();
    end
    apply_params(model_name, whitelist, params);

    % ---- 场景条件注入：唯一委托给 private/assign_scenario.m（与 simulate_once.m
    %      同一实现；此处曾是一份独立副本，其白名单漂移导致过整条裕量链路静默
    %      失败，详见 assign_scenario.m 的文件头） ----
    assign_scenario(model_name, scenario);

    switch primary_method
        case 'linear_analysis_on_averaged'
            engine_starts = engine_starts + 1;
            % UseExactDelayModel='on' 是必需的，不是可选优化：见文件头
            % "为什么必须 UseExactDelayModel" 一节的实测记录。
            lin_opt = linearizeOptions('UseExactDelayModel', 'on');
            % io 必须显式传入：linearize(model, options) 这个两参数形式**不被支持**
            % （R2024a 实测报 "下标为 1 的索引超出范围"，linearize.m:355——它把第二个
            % 实参当成了 io/op 而不是 options）。只有 linearize(model, io, options)
            % 三参数形式有效。getlinio 取回的正是模型内已标注的分析点，与不带
            % options 时 linearize(model) 隐式使用的是同一组。
            io = getlinio(model_name);
            linsys = linearize(model_name, io, lin_opt);
            margin_data = allmargin(linsys); %#ok<NASGU>
            freq_response_path = local_resolve_freq_response_path(cfg, scenario);
            % 只存 margin_data，且显式 '-v7'：见文件头"落盘只存 margin_data"一节。
            save(freq_response_path, 'margin_data', '-v7');

        case 'freq_response_estimator_on_switching'
            engine_starts = engine_starts + 1;
            io = getlinio(model_name);
            freq_hz = logspace(1, 6, 200);   % 占位默认值，见上方说明，待真实模型标定
            freq_rad_s = 2 * pi * freq_hz;   % frestimate 默认按 rad/s 解释频率向量
            frd_data = frestimate(model_name, io, freq_rad_s); %#ok<NASGU>
            freq_response_path = local_resolve_freq_response_path(cfg, scenario);
            save(freq_response_path, 'frd_data', '-v7');

        otherwise
            % margin_cfg.primary_method 的取值域由 config.schema（Python 侧）
            % 强制为二值枚举；出现枚举外取值属系统性契约违反，与
            % inspect_model.m / apply_params.m 的 ContractError 约定一致。
            error('pa:ContractError', ...
                'margin_cfg.primary_method 取值超出枚举 {linear_analysis_on_averaged, freq_response_estimator_on_switching}: %s', ...
                primary_method);
    end

catch err
    if strcmp(err.identifier, 'pa:ContractError')
        rethrow(err);
    end
    if local_is_engine_transient(err)
        status = 'engine_transient';
    end
    % 非引擎瞬态异常（含 linearize/allmargin/frestimate 因数值奇异、模型未
    % 标注分析点等原因抛出的异常）：status 保持 'ok'，freq_response_path
    % 保持已初始化的空字符串（见上方"为什么本文件不复用 map_error.m"说明）。
    freq_response_path = '';
    % 把异常原因带出来（见下方 res.reason 的说明）。
    reason = local_error_summary(err);
end

res = struct();
res.status = status;
res.freq_response_path = freq_response_path;
res.method = primary_method;
res.engine_starts = engine_starts;
% reason：采集未产出可用频响时的原因文本，成功时为空字符串。
%
% 加这个字段的理由：本文件对「引擎没坏，只是这次没算出能用的裕量数据」的处理是
% status 保持 'ok'、freq_response_path 置空（design.md §11.3 错误映射表的那一行）。
% 那个设计是对的——下游 eval.margin.extract_margin() 据此判 extraction_failed——
% 但它此前**不留任何痕迹**：调用方只看到一个空路径，无从知道是模型没标分析点、
% linearize 数值奇异，还是落盘失败。实测踩过一次：linearize 的两参数形式不被支持
% 而报「下标为 1 的索引超出范围」，返回值里看不出任何线索，只能靠在 MATLAB 里
% 逐步手工复现才定位到。
%
% Python 后端的 PythonSession._run_linear_analysis() 早已在同一情形下返回
% "reason" 字段，本字段与它对齐而非新发明。sim/simulate.py 不读它（那里只需要
% freq_response_path 是否为空），它的用途是诊断与日志。
res.reason = reason;

end


function s = local_error_summary(err)
% 把 MException 压成一行可读文本：标识符 + 首行消息 + 最内层的文件:行号。
% 不做完整堆栈——这个字段是给「为什么没算出裕量」一个可查的线索，不是替代调试器。
id = local_safe_field(err, 'identifier');
msg = local_safe_field(err, 'message');
msg = regexprep(msg, '\s+', ' ');
if numel(msg) > 300
    msg = [msg(1:300) '...'];
end
where = '';
try
    if ~isempty(err.stack)
        where = sprintf(' at %s:%d', err.stack(1).name, err.stack(1).line);
    end
catch
    % err 可能是不带 stack 的等价 struct；留空。
end
s = sprintf('%s: %s%s', id, msg, where);
end

function name = local_model_name(model_path)
[~, name] = fileparts(model_path);
end

function p = local_resolve_freq_response_path(cfg, scenario)
% 落盘路径解析方式镜像 simulate_once.m 的 local_resolve_waveform_path：
% run_id 非空时取其值命名，否则取时间戳+随机数后缀；输出目录取
% cfg.output_dir，否则取 tempdir 下的固定子目录（此处用
% 'poweragent_freq_response' 与波形落盘目录区分，避免文件名一次性冲突
% 之外的语义混淆）。实际的原子落盘、跨机器 sha256 校验与最终归档目录，
% 与 simulate_once.m 的波形路径一样，由 Python 侧
% store.artifacts.ArtifactStore 负责（design.md §6.9），本文件返回的
% freq_response_path 只是一个"本次调用产出的临时/工作路径"引用。
if isfield(scenario, 'run_id') && ~isempty(scenario.run_id)
    stem = char(scenario.run_id);
else
    stem = sprintf('run_%s_%06d', datestr(now, 'yyyymmddHHMMSSFFF'), randi(999999));
end

if isfield(cfg, 'output_dir') && ~isempty(cfg.output_dir)
    out_dir = cfg.output_dir;
else
    out_dir = fullfile(tempdir, 'poweragent_freq_response');
end
if ~exist(out_dir, 'dir')
    mkdir(out_dir);
end
p = fullfile(out_dir, [stem '_freq_response.mat']);
end

function tf = local_is_engine_transient(err)
% 与 map_error.m 的 local_is_engine_transient 同一组最佳努力启发式的最小
% 复制（见文件头"引擎瞬态异常识别"说明）。待 MATLAB Contract 测试层
% （任务 2.5 同类）用真实异常样本核对/调整；两处需同步修改。
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

function v = local_safe_field(err, field_name)
% err 为 MException 或等价 struct；字段缺失时返回空字符串（与 map_error.m
% 的同名 local 函数逐字符一致，见上方复制理由）。
%
% 不用 isfield()：它只对 struct 有效，对 MException **对象**恒返回 false，
% 会使本函数对真实异常永远返回空字符串、连带让 local_is_engine_transient()
% 的全部判据恒为假。完整说明见 private/map_error.m 的同名函数——两处已同步修改。
v = '';
try
    raw = err.(field_name);
    if ~isempty(raw)
        v = char(raw);
    end
catch
    v = '';
end
end
