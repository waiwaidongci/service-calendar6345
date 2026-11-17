from sqlalchemy import (BigInteger, Boolean, Column, Float, Index, Integer, JSON,
                        String, Text, UniqueConstraint, text)
from src.config.database import Base

# SQLite 的自增主键必须是 INTEGER，因此在大整数上做方言变体。
BIG = BigInteger().with_variant(Integer, 'sqlite')


class PivotControl(Base):
    """单一控制行：接收水位。写入方在持锁事务内推进它并提交变更事件。"""
    __tablename__ = 'pivot_control'
    id = Column(Integer, primary_key=True)
    # 所有 seq <= receive_seq 的变更事件都已提交可见；没有空洞。
    receive_seq = Column(BIG, nullable=False, default=0)
    updated = Column(Float)


class SourceChange(Base):
    """底稿变更日志（outbox）。seq 在控制行锁内分配，因此等于提交顺序，无空洞。"""
    __tablename__ = 'source_changes'
    seq = Column(BIG, primary_key=True, autoincrement=True)
    source_id = Column(Integer, nullable=False)
    op = Column(String(10), nullable=False)  # upsert / delete
    data = Column(JSON)
    actor = Column(String(120))
    at = Column(Float)
    __table_args__ = (Index('ix_source_changes_source_id', 'source_id'),)


class PivotSnapshot(Base):
    """一个固定口径的透视版本；只有 active 状态会被页面读取。"""
    __tablename__ = 'pivot_snapshots'
    id = Column(Integer, primary_key=True)
    policy_id = Column(Integer, nullable=False)
    policy_version = Column(String(120), nullable=False)
    policy_data = Column(JSON, nullable=False)
    # building=基线装载中；catching=基线完成、追赶变更；active=已原子切换；
    # superseded=被新快照替换；failed=重建失败（旧 active 继续服务）
    status = Column(String(20), nullable=False)
    base_watermark = Column(BIG, nullable=False, default=0)  # 基线开始时已提交的接收水位
    base_cursor = Column(Integer, nullable=False, default=0)  # 基线已装载到的 source.id
    base_complete = Column(Boolean, nullable=False, default=False)
    base_total = Column(Integer, nullable=False, default=0)
    base_done = Column(Integer, nullable=False, default=0)
    change_cursor = Column(BIG, nullable=False, default=0)  # 已应用的事件 seq
    watermark = Column(BIG, nullable=False, default=0)  # 页面可见结果对应的水位
    watermark_at = Column(Float)
    failures = Column(Integer, nullable=False, default=0)
    created = Column(Float)
    activated = Column(Float)
    error = Column(Text)
    last_error = Column(Text)
    last_error_at = Column(Float)
    # 全局仅一个 active：页面始终读取同一水位的完整结果，切换在控制行锁内原子完成。
    singleton = Column(Integer, nullable=False, default=1)
    __table_args__ = (
        Index('uq_pivot_active_singleton', 'singleton', unique=True,
              sqlite_where=text("status = 'active'"),
              postgresql_where=text("status = 'active'")),
    )


class PivotCell(Base):
    """固定维度（期间/小组/优先级）的增量聚合单元。微秒整数避免浮点漂移。"""
    __tablename__ = 'pivot_cells'
    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, nullable=False)
    period = Column(String(100), nullable=False)
    department = Column(String(100), nullable=False)
    priority = Column(String(100), nullable=False)
    samples = Column(Integer, nullable=False, default=0)
    micros = Column(BigInteger, nullable=False, default=0)
    overdue = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint('snapshot_id', 'period', 'department', 'priority',
                                       name='uq_pivot_cell'),)


class PivotContribution(Base):
    """每个底稿样本在一个快照中的当前贡献，用于 O(1) 撤回旧贡献。

    seq 是该贡献最后一次被应用的事件序号；晚到的旧事件（seq 更小）会被忽略。
    删除后保留 active=False 的墓碑行，避免更旧的事件让样本“复活”。
    """
    __tablename__ = 'pivot_contributions'
    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, nullable=False)
    source_id = Column(Integer, nullable=False)
    seq = Column(BIG, nullable=False, default=0)
    period = Column(String(100), nullable=False, default='')
    department = Column(String(100), nullable=False, default='')
    priority = Column(String(100), nullable=False, default='')
    micros = Column(BigInteger, nullable=False, default=0)
    overdue = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    __table_args__ = (UniqueConstraint('snapshot_id', 'source_id',
                                       name='uq_pivot_contribution'),)


class PivotIssue(Base):
    """无法计入的样本：不阻塞水位推进，但在页面上显式暴露原因。"""
    __tablename__ = 'pivot_issues'
    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, nullable=False)
    source_id = Column(Integer, nullable=False)
    seq = Column(BIG, nullable=False, default=0)
    reason = Column(String(300), nullable=False)
    at = Column(Float)
    __table_args__ = (Index('ix_pivot_issues_snapshot', 'snapshot_id'),)


class PivotLease(Base):
    """处理租约：同一时刻只有一个进程推进快照；持有者中断后租约过期可被接管。"""
    __tablename__ = 'pivot_lease'
    id = Column(Integer, primary_key=True)
    owner = Column(String(160))
    expires = Column(Float, nullable=False, default=0)


class PivotWorker(Base):
    """处理进程心跳，供页面解释“为什么滞后”。"""
    __tablename__ = 'pivot_workers'
    name = Column(String(160), primary_key=True)
    heartbeat = Column(Float, nullable=False)
    note = Column(String(240))
