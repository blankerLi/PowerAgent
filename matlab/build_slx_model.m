function build_slx_model(spec_json)
% 从电路参数规格以编程方式生成一个 Simulink 模型（.slx）
%
%   BUILD_SLX_MODEL(SPEC_JSON)
%
%   SPEC_JSON 为 JSON 字符串，由 scripts/build_slx_models.py 从
%   models/buck4ph_*.yaml（唯一参数源）与 configs/model.yaml 组装后传入。字段见
%   local_build_averaged / local_build_switching 的取用处；关键项：
%     model_name / model_variant / out_path
%     n_phase / fsw_hz / l_per_phase_h / cout_f / cout_esr_ohm
%     dcr_per_phase_ohm / rds_on_ohm / vout_nom_v
%     gm_s / ri_ohm / i_limit_a
%     rcomp_default / ccomp_default
%     solver.{max_step, rel_tol, stop_time} / output_dt_s
%     signal_names.{vout, iout, iphase}
%
% =======================================================================
% 为什么用脚本生成而不是手工搭图
%
%   .slx 是二进制文件：不可 diff、不可在代码评审里逐行看、改了什么只能靠人复述。
%   而这个模型是「被仿真的对象」，它的每一处参数与连线都是结论成立的前提。用
%   add_block/add_line/set_param 以编程方式生成后，进版本控制的是这份文本脚本，
%   .slx 退化为可重新生成的派生产物——参数从哪来、为什么这样连、求解器为什么这样配，
%   全部可审查。
%
%   .slx 仍然独立计入 model_package_hash（model.yaml 的 slx_entry 字段，见
%   config/schema.py 的 ModelVariantPackage docstring）：「由脚本生成」是当前的
%   工作方式，不是文件系统强制的约束，有人手工改了 .slx 而不改脚本，哈希必须变。
%
% =======================================================================
% 与 Python 后端的关系：同一组方程，不是另一个电路
%
%   两个后端必须描述同一个电路，否则双后端一致性核对无从归因。本文件搭出的方程
%   逐项对应 sim/backends/buck_model.py 的模块 docstring 与
%   sim/backends/averaged.py / switching.py 的实现：
%
%     iload(t) = load_start + sign(dI)*min(slew*1e6*(t - t_step), |dI|)
%     vout  = vc + ESR*(iL - iload)
%     verr  = vref - vout
%     vcomp = xc + Rcomp*gm*verr
%     iL_cmd = clip(vcomp/Ri, 0, i_limit)
%     d_xc  = gm*verr/Ccomp            （抗积分饱和：触限且误差同向时冻结）
%     d_vc  = (iL - iload)/Cout
%     Ri(T) = ri_nom*(1 + 0.00393*(T - 25))
%
%   温度修正在模型内完成（Ri 的 Constant 块值是引用模型工作区变量 temp_c 的表达式），
%   而不是由 Python 侧算好后注入：注入白名单只有 rcomp/ccomp 两项（design.md §4.1
%   的设计变量集合），Ri 不是设计变量，把它变成第三个注入点会扩大那个白名单的语义。
%
% =======================================================================
% 一处刻意的差异：平均模型的采样保持用 Transport Delay（纯延迟）
%
%   Python 平均模型的时域求解把电流内环的采样保持近似为一阶滞后 1/(1+s*Td)，
%   理由写在 sim/backends/margin.py 里：纯延迟会把 ODE 变成延迟微分方程，scipy 侧
%   需要维护历史缓存、求解器与可复现性都要重做。而它的**频域**裕量提取用的是精确
%   纯延迟 exp(-s*Td)——否则相位下界只到 -180° 渐近线、永不穿越，增益裕量在数学上
%   无界，下游会判为 extraction_failed。Python 后端因此存在一处时域/频域分裂。
%
%   Simulink 原生支持带延迟的连续求解，这个约束不存在。本模型在时域路径上直接用
%   Transport Delay，于是同一个模型的时域与频域用的是同一个延迟表达，不需要分裂。
%   代价是它与 Python 平均模型的时域结果有一处系统性差异（一阶滞后 vs 真延迟），
%   这个差异是已知且可量化的，由双后端一致性核对负责测量。
%
%   频域侧必须配合 linearizeOptions('UseExactDelayModel','on')，否则 linearize 会
%   按 Pade 近似处理 Transport Delay（默认 PadeOrder=0，即整个丢弃）——见
%   matlab/+pa/run_linear_analysis.m 文件头的实测记录。
%
% =======================================================================
% 求解器按变体分别配置，不沿用 model.yaml 的单一 solver 节
%
%   model.yaml 的 io_contract.solver 只有一组值（type=ode23tb, max_step=2e-8,
%   rel_tol=1e-4, stop_time=1e-3），而两个变体对求解器的要求本就不同——Python 后端
%   同样如此，且它的做法就写在代码里：
%     - averaged：sim/backends/session.py 只把 stop_time 与 rel_tol 传给
%       simulate_averaged()，**不传 max_step**；averaged.py 内部把步长上限锁到输出
%       网格间距 output_dt_s（0.2 µs）。本文件对应地设 MaxStep=output_dt_s，
%       而不是 model.yaml 的 2e-8——后者会让 1 ms 仿真跑 5×10^4 步，且没有收益：
%       平均模型按定义不含开关纹波，没有需要 20 ns 才能分辨的动态。
%     - switching：switching.py 用固定步长显式欧拉，步长取 model.yaml 的
%       max_step（20 ns）。本文件对应地设 Solver='ode1'、FixedStep=max_step。
%       用变步长隐式求解器跑开关模型会在每个开关点反复缩步重试，比固定小步长更慢
%       也更不可靠（switching.py 的模块 docstring 记录了同一判断）。
%
%   io_contract.solver.type 因此描述的是**平均模型**的求解器；这一点在
%   model.yaml 的注释里同步说明。

