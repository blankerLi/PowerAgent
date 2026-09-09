function collect_signals(sim_out, waveform_path)
% 从一次已完成的 sim() 结果中取出全量 logsout，落盘到指定 MAT 文件路径。
%
%   COLLECT_SIGNALS(SIM_OUT, WAVEFORM_PATH)
%
%   SIM_OUT        - sim() 的返回值（Simulink.SimulationOutput），由
%                    simulate_once.m 在仿真成功（status='ok'）后传入。
%   WAVEFORM_PATH  - 目标 MAT 文件的完整路径（由 simulate_once.m 的
%                    local_resolve_waveform_path 产出），本函数只负责把
%                    logsout 写到该路径，不做路径解析、不做原子移动。
%
%   落盘内容恰为变量名 'logsout' 的 Simulink.SimulationData.Dataset 对象，
%   不做任何裁剪或转换（simulate_once.m 只持有结果引用，不持有波形采样点
%   本体——任务 2.4 描述"返回值只含 status/waveform_path/elapsed_ms/
%   engine_starts，不含波形采样点本体"，波形采样点只存在于本函数落盘的
%   MAT 文件中，供后续 pa.export_observables 按需读取）。
%
%   本函数假定仿真使用的信号记录集合变量名为 'logsout'（Simulink 默认名称，
%   design.md 未显式声明；若模型实际配置了其他记录变量名，需同步调整
%   simulate_once.m 的 local_get_logsout 与本函数）。

try
    logsout = sim_out.get('logsout');
catch
    logsout = sim_out.logsout;
end

save(waveform_path, 'logsout', '-v7.3');

end
