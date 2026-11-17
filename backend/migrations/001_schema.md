# 初始结构

SQLAlchemy启动创建以下业务表：service_periods, service_cases, business_calendars, sla_runs, delivery_schedules, deliveries、users、audits。Source专属JSON字段由对应validation严格校验；Policy为不可覆盖的版本记录；Run保存source/policy冻结快照；Period仅用于已有期间选择器，不计入核心复杂度。已有数据库升级需先备份，后续字段演进应以独立迁移执行。


## 002 固定口径增量透视

新增表：pivot_control（单一接收水位）、source_changes（底稿变更日志/outbox）、pivot_snapshots（快照与进度）、pivot_cells（固定维度聚合）、pivot_contributions（每样本当前贡献）、pivot_issues（未计入原因）、pivot_lease（处理租约）、pivot_workers（处理心跳）。

增量列（由 `pivot.ensure_control()` 在启动时幂等补齐，参照独立迁移执行）：
- `service_cases.revision INTEGER NOT NULL DEFAULT 0`
- `sla_runs.started DOUBLE PRECISION NULL`（worker 中断恢复用）

一致性约束：
- `pivot_snapshots` 上有部分唯一索引 `uq_pivot_active_singleton`，保证全局只有一个 active 快照；切换在 `pivot_control` 行锁内完成。
- `pivot_cells` 唯一键 `(snapshot_id, period, department, priority)`；`pivot_contributions` 唯一键 `(snapshot_id, source_id)`。
- `source_changes.seq` 在控制行锁内分配，因此事件序号等于提交顺序、无空洞，可直接作为接收水位。

回滚注意：旧代码不认识上述表与列；降级前应先停写并备份。删除这些表不会影响已有 sla_runs / deliveries 的冻结内容。