spec = jsondecode(spec_json);

switch spec.model_variant
    case 'averaged'
        local_build_averaged(spec);
    case 'switching'
        local_build_switching(spec);
    otherwise
        error('pa:BuildError', ...
            'model_variant 取值超出 {averaged, switching}: %s', spec.model_variant);
end

end


% =======================================================================
% 平均模型
% =======================================================================
function local_build_averaged(spec)

m = spec.model_name;
local_new(m);

% ---- 数值/表达式片段（引用模型工作区变量的表达式保持为文本，运行期才求值） ----
ri_expr = sprintf('%s*(1 + 0.00393*(temp_c - 25))', local_num(spec.ri_ohm));
xc0_expr = sprintf('load_start_a*(%s)', ri_expr);

% ---- 负载电流：iload = load_start + sign(dI)*min(slew*1e6*(t-t_step), |dI|) ----
% Saturation 的下限 0 同时承担「t < t_step 时 ramp 为负、应取 0」这一段，
% 不需要额外的 max(t-t_step, 0)。
local_add(m, 'simulink/Sources/Clock', 'clk');
local_add(m, 'simulink/Math Operations/Bias', 'shift_t', ...
    'Bias', '-step_trigger_s');
local_add(m, 'simulink/Math Operations/Gain', 'slew', ...
    'Gain', 'slew_a_per_us*1e6');
local_add(m, 'simulink/Discontinuities/Saturation', 'clip_ramp', ...
    'UpperLimit', 'abs(load_end_a - load_start_a)', 'LowerLimit', '0');
local_add(m, 'simulink/Math Operations/Gain', 'ramp_sign', ...
    'Gain', 'sign(load_end_a - load_start_a)');
local_add(m, 'simulink/Math Operations/Bias', 'iload', ...
    'Bias', 'load_start_a');

% ---- 电压误差与 Type-II 补偿器 ----
local_add(m, 'simulink/Sources/Constant', 'vref', ...
    'Value', local_num(spec.vout_nom_v));
local_add(m, 'simulink/Math Operations/Sum', 'sum_err', 'Inputs', '+-');
local_add(m, 'simulink/Math Operations/Gain', 'gm', ...
    'Gain', local_num(spec.gm_s));

% 两个设计变量的注入点（见 local_build_compensator 的说明）。
local_build_compensator(m, spec);

local_add(m, 'simulink/Math Operations/Product', 'mul_rcomp', 'Inputs', '2');
local_add(m, 'simulink/Math Operations/Divide', 'div_ccomp', 'Inputs', '*/');
local_add(m, 'simulink/Continuous/Integrator', 'int_xc', ...
    'InitialCondition', xc0_expr);
local_add(m, 'simulink/Math Operations/Sum', 'sum_comp', 'Inputs', '++');

% ---- 电流指令：iL_cmd = clip(vcomp/Ri, 0, i_limit) ----
local_add(m, 'simulink/Sources/Constant', 'Ri', 'Value', ri_expr);
local_add(m, 'simulink/Math Operations/Divide', 'div_ri', 'Inputs', '*/');
local_add(m, 'simulink/Discontinuities/Saturation', 'clip_cmd', ...
    'UpperLimit', local_num(spec.i_limit_a), 'LowerLimit', '0');

% ---- 抗积分饱和：指令已触限且误差仍在往该方向推时冻结积分 ----
% 判断「是否触限」必须用钳位**前**的原始值：钳位后的值已经丢失这个信息
% （buck_model.py 的 current_command() 出于同一理由一次算出并返回两个标志）。
local_add(m, 'simulink/Sources/Constant', 'i_limit', ...
    'Value', local_num(spec.i_limit_a));
local_add(m, 'simulink/Sources/Constant', 'zero', 'Value', '0');
local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', ...
    'cmp_hi', 'Operator', '>');
local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', ...
    'cmp_lo', 'Operator', '<');
local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', ...
    'cmp_err_pos', 'Operator', '>');
local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', ...
    'cmp_err_neg', 'Operator', '<');
local_add(m, 'simulink/Logic and Bit Operations/Logical Operator', ...
    'and_hi', 'Operator', 'AND', 'Inputs', '2');
local_add(m, 'simulink/Logic and Bit Operations/Logical Operator', ...
    'and_lo', 'Operator', 'AND', 'Inputs', '2');
local_add(m, 'simulink/Logic and Bit Operations/Logical Operator', ...
    'or_freeze', 'Operator', 'OR', 'Inputs', '2');
local_add(m, 'simulink/Signal Routing/Switch', 'freeze', ...
    'Criteria', 'u2 > Threshold', 'Threshold', '0.5');

% ---- 采样保持（纯延迟）与功率级 ----
% InitialOutput 取 load_start_a：稳态电感电流等于起始负载电流，使 t=0 的状态
% 就是精确稳态（buck_model.py 的 steady_state() 给出同一组初值），不浪费仿真
% 时间去建立稳态，也使每次仿真的初始条件严格相同。
local_add(m, 'simulink/Continuous/Transport Delay', 'delay_td', ...
    'DelayTime', local_num(0.5 / (spec.n_phase * spec.fsw_hz)), ...
    'InitialOutput', 'load_start_a');
