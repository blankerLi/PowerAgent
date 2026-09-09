"""LLM 域（design.md §1）。

本包是全系统唯一允许调用大模型的地方。三条边界由代码结构强制，不靠约定：

1. **只读输入**：`propose()` 只接受 `SearchState`，不持有 `Store` 句柄，无法写事实状态。
2. **只输出候选与假设**：输出必须经 `agent.validate` 才能进入仿真；`propose()` 不调仿真。
3. **无权决定校验严格度**：`validate()` 的 `mode` 由 `controller` 按确定性规则给出，
   LLM 不得选择——让被校验方挑选校验标准会直接击穿权限边界（design.md §6.7）。

判分权不在本包：约束判定、worst-case 聚合与排序全部在 `eval`（确定性域）。
"""
