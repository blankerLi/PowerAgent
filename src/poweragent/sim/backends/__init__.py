"""Python 仿真后端（仿真域）。

这里的「后端」指仿真求解器的实现，与 web backend 无关。系统有两个后端：

- **MATLAB 后端**：`sim/engine.py` 的 `MatlabSession` + `matlab/+pa/*.m`，
  把参数注入 Simulink 模型后调 `sim()`。高保真，但依赖 Simulink 许可证。
- **Python 后端**：本包，用 `scipy` 直接解多相 Buck 的微分方程。无外部许可证
  依赖，可在 CI 中运行，快到足以支撑上百次搜索迭代。

两者共享同一个函数边界：`session.call(fn, *args, nargout=)`。`sim/simulate.py`
只通过这个边界与后端交互，因此换后端不需要改它，也不需要引入抽象基类
（design.md §1：两个扩展点是函数边界而非抽象层）。

保真度分层：平均模型（`averaged`）按开关周期平均，不含 PWM 纹波，用于筛选层；
开关模型（`switching`）显式建模 PWM 与相位交错，含真实纹波，用于评价层。两者
描述同一个电路，功率级与工作点参数由 `models/buck4ph_*.yaml` 提供并由测试守护
其一致性。
"""