local_add(m, 'simulink/Math Operations/Sum', 'sum_cap', 'Inputs', '+-');
local_add(m, 'simulink/Math Operations/Gain', 'inv_cout', ...
    'Gain', local_num(1.0 / spec.cout_f));
local_add(m, 'simulink/Continuous/Integrator', 'int_vc', ...
    'InitialCondition', local_num(spec.vout_nom_v));
local_add(m, 'simulink/Math Operations/Gain', 'esr', ...
    'Gain', local_num(spec.cout_esr_ohm));
local_add(m, 'simulink/Math Operations/Sum', 'sum_vout', 'Inputs', '++');

% ---- 相电流：平均模型下各相均流，把总电流均分为 n_phase 通道的向量 ----
% 形状与开关模型一致（eval/metrics.py 的 phase_peak_current 按 (n_time, n_phase)
% 同时做跨相与跨时间聚合），只是各通道相同。
local_add(m, 'simulink/Math Operations/Gain', 'iphase', ...
    'Gain', sprintf('ones(%d,1)/%d', spec.n_phase, spec.n_phase), ...
    'Multiplication', 'Matrix(K*u)');

% ---- 连线 ----
local_connect(m, {
    'clk/1',            'shift_t/1'
    'shift_t/1',        'slew/1'
    'slew/1',           'clip_ramp/1'
    'clip_ramp/1',      'ramp_sign/1'
    'ramp_sign/1',      'iload/1'

    'vref/1',           'sum_err/1'
    'sum_vout/1',       'sum_err/2'
    'sum_err/1',        'gm/1'

    'gm/1',             'mul_rcomp/1'
    'compensator/1',    'mul_rcomp/2'
    'gm/1',             'div_ccomp/1'
    'compensator/2',    'div_ccomp/2'
    'div_ccomp/1',      'freeze/3'
    'zero/1',           'freeze/1'
    'or_freeze/1',      'freeze/2'
    'freeze/1',         'int_xc/1'

    'int_xc/1',         'sum_comp/1'
    'mul_rcomp/1',      'sum_comp/2'
    'sum_comp/1',       'div_ri/1'
    'Ri/1',             'div_ri/2'
    'div_ri/1',         'clip_cmd/1'

    'div_ri/1',         'cmp_hi/1'
    'i_limit/1',        'cmp_hi/2'
    'div_ri/1',         'cmp_lo/1'
    'zero/1',           'cmp_lo/2'
    'sum_err/1',        'cmp_err_pos/1'
    'zero/1',           'cmp_err_pos/2'
    'sum_err/1',        'cmp_err_neg/1'
    'zero/1',           'cmp_err_neg/2'
    'cmp_hi/1',         'and_hi/1'
    'cmp_err_pos/1',    'and_hi/2'
    'cmp_lo/1',         'and_lo/1'
    'cmp_err_neg/1',    'and_lo/2'
    'and_hi/1',         'or_freeze/1'
    'and_lo/1',         'or_freeze/2'

    'clip_cmd/1',       'delay_td/1'
    'delay_td/1',       'sum_cap/1'
    'iload/1',          'sum_cap/2'
    'sum_cap/1',        'inv_cout/1'
    'inv_cout/1',       'int_vc/1'
    'sum_cap/1',        'esr/1'
    'esr/1',            'sum_vout/1'
    'int_vc/1',         'sum_vout/2'

    'delay_td/1',       'iphase/1'
});

% ---- 求解器（按变体配置，理由见文件头） ----
local_set_solver_averaged(m, spec);

% ---- 信号记录 ----
% ESR 上的压降用 (iL - iload)（即 sum_cap 的输出）而不是单独再算一次：
% vout = vc + ESR*(iL - iload)，与 averaged.py 的 vout 表达式逐项一致。
local_log_signal(m, 'sum_vout', 1, spec.signal_names.vout);
local_log_signal(m, 'iload', 1, spec.signal_names.iout);
local_log_signal(m, 'iphase', 1, spec.signal_names.iphase);

% ---- 线性分析点：在 verr 处断环，输入 verr、输出 vout ----
% 与 sim/backends/margin.py 的开环定义严格一致（「在误差信号处断环，
% 输入 u = verr，输出 vout」）。'openinput' 同时做两件事：在该点注入输入、
% 断开该点原有的下游连接，因此反馈不再闭合，linearize 得到的是开环传函 T(s)
% 而不是闭环响应。若只标 'input'（不断环），拿到的是闭环传函，allmargin 的
% 结果没有环路裕量的意义。
io = linio([m '/sum_err'], 1, 'openinput');
io(2) = linio([m '/sum_vout'], 1, 'output');
setlinio(m, io);

local_save(m, spec.out_path);

end


function local_set_solver_averaged(m, spec)
% MaxStep 取 output_dt_s（0.2 µs）而不是 model.yaml 的 max_step（20 ns）：
% 与 Python 平均模型一致，理由见文件头「求解器按变体分别配置」。
set_param(m, ...
    'SolverType', 'Variable-step', ...
    'Solver', 'ode23tb', ...
    'RelTol', local_num(spec.solver.rel_tol), ...
    'MaxStep', local_num(spec.output_dt_s), ...
    'StopTime', local_num(spec.solver.stop_time), ...
    'SignalLogging', 'on', ...
    'SignalLoggingName', 'logsout', ...
    'SignalLoggingSaveFormat', 'Dataset');
