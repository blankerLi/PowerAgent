function assign_scenario(model_name, scenario)
% 把场景条件写入模型工作区；全系统唯一的场景注入实现
%
%   ASSIGN_SCENARIO(MODEL_NAME, SCENARIO)
%
%   MODEL_NAME - 已加载模型的名称（char/string）。
%   SCENARIO   - jsondecode 后的 scenario struct。只有出现在下方白名单里、且在
%                该 struct 中存在的字段会被写入；其余字段忽略，缺失字段不写入。
%
%   模型内部的 Vin 源、负载阶跃块与温度相关表达式约定通过读取这些工作区变量获得
%   场景条件（build_slx_model.m 生成的模型里，这些变量出现在 Constant/Bias/Gain/
%   Saturation/Integrator 的参数表达式中）。
%
%   rcomp/ccomp 两个设计变量**不**走这条路径：它们由 apply_params.m 注入
%   （design.md 任务 2.2 明确其为"全系统唯一参数注入实现"）。两者的区别是
%   设计变量由搜索决定、场景条件由 task.yaml 决定。
%
% -----------------------------------------------------------------------
% 为什么提取成共享私有函数（推翻先前"刻意复制"的决定，有实测代价）
%
%   这段逻辑此前在 simulate_once.m 与 run_linear_analysis.m 各有一份副本，
%   后者的文件头把复制理由写成"两处调用点都很小、都是叶子级逻辑，共享一个私有
%   模块换来的收益不足以抵消触碰已评审文件的成本"，并留了一句"两处如需调整需
%   同步修改"。
%
%   那句同步要求实测没有被满足，代价是整条裕量链路静默失败：新增
%   step_trigger_s 字段时只改了 simulate_once.m 的白名单，run_linear_analysis.m
%   的副本仍是五字段。于是评价场景（时域跑 switching、裕量跑 averaged）下，
%   averaged 模型第一次被 run_linear_analysis.m 加载时工作区里没有
%   step_trigger_s，负载阶跃块的 Bias='-step_trigger_s' 无法求值，linearize
%   抛 Simulink:Parameters:InvParamSetting，而该异常被归类为"引擎没坏、只是这次
%   没算出裕量"——status 仍为 'ok'、freq_response_path 为空，下游把每个候选都
%   记为 extraction_failed。
%
%   这个失败模式的特征是**只在两个变体分别承担时域与频域时才出现**：如果时域和
%   频域用同一个变体，simulate_once.m 会先把变量写好，run_linear_analysis.m
%   的白名单缺项就被掩盖。也就是说"两处副本保持同步"这件事没有任何机制保证，
%   而不同步的后果不是报错而是一个看起来正常的空结果。
%
%   白名单必须只有一处。这不是消除重复的洁癖，是消除一个已经发生过的、
%   无法被测试轻易覆盖的静默失败。

scenario_fields = { ...
    'vin_v', ...            % 输入电压（V）
    'temp_c', ...           % 环境温度（degC），进入 Ri/R_loop 的温度修正表达式
    'load_start_a', ...     % 阶跃前负载电流（A），同时是电感电流与补偿积分状态的初值
    'load_end_a', ...       % 阶跃后负载电流（A）
    'slew_a_per_us', ...    % 负载电流变化率（A/us）
    'step_trigger_s'};      % 负载阶跃触发时刻（s），由 Python 侧按
                            %   STEP_TRIGGER_FRACTION * stop_time 算出后经
                            %   scenario_json 传入（sim/simulate.py 的
                            %   _build_scenario_payload）。两个后端共用同一个值，
                            %   否则两份波形在时间轴上对不齐、
                            %   settling_time/overshoot/undershoot 三个指标的窗口
                            %   [step_trigger, step_trigger_plus_500us] 比较的
                            %   就不是同一段瞬态。

mws = get_param(model_name, 'ModelWorkspace');
for i = 1:numel(scenario_fields)
    f = scenario_fields{i};
    if isfield(scenario, f)
        assignin(mws, f, double(scenario.(f)));
    end
end

end