end


% =======================================================================
% 开关模型
% =======================================================================
function local_build_switching(spec)
% 与平均模型共享电压外环（补偿器 + 抗积分饱和 + 电流指令限幅）与输出电容，
% 区别在于把「采样保持的一阶滞后/纯延迟」替换为**显式的逐相 PWM**：
%
%   载波相位   frac_k   = mod(t/Tsw - (k-1)/N, 1)          （k = 1..N）
%   本周期已导通 on_time_k = frac_k * Tsw
%   周期起点   set_k    = on_time_k < dt                    （刚进入新周期的那一步）
%   指令采样   held_k   = set_k ? iL_cmd/N : held_k(上一步)  （周期起点采样并保持）
%   关断条件   reset_k  = (i_k >= held_k && on_time_k >= t_on_min) || on_time_k >= t_on_max
%   开关状态   on_k     = set_k ? 1 : (reset_k ? 0 : on_k(上一步))
%   电感       di_k/dt  = (on_k*vin - vout - i_k*R_loop(T)) / L
%   输出电容   dvc/dt   = (sum(i_k) - iload) / Cout
%   输出电压   vout     = vc + ESR*(sum(i_k) - iload)
%
% 逐项对应 sim/backends/switching.py 的实现。
%
% ## on_time 由载波相位直接算出，不另设计时器
%
%   switching.py 用一个 on_since 数组记录每相本周期的开通时刻，再算
%   on_time = t - on_since。这里不需要：周期起点就是开通时刻，因此
%   on_time 恒等于 frac_k * Tsw。少一个需要采样保持的状态量，也少一处
%   「采样时刻与实际开通时刻可能错开一步」的出错方式。
%
% ## SR latch 用 Memory + Switch，不用 Simulink Extras 的 S-R Flip-Flop
%
%   Memory 块在固定步长下的语义就是「上一个求解步的值」，正好对应
%   switching.py 里 switch_on / i_cmd_held 两个跨步保持的变量。用它搭出的
%   latch 只有基本 Simulink 块，且 Memory 天然打断了
%   on -> reset -> on 与 held -> reached -> held 两条反馈路径上的代数环。
%   Simulink Extras 的 S-R Flip-Flop 是掩码子系统，内部同样是这套结构，
%   但它引入一个额外的库依赖，而这里只需要三个块。
%
% ## set 优先于 reset（与 switching.py 在实际路径上等价）
%
%   switching.py 的更新顺序是先 `switch_on |= new_cycle` 再
%   `switch_on &= ~(reached | hit_duty_limit)`，即 reset 优先。本模型写成
%   `on = set ? 1 : (reset ? 0 : on_prev)`，即 set 优先。两者只在「周期起点
%   那一步同时满足关断条件」时不同，而那一步 on_time ≈ 0，既不满足
%   `on_time >= t_on_min` 也不满足 `on_time >= t_on_max`，reset 恒为假——
%   因此在任何可达状态上两种写法给出相同结果。
%
% ## 一处与 switching.py 的已知差异：t=0 的相 0
%
%   switching.py 用 `frac < frac_prev` 检测载波回绕，其 frac_prev 初值取
%   `mod(-phase_offset, 1)`，于是第一步所有相都判为「非新周期」，相 0 要等到
%   第二个周期起点才开通。本模型用 `on_time < dt`，相 0 在 t=0 即判为周期起点
%   并开通。差异是一个开关周期内的初始相位，而 switching.py 的模块 docstring
%   已记录初始瞬态需要若干开关周期衰减、且 stop_time=1 ms（阶跃触发前约 50 个
%   开关周期）下残余影响低于 0.5%。

m = spec.model_name;
n = spec.n_phase;
tsw = 1.0 / spec.fsw_hz;
dt = spec.solver.max_step;

local_new(m);

ri_expr = sprintf('%s*(1 + 0.00393*(temp_c - 25))', local_num(spec.ri_ohm));
xc0_expr = sprintf('load_start_a*(%s)', ri_expr);
% 每相回路等效串联电阻：DCR 与导通电阻各按自己的温度系数修正后相加
% （buck_model.py 的 conduction_resistance_ohm；用同一个系数修正两者会低估高温阻抗）。
rloop_expr = sprintf('%s*(1 + 0.00393*(temp_c - 25)) + %s*(1 + 0.005*(temp_c - 25))', ...
    local_num(spec.dcr_per_phase_ohm), local_num(spec.rds_on_ohm));

% ---- 负载电流（与平均模型逐块相同） ----
local_add(m, 'simulink/Sources/Clock', 'clk');
local_add(m, 'simulink/Math Operations/Bias', 'shift_t', ...
    'Bias', '-step_trigger_s');
local_add(m, 'simulink/Math Operations/Gain', 'slew', ...
    'Gain', 'slew_a_per_us*1e6');
local_add(m, 'simulink/Discontinuities/Saturation', 'clip_ramp', ...
    'UpperLimit', 'abs(load_end_a - load_start_a)', 'LowerLimit', '0');
local_add(m, 'simulink/Math Operations/Gain', 'ramp_sign', ...
    'Gain', 'sign(load_end_a - load_start_a)');
local_add(m, 'simulink/Math Operations/Bias', 'iload', ...
    'Bias', 'load_start_a');

% ---- 电压外环（与平均模型逐块相同） ----
local_add(m, 'simulink/Sources/Constant', 'vref', ...
    'Value', local_num(spec.vout_nom_v));
local_add(m, 'simulink/Math Operations/Sum', 'sum_err', 'Inputs', '+-');
local_add(m, 'simulink/Math Operations/Gain', 'gm', 'Gain', local_num(spec.gm_s));
local_build_compensator(m, spec);
local_add(m, 'simulink/Math Operations/Product', 'mul_rcomp', 'Inputs', '2');
local_add(m, 'simulink/Math Operations/Divide', 'div_ccomp', 'Inputs', '*/');
local_add(m, 'simulink/Continuous/Integrator', 'int_xc', ...
    'InitialCondition', xc0_expr);
local_add(m, 'simulink/Math Operations/Sum', 'sum_comp', 'Inputs', '++');
local_add(m, 'simulink/Sources/Constant', 'Ri', 'Value', ri_expr);
local_add(m, 'simulink/Math Operations/Divide', 'div_ri', 'Inputs', '*/');
local_add(m, 'simulink/Discontinuities/Saturation', 'clip_cmd', ...
    'UpperLimit', local_num(spec.i_limit_a), 'LowerLimit', '0');

local_add(m, 'simulink/Sources/Constant', 'i_limit', ...
    'Value', local_num(spec.i_limit_a));
local_add(m, 'simulink/Sources/Constant', 'zero', 'Value', '0');
local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', 'cmp_hi', ...
    'Operator', '>');
local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', 'cmp_lo', ...
    'Operator', '<');
local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', 'cmp_err_pos', ...
    'Operator', '>');
local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', 'cmp_err_neg', ...
    'Operator', '<');
local_add(m, 'simulink/Logic and Bit Operations/Logical Operator', 'and_hi', ...
    'Operator', 'AND', 'Inputs', '2');
local_add(m, 'simulink/Logic and Bit Operations/Logical Operator', 'and_lo', ...
    'Operator', 'AND', 'Inputs', '2');
local_add(m, 'simulink/Logic and Bit Operations/Logical Operator', 'or_freeze', ...
    'Operator', 'OR', 'Inputs', '2');
local_add(m, 'simulink/Signal Routing/Switch', 'freeze', ...
    'Criteria', 'u2 > Threshold', 'Threshold', '0.5');

% ---- PWM 与逐相功率级共享的常量 ----
local_add(m, 'simulink/Sources/Constant', 'vin', 'Value', 'vin_v');
local_add(m, 'simulink/Sources/Constant', 'one', 'Value', '1');
local_add(m, 'simulink/Sources/Constant', 'unity', 'Value', '1');   % mod 的第二输入
local_add(m, 'simulink/Sources/Constant', 'dt_const', 'Value', local_num(dt));
% 两个导通时间阈值都加上 0.5*dt，抵消载波相位的半步偏移（见 local_carrier_bias
% 的说明）：偏移后 on_time 比真实导通时间大 0.5*dt，两个阈值同样加 0.5*dt 之后，
% 比较的仍是真实导通时间。
local_add(m, 'simulink/Sources/Constant', 't_on_min', ...
    'Value', local_num(spec.duty_min * tsw + 0.5 * dt));
local_add(m, 'simulink/Sources/Constant', 't_on_max', ...
    'Value', local_num(spec.duty_max * tsw + 0.5 * dt));
local_add(m, 'simulink/Math Operations/Gain', 'cmd_per_phase', ...
    'Gain', local_num(1.0 / n));
local_add(m, 'simulink/Math Operations/Gain', 't_over_tsw', ...
    'Gain', local_num(1.0 / tsw));

% ---- 输出电容与输出电压 ----
local_add(m, 'simulink/Math Operations/Sum', 'i_total', ...
    'Inputs', repmat('+', 1, n));
local_add(m, 'simulink/Math Operations/Sum', 'sum_cap', 'Inputs', '+-');
local_add(m, 'simulink/Math Operations/Gain', 'inv_cout', ...
    'Gain', local_num(1.0 / spec.cout_f));
local_add(m, 'simulink/Continuous/Integrator', 'int_vc', ...
    'InitialCondition', local_num(spec.vout_nom_v));
local_add(m, 'simulink/Math Operations/Gain', 'esr', ...
    'Gain', local_num(spec.cout_esr_ohm));
local_add(m, 'simulink/Math Operations/Sum', 'sum_vout', 'Inputs', '++');
local_add(m, 'simulink/Signal Routing/Mux', 'iphase', 'Inputs', local_int(n));

% ---- 共享部分的连线 ----
local_connect(m, {
    'clk/1',            'shift_t/1'
    'shift_t/1',        'slew/1'
    'slew/1',           'clip_ramp/1'
    'clip_ramp/1',      'ramp_sign/1'
    'ramp_sign/1',      'iload/1'
    'clk/1',            't_over_tsw/1'

    'vref/1',           'sum_err/1'
    'sum_vout/1',       'sum_err/2'
    'sum_err/1',        'gm/1'

    'gm/1',             'mul_rcomp/1'
    'compensator/1',    'mul_rcomp/2'
    'gm/1',             'div_ccomp/1'
    'compensator/2',    'div_ccomp/2'
    'div_ccomp/1',      'freeze/3'
    'zero/1',           'freeze/1'
    'or_freeze/1',      'freeze/2'
    'freeze/1',         'int_xc/1'

    'int_xc/1',         'sum_comp/1'
    'mul_rcomp/1',      'sum_comp/2'
    'sum_comp/1',       'div_ri/1'
    'Ri/1',             'div_ri/2'
    'div_ri/1',         'clip_cmd/1'

    'div_ri/1',         'cmp_hi/1'
    'i_limit/1',        'cmp_hi/2'
    'div_ri/1',         'cmp_lo/1'
    'zero/1',           'cmp_lo/2'
    'sum_err/1',        'cmp_err_pos/1'
    'zero/1',           'cmp_err_pos/2'
    'sum_err/1',        'cmp_err_neg/1'
    'zero/1',           'cmp_err_neg/2'
    'cmp_hi/1',         'and_hi/1'
    'cmp_err_pos/1',    'and_hi/2'
    'cmp_lo/1',         'and_lo/1'
    'cmp_err_neg/1',    'and_lo/2'
    'and_hi/1',         'or_freeze/1'
    'and_lo/1',         'or_freeze/2'

    'clip_cmd/1',       'cmd_per_phase/1'

    'i_total/1',        'sum_cap/1'
    'iload/1',          'sum_cap/2'
    'sum_cap/1',        'inv_cout/1'
    'inv_cout/1',       'int_vc/1'
    'sum_cap/1',        'esr/1'
    'esr/1',            'sum_vout/1'
    'int_vc/1',         'sum_vout/2'
});

% ---- 逐相 PWM 与电感 ----
for k = 1:n
    sfx = sprintf('_%d', k);
    local_add(m, 'simulink/Math Operations/Bias', ['ph_off' sfx], ...
        'Bias', local_num(local_carrier_bias(k, n, dt, tsw)));
    local_add(m, 'simulink/Math Operations/Math Function', ['frac' sfx], ...
        'Operator', 'mod');
    local_add(m, 'simulink/Math Operations/Gain', ['on_time' sfx], ...
        'Gain', local_num(tsw));
    local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', ...
        ['set' sfx], 'Operator', '<');
    local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', ...
        ['ge_min' sfx], 'Operator', '>=');
    local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', ...
        ['ge_max' sfx], 'Operator', '>=');
    % 指令采样保持：周期起点取当前 iL_cmd/N，其余步保持上一步的值。
    local_add(m, 'simulink/Discrete/Memory', ['mem_held' sfx], ...
        'InitialCondition', sprintf('load_start_a/%d', n));
    local_add(m, 'simulink/Signal Routing/Switch', ['sw_held' sfx], ...
        'Criteria', 'u2 > Threshold', 'Threshold', '0.5');
    local_add(m, 'simulink/Logic and Bit Operations/Relational Operator', ...
        ['cmp_reach' sfx], 'Operator', '>=');
    local_add(m, 'simulink/Logic and Bit Operations/Logical Operator', ...
        ['and_reach' sfx], 'Operator', 'AND', 'Inputs', '2');
    local_add(m, 'simulink/Logic and Bit Operations/Logical Operator', ...
        ['or_reset' sfx], 'Operator', 'OR', 'Inputs', '2');
    % 开关状态 latch：初值 0（初始全部关断，与 switching.py 一致）。
    local_add(m, 'simulink/Discrete/Memory', ['mem_on' sfx], ...
        'InitialCondition', '0');
    local_add(m, 'simulink/Signal Routing/Switch', ['sw_reset' sfx], ...
        'Criteria', 'u2 > Threshold', 'Threshold', '0.5');
    local_add(m, 'simulink/Signal Routing/Switch', ['sw_on' sfx], ...
        'Criteria', 'u2 > Threshold', 'Threshold', '0.5');
    local_add(m, 'simulink/Math Operations/Product', ['mul_vsw' sfx], 'Inputs', '2');
    local_add(m, 'simulink/Math Operations/Gain', ['r_loop' sfx], 'Gain', rloop_expr);
    local_add(m, 'simulink/Math Operations/Sum', ['sum_vind' sfx], 'Inputs', '+--');
    local_add(m, 'simulink/Math Operations/Gain', ['inv_l' sfx], ...
        'Gain', local_num(1.0 / spec.l_per_phase_h));
    local_add(m, 'simulink/Continuous/Integrator', ['int_i' sfx], ...
        'InitialCondition', sprintf('load_start_a/%d', n));

    local_connect(m, {
        't_over_tsw/1',         ['ph_off' sfx '/1']
        ['ph_off' sfx '/1'],    ['frac' sfx '/1']
        'unity/1',              ['frac' sfx '/2']
        ['frac' sfx '/1'],      ['on_time' sfx '/1']

        ['on_time' sfx '/1'],   ['set' sfx '/1']
        'dt_const/1',           ['set' sfx '/2']
        ['on_time' sfx '/1'],   ['ge_min' sfx '/1']
        't_on_min/1',           ['ge_min' sfx '/2']
        ['on_time' sfx '/1'],   ['ge_max' sfx '/1']
        't_on_max/1',           ['ge_max' sfx '/2']

        'cmd_per_phase/1',      ['sw_held' sfx '/1']
        ['set' sfx '/1'],       ['sw_held' sfx '/2']
        ['mem_held' sfx '/1'],  ['sw_held' sfx '/3']
        ['sw_held' sfx '/1'],   ['mem_held' sfx '/1']

        ['int_i' sfx '/1'],     ['cmp_reach' sfx '/1']
        ['sw_held' sfx '/1'],   ['cmp_reach' sfx '/2']
        ['cmp_reach' sfx '/1'], ['and_reach' sfx '/1']
        ['ge_min' sfx '/1'],    ['and_reach' sfx '/2']
        ['and_reach' sfx '/1'], ['or_reset' sfx '/1']
        ['ge_max' sfx '/1'],    ['or_reset' sfx '/2']

        'zero/1',               ['sw_reset' sfx '/1']
        ['or_reset' sfx '/1'],  ['sw_reset' sfx '/2']
        ['mem_on' sfx '/1'],    ['sw_reset' sfx '/3']
        'one/1',                ['sw_on' sfx '/1']
        ['set' sfx '/1'],       ['sw_on' sfx '/2']
        ['sw_reset' sfx '/1'],  ['sw_on' sfx '/3']
        ['sw_on' sfx '/1'],     ['mem_on' sfx '/1']

        ['sw_on' sfx '/1'],     ['mul_vsw' sfx '/1']
        'vin/1',                ['mul_vsw' sfx '/2']
        ['int_i' sfx '/1'],     ['r_loop' sfx '/1']
        ['mul_vsw' sfx '/1'],   ['sum_vind' sfx '/1']
        'sum_vout/1',           ['sum_vind' sfx '/2']
        ['r_loop' sfx '/1'],    ['sum_vind' sfx '/3']
        ['sum_vind' sfx '/1'],  ['inv_l' sfx '/1']
        ['inv_l' sfx '/1'],     ['int_i' sfx '/1']

        ['int_i' sfx '/1'],     sprintf('i_total/%d', k)
        ['int_i' sfx '/1'],     sprintf('iphase/%d', k)
    });
end

% ---- 求解器：固定步长显式欧拉，与 switching.py 一致（理由见文件头） ----
set_param(m, ...
    'SolverType', 'Fixed-step', ...
    'Solver', 'ode1', ...
    'FixedStep', local_num(dt), ...
    'StopTime', local_num(spec.solver.stop_time), ...
    'SignalLogging', 'on', ...
    'SignalLoggingName', 'logsout', ...
    'SignalLoggingSaveFormat', 'Dataset');

% ---- 信号记录（抽取到输出网格间距，对应 switching.py 的 record_every） ----
% 20 ns 步长下 1 ms 仿真有 5×10^4 步，全量记录会让产物大出一个数量级而无收益：
% 指标计算只需要能分辨 0.5 µs 的 settling_time 比较容差。
decim = local_int(round(spec.output_dt_s / dt));
local_log_signal(m, 'sum_vout', 1, spec.signal_names.vout, decim);
local_log_signal(m, 'iload', 1, spec.signal_names.iout, decim);
local_log_signal(m, 'iphase', 1, spec.signal_names.iphase, decim);

% ---- 线性分析点：与平均模型同一组，供 freq_response_estimator_on_switching 使用 ----
% 当前 metrics.yaml 的 primary_method 是 linear_analysis_on_averaged，走不到这条路径；
% 标注它的成本是两行，而缺了它那个分支会在模型上抛"未标注分析点"的异常并被
% run_linear_analysis.m 归类为"没算出可用裕量"，排查起来要绕远。
io = linio([m '/sum_err'], 1, 'openinput');
io(2) = linio([m '/sum_vout'], 1, 'output');
setlinio(m, io);

local_save(m, spec.out_path);

end


% =======================================================================
% 搭图辅助
% =======================================================================
function local_new(m)
% 已加载的同名模型先无条件关闭（不保存）：重复生成是常态，残留的旧模型会让
% add_block 因重名失败，而那个报错与真实原因（上一次生成没清理）距离很远。
if bdIsLoaded(m)
    close_system(m, 0);
end
new_system(m, 'Model');
end


function local_add(m, kind, name, varargin)
add_block(kind, [m '/' name], varargin{:});
end


function local_build_compensator(m, spec)
% 建出 compensator 子系统，内含两个设计变量的注入点：
%   <model>/compensator/Rcomp   Constant，Value = Rcomp 的 SI 值（Ω）
%   <model>/compensator/Ccomp   Constant，Value = Ccomp 的 SI 值（F）
% 两个值经子系统的第 1 / 第 2 个输出端口送到顶层。
%
% 为什么必须有这一层子系统：model.yaml 的 io_contract.injectable_params 声明的
% block_path 是 '<占位根>/compensator/Rcomp'，apply_params.m 与 inspect_model.m
% 都按这个路径做 set_param / 存在性校验（根段由 resolve_block_path.m 替换为实际
% 模型名）。路径里的 compensator 这一层因此必须是模型中真实存在的子系统。
%
% 为什么它只是一个参数容器、不含补偿器的运算逻辑：补偿器的抗积分饱和判据需要
% 钳位前的电流指令（iL_cmd_raw = vcomp/Ri），而 Ri 在子系统之外（它是温度的函数，
% 不是设计变量）。把运算逻辑搬进来会让这个子系统需要两个输入、并把 freeze 判据
% 拆到边界两侧。保持它为"补偿网络的元件值"这一单一职责，运算留在顶层，
% block_path 的语义（哪个块持有这个参数）也更直白。
%
% 用 Constant('Value') 而不是 Gain('Gain')：Ccomp 在方程里以 1/Ccomp 出现
% （d_xc = i_err/Ccomp），若用 Gain 承载就必须注入倒数，而 apply_params.m 明确
% 不做任何换算（它只 sprintf('%.17g') 后 set_param）。让 Constant 持有原值、
% 由顶层的 Divide 块做除法，注入值与 model.yaml 声明的 SI 单位逐位一致。
sub = [m '/compensator'];
add_block('simulink/Ports & Subsystems/Subsystem', sub);
% 新建的 Subsystem 自带一对 In1/Out1 与一条连线；本子系统没有输入，删掉默认内容。
delete_line(sub, 'In1/1', 'Out1/1');
delete_block([sub '/In1']);
delete_block([sub '/Out1']);

add_block('simulink/Sources/Constant', [sub '/Rcomp'], ...
    'Value', local_num(spec.rcomp_default));
add_block('simulink/Sources/Constant', [sub '/Ccomp'], ...
    'Value', local_num(spec.ccomp_default));
add_block('simulink/Sinks/Out1', [sub '/rcomp_out'], 'Port', '1');
add_block('simulink/Sinks/Out1', [sub '/ccomp_out'], 'Port', '2');
add_line(sub, 'Rcomp/1', 'rcomp_out/1', 'autorouting', 'on');
add_line(sub, 'Ccomp/1', 'ccomp_out/1', 'autorouting', 'on');
end


function local_connect(m, pairs)
for k = 1:size(pairs, 1)
    add_line(m, pairs{k, 1}, pairs{k, 2}, 'autorouting', 'on');
end
end


function local_log_signal(m, block, port_index, signal_name, decimation)
% 信号记录设在**输出端口**上，不是信号线上：R2024a 的 line 对象没有 DataLogging
% 参数（实测），信号记录是端口属性。matlab/+pa/inspect_model.m 的
% local_logged_signal_names() 以同一方式反查，两处必须一致——它遍历
% find_system(..., 'Type','port', 'PortType','outport') 并读 DataLoggingName。
%
% DataLoggingNameMode 必须显式设为 'Custom'，否则 logsout 用信号线名，而这里
% 并不给线命名。
%
% decimation 省略或 <= 1 时不做抽取（平均模型：MaxStep 已锁到输出网格间距，
% 求解步本身就是想要的分辨率）；> 1 时每 decimation 步记录一次（开关模型：
% 20 ns 求解步远细于指标需要的分辨率）。
ph = get_param([m '/' block], 'PortHandles');
port = ph.Outport(port_index);
set_param(port, 'DataLogging', 'on');
set_param(port, 'DataLoggingNameMode', 'Custom');
set_param(port, 'DataLoggingName', signal_name);
if nargin >= 5 && ~isempty(decimation) && str2double(decimation) > 1
    set_param(port, 'DataLoggingDecimateData', 'on');
    set_param(port, 'DataLoggingDecimation', decimation);
end
end


function local_save(m, out_path)
out_dir = fileparts(out_path);
if ~isempty(out_dir) && ~exist(out_dir, 'dir')
    mkdir(out_dir);
end
save_system(m, out_path);
close_system(m, 0);
end


function bias = local_carrier_bias(k, n, dt, tsw)
% 相 k 的载波相位偏置：-(k-1)/n 再加**半个求解步**。
%
% 半步偏移不是微调，它修的是一个会让整相 PWM 漏拍的浮点问题（实测）：
%
%   载波相位 frac_k = mod(t/Tsw - (k-1)/n, 1)，周期起点判据是 frac_k*Tsw < dt。
%   不加偏移时，理想周期起点恰好落在 frac_k == 0 这个**边界**上。而 t 由
%   Clock 给出、再经 Gain(1/Tsw) 缩放，t/Tsw - (k-1)/n 在该点的浮点结果可能是
%   +1e-16 也可能是 -1e-16：
%     mod(+1e-16, 1) = 1e-16          -> on_time ≈ 0，判据成立，正常开通
%     mod(-1e-16, 1) = 1 - 1e-16      -> on_time ≈ Tsw，判据不成立，**漏开通**
%   漏一次开通意味着该相在这个周期完全不导通，电流多下降一个周期，其纹波峰峰
%   翻倍。实测 4 相里恰好相 2（偏置 -0.25）踩中这一侧：相电流峰峰 22.47 A，
%   而其余三相 11~12.8 A；末 10% 窗口的 Vout 频谱里随之出现 10~80 kHz 的低频
%   成分（幅值与 2 MHz 主纹波相当），output_ripple 因此比 Python 后端大 4.5 倍。
%
%   加半步偏移后，理想周期起点处 frac_k*Tsw = 0.5*dt，稳稳落在判据内部；
%   它前一步为 Tsw - 0.5*dt，稳稳落在判据外部。两侧余量都是 0.5*dt（本配置
%   10 ns），比 1e-16 量级的浮点误差大 8 个数量级，判据不可能再翻转。
%
%   代价是 on_time 整体比真实导通时间大 0.5*dt，因此 t_on_min / t_on_max 两个
%   阈值同样加 0.5*dt 抵消（见上方构造处）——比较的仍是真实导通时间。
bias = -(k - 1) / n + 0.5 * dt / tsw;
end


function s = local_int(value)
% 整数型块参数（Mux 的 Inputs、Sum 的输入数、记录抽取倍数）不能用 %.17g——
% 那会把 4 写成 "4" 但把 10 写成 "10" 之外的情形（如 1e+01）留下隐患。
s = sprintf('%d', round(value));
end


function s = local_num(value)
% 数值一律按双精度可 round-trip 的 17 位有效数字写成块参数字符串，与
% apply_params.m 的 local_format_value() 同一口径：块的对话框参数以字符串存储，
% 十进制截断会让生成的模型与参数源存在一个说不清来源的微小偏差。
s = sprintf('%.17g', value);
end
