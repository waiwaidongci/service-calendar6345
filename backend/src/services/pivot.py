"""固定口径增量透视。

设计要点
--------
* 底稿写入与变更事件在同一事务提交；事件 seq 在控制行锁内分配，按提交顺序严格递增且无空洞。
* 每个快照为每条底稿保存一份“当前贡献”，单条更正只做一次撤回 + 一次计入，不扫描全库。
* 期间或小组调动时先撤回旧贡献、再计入新贡献；聚合单元按固定维度存放。
* 事件应用是幂等的：贡献上记录最后应用的 seq，重复投递与晚到的旧事件都会被忽略。
* 基线装载与事件应用都从游标继续；游标与聚合数据在同一事务提交，中断恢复不重不漏。
* 只有追平接收水位后才原子切换 active；页面在同一事务快照内读取结果与水位。
"""
import logging
import os
import socket
import time

from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError, OperationalError
from src.config.database import Session, engine
from src.models.pivot import (PivotCell, PivotContribution, PivotControl, PivotIssue,
                              PivotLease, PivotSnapshot, PivotWorker, SourceChange)
from src.models.source import Policy, Source
from src.services.calculation import business_minutes
from src.services.validation import validate_policy, validate_source

LOG = logging.getLogger('pivot')
MICROS_PER_MINUTE = 60_000_000
BASELINE_PAGE = 1000
EVENT_BATCH = 1000
LEASE_SECONDS = 10  # 持有者中断后最多等这么久即可被接管
MAX_BUILD_FAILURES = 5


def own_name():
    return f'{socket.gethostname()}:{os.getpid()}'


def ensure_schema(engine_=engine):
    """为已存在的库补齐新增列（新库由 create_all 建立）。"""
    from sqlalchemy import inspect
    insp = inspect(engine_)
    tables = set(insp.get_table_names())
    if 'service_cases' in tables:
        columns = {c['name'] for c in insp.get_columns('service_cases')}
        if 'revision' not in columns:
            with engine_.begin() as conn:
                conn.execute(text('ALTER TABLE service_cases ADD COLUMN revision INTEGER NOT NULL DEFAULT 0'))
    if 'sla_runs' in tables:
        columns = {c['name'] for c in insp.get_columns('sla_runs')}
        if 'started' not in columns:
            with engine_.begin() as conn:
                conn.execute(text('ALTER TABLE sla_runs ADD COLUMN started '
                                  + ('DOUBLE PRECISION' if engine_.dialect.name == 'postgresql' else 'FLOAT')))
    if 'pivot_snapshots' in tables:
        columns = {c['name'] for c in insp.get_columns('pivot_snapshots')}
        additions = {
            'watermark_at': 'DOUBLE PRECISION' if engine_.dialect.name == 'postgresql' else 'FLOAT',
            'failures': 'INTEGER NOT NULL DEFAULT 0',
            'last_error': 'TEXT',
            'last_error_at': 'DOUBLE PRECISION' if engine_.dialect.name == 'postgresql' else 'FLOAT',
        }
        with engine_.begin() as conn:
            for name, ddl in additions.items():
                if name not in columns:
                    conn.execute(text(f'ALTER TABLE pivot_snapshots ADD COLUMN {name} {ddl}'))


def ensure_control():
    """幂等建立控制行与租约行，并补齐存量库结构。"""
    ensure_schema()
    with Session() as db:
        if db.get(PivotControl, 1) is None:
            try:
                db.add(PivotControl(id=1, receive_seq=0, updated=time.time()))
                db.commit()
            except Exception:
                db.rollback()
        if db.get(PivotLease, 1) is None:
            try:
                db.add(PivotLease(id=1, owner=None, expires=0))
                db.commit()
            except Exception:
                db.rollback()


def begin_write(db):
    """在事务开头取得控制行的写锁。

    空更新在 PostgreSQL 上是行锁、在 SQLite 上取得 RESERVED 写锁，两种库都能让后续
    读取看到“已提交的最新接收水位”，从而保证事件序号与提交顺序一致、无空洞。
    """
    db.execute(text('UPDATE pivot_control SET updated = updated WHERE id = 1'))


def lock_control(db):
    begin_write(db)
    ctrl = (db.query(PivotControl).filter_by(id=1)
            .populate_existing().with_for_update().first())
    if ctrl is None:
        raise RuntimeError('透视控制行缺失，请重启后端完成初始化')
    return ctrl


def record_change(db, source_id, op, data, actor):
    """写入变更事件并推进接收水位；必须与底稿写入在同一事务提交。"""
    ctrl = lock_control(db)
    seq = int(ctrl.receive_seq) + 1
    ctrl.receive_seq = seq
    ctrl.updated = time.time()
    db.add(SourceChange(seq=seq, source_id=source_id, op=op, data=data,
                        actor=actor, at=time.time()))
    return seq


# ---------------------------------------------------------------------------
# 贡献计算与聚合
# ---------------------------------------------------------------------------

def contribution(data, policy):
    validate_source(data)
    priority = data['priority']
    if priority not in policy['sla_minutes']:
        raise ValueError('优先级无SLA口径：' + priority)
    minutes = business_minutes(data['opened'], data['closed'], policy)
    return (data['period'], data['department'], priority,
            int(round(minutes * MICROS_PER_MINUTE)),
            int(minutes > policy['sla_minutes'][priority]))


def _cell(db, snapshot_id, period, department, priority):
    return db.query(PivotCell).filter_by(
        snapshot_id=snapshot_id, period=period, department=department,
        priority=priority).with_for_update().first()


def adjust_cell(db, snapshot_id, period, department, priority, samples, micros, overdue):
    cell = _cell(db, snapshot_id, period, department, priority)
    if cell is None:
        cell = PivotCell(snapshot_id=snapshot_id, period=period, department=department,
                         priority=priority, samples=0, micros=0, overdue=0)
        db.add(cell)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            # 并发建立同一维度单元时退回已有行。
            cell = _cell(db, snapshot_id, period, department, priority)
            if cell is None:
                raise
    cell.samples += samples
    cell.micros += micros
    cell.overdue += overdue
    if cell.samples < 0 or cell.micros < 0 or cell.overdue < 0:
        raise RuntimeError(f'聚合单元出现负值：{period}/{department}/{priority}')
    if cell.samples == 0:
        db.delete(cell)


def _row(db, snapshot_id, source_id):
    return db.query(PivotContribution).filter_by(
        snapshot_id=snapshot_id, source_id=source_id).with_for_update().first()


def _withdraw(db, snapshot_id, row):
    if row is not None and row.active:
        adjust_cell(db, snapshot_id, row.period, row.department, row.priority,
                    -1, -int(row.micros), -int(row.overdue))
    if row is not None:
        row.active = False
        row.micros = 0
        row.overdue = 0


def _clear_issues(db, snapshot_id, source_id):
    db.query(PivotIssue).filter_by(snapshot_id=snapshot_id, source_id=source_id).delete()


def _tombstone(db, snapshot_id, source_id, seq):
    row = _row(db, snapshot_id, source_id)
    if row is None:
        row = PivotContribution(snapshot_id=snapshot_id, source_id=source_id, seq=0,
                                period='', department='', priority='', micros=0,
                                overdue=0, active=False)
        db.add(row)
    row.seq = seq
    row.active = False
    return row


def apply_event(db, snap, ev):
    """应用一条变更事件；返回 applied / skipped / deleted / issue。"""
    row = _row(db, snap.id, ev.source_id)
    if row is not None and int(row.seq) >= int(ev.seq):
        return 'skipped'  # 重复投递或晚到的旧事件，绝不倒退
    _withdraw(db, snap.id, row)
    if ev.op == 'delete':
        _tombstone(db, snap.id, ev.source_id, ev.seq)
        _clear_issues(db, snap.id, ev.source_id)
        return 'deleted'
    try:
        period, department, priority, micros, overdue = contribution(ev.data, snap.policy_data)
    except ValueError as exc:
        _tombstone(db, snap.id, ev.source_id, ev.seq)
        _clear_issues(db, snap.id, ev.source_id)
        db.add(PivotIssue(snapshot_id=snap.id, source_id=ev.source_id, seq=ev.seq,
                          reason=str(exc)[:300], at=time.time()))
        return 'issue'
    row = _row(db, snap.id, ev.source_id)
    if row is None:
        row = PivotContribution(snapshot_id=snap.id, source_id=ev.source_id, seq=0)
        db.add(row)
    row.period, row.department, row.priority = period, department, priority
    row.micros, row.overdue, row.seq, row.active = micros, overdue, ev.seq, True
    adjust_cell(db, snap.id, period, department, priority, 1, micros, overdue)
    _clear_issues(db, snap.id, ev.source_id)
    return 'applied'


def apply_pending(db, snap, limit=EVENT_BATCH, upto=None):
    query = db.query(SourceChange).filter(SourceChange.seq > int(snap.change_cursor))
    if upto is not None:
        query = query.filter(SourceChange.seq <= upto)
    events = query.order_by(SourceChange.seq).limit(limit).all()
    for ev in events:
        apply_event(db, snap, ev)
        # 水位只前进不倒退；数据与游标同事务提交。
        snap.change_cursor = max(int(snap.change_cursor), int(ev.seq))
        snap.watermark = max(int(snap.watermark), int(snap.change_cursor))
    return len(events)


def backlog(db, snap):
    return int(db.query(func.count(SourceChange.seq))
               .filter(SourceChange.seq > int(snap.change_cursor)).scalar() or 0)


# ---------------------------------------------------------------------------
# 基线装载
# ---------------------------------------------------------------------------

def seed_contribution(db, snap, src):
    """基线装载一条底稿；贡献标记为基线水位，更旧的重复事件不会重复计入。"""
    seq = int(snap.base_watermark)
    try:
        period, department, priority, micros, overdue = contribution(src.data, snap.policy_data)
    except ValueError as exc:
        db.add(PivotContribution(snapshot_id=snap.id, source_id=src.id, seq=seq,
                                 period='', department='', priority='', micros=0,
                                 overdue=0, active=False))
        db.add(PivotIssue(snapshot_id=snap.id, source_id=src.id, seq=seq,
                          reason=str(exc)[:300], at=time.time()))
        return
    db.add(PivotContribution(snapshot_id=snap.id, source_id=src.id, seq=seq,
                             period=period, department=department, priority=priority,
                             micros=micros, overdue=overdue, active=True))
    adjust_cell(db, snap.id, period, department, priority, 1, micros, overdue)


def build_page(db, snap, limit=BASELINE_PAGE):
    # 基线开始前再校验存储的口径，损坏数据以失败状态暴露而不是产生半对结果。
    validate_policy(snap.policy_data)
    if not snap.base_total:
        snap.base_total = int(db.query(func.count(Source.id)).scalar() or 0)
    rows = (db.query(Source).filter(Source.id > int(snap.base_cursor))
            .order_by(Source.id).limit(limit).all())
    for src in rows:
        seed_contribution(db, snap, src)
        snap.base_cursor = int(src.id)
        snap.base_done = int(snap.base_done) + 1
    if not rows or len(rows) < limit:
        snap.base_complete = True
    db.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 快照建立、追平与原子切换
# ---------------------------------------------------------------------------

def create_snapshot(db, policy, return_created=False):
    """为口径建立新的构建快照；旧 active 继续服务，重建期间照常接收改动。"""
    validate_policy(policy.data)
    existing = (db.query(PivotSnapshot)
                .filter(PivotSnapshot.policy_id == policy.id,
                        PivotSnapshot.status.in_(('building', 'catching')))
                .order_by(PivotSnapshot.id).first())
    if existing is not None:
        return (existing, False) if return_created else existing
    ctrl = lock_control(db)
    snap = PivotSnapshot(policy_id=policy.id, policy_version=policy.version,
                         policy_data=policy.data, status='building',
                         base_watermark=int(ctrl.receive_seq),
                         change_cursor=int(ctrl.receive_seq),
                         watermark=int(ctrl.receive_seq), created=time.time())
    db.add(snap)
    db.flush()
    if int(db.query(func.count(Source.id)).scalar() or 0) == 0:
        snap.base_complete = True
    db.commit()
    return (snap, True) if return_created else snap


def _failure(db, snap, exc):
    snap.failures = int(snap.failures or 0) + 1
    snap.last_error = str(exc)[:1000]
    snap.last_error_at = time.time()
    if snap.status in ('building', 'catching') and snap.failures >= MAX_BUILD_FAILURES:
        snap.status = 'failed'
        snap.error = snap.last_error
    db.commit()


def activate(db, snap):
    """追平接收水位后原子切换；旧快照保留但不再被读取。

    先在锁外把积压追到一批以内，再持控制行锁完成最后一批与切换，
    把阻塞底稿写入的窗口压到最小；锁内读到的是唯一确定的切换水位。
    """
    while backlog(db, snap) > EVENT_BATCH:
        if not apply_pending(db, snap):
            break
        db.commit()
    ctrl = lock_control(db)
    target = int(ctrl.receive_seq)
    while int(snap.change_cursor) < target:
        if not apply_pending(db, snap, upto=target):
            break
    if int(snap.change_cursor) < target:
        raise RuntimeError('追平接收水位失败，暂不切换')
    # 全局只保留一个 active。先以一条 UPDATE 撤下旧快照（顺序确定），再让新快照生效，
    # 避免部分唯一索引在切换瞬间看到两个 active。
    (db.query(PivotSnapshot)
     .filter(PivotSnapshot.status == 'active', PivotSnapshot.id != snap.id)
     .update({'status': 'superseded'}, synchronize_session=False))
    db.flush()
    snap.status = 'active'
    snap.watermark = target
    snap.watermark_at = time.time()
    snap.activated = time.time()
    snap.error = None
    db.commit()


# ---------------------------------------------------------------------------
# 租约与心跳：多进程下同一时刻只有一个推进者，中断后租约过期自动接管
# ---------------------------------------------------------------------------

def acquire_lease(db, owner, ttl=LEASE_SECONDS):
    now = time.time()
    try:
        updated = (db.query(PivotLease)
                   .filter(PivotLease.id == 1,
                           or_(PivotLease.owner.is_(None), PivotLease.expires < now,
                               PivotLease.owner == owner))
                   .update({'owner': owner, 'expires': now + ttl}, synchronize_session=False))
        db.commit()
    except OperationalError:
        # 别的进程正在推进；本轮跳过即可，下一轮再试。
        db.rollback()
        return False
    return updated == 1


def renew_lease(db, owner, ttl=LEASE_SECONDS):
    now = time.time()
    try:
        updated = (db.query(PivotLease)
                   .filter(PivotLease.id == 1, PivotLease.owner == owner)
                   .update({'expires': now + ttl}, synchronize_session=False))
        db.commit()
    except OperationalError:
        db.rollback()
        return False
    return updated == 1


def release_lease(db, owner):
    try:
        (db.query(PivotLease)
         .filter(PivotLease.id == 1, PivotLease.owner == owner)
         .update({'expires': 0}, synchronize_session=False))
        db.commit()
    except OperationalError:
        db.rollback()


def heartbeat(db, owner):
    now = time.time()
    try:
        row = db.get(PivotWorker, owner)
        if row is None:
            db.add(PivotWorker(name=owner, heartbeat=now, note='增量透视处理进程'))
        else:
            row.heartbeat = now
        db.commit()
    except OperationalError:
        db.rollback()


# ---------------------------------------------------------------------------
# 调度入口
# ---------------------------------------------------------------------------

def status_summary():
    """健康检查用：不需要认证即可得知透视是否追平。"""
    try:
        with Session() as db:
            ctrl = db.get(PivotControl, 1)
            active = db.query(PivotSnapshot).filter_by(status='active').first()
            building = db.query(PivotSnapshot).filter(
                PivotSnapshot.status.in_(('building', 'catching'))).count()
            receive = int(ctrl.receive_seq) if ctrl else 0
            return {'receive_seq': receive,
                    'watermark': int(active.watermark) if active else 0,
                    'lag': max(0, receive - int(active.watermark)) if active else receive,
                    'building': building}
    except Exception:
        LOG.exception('透视状态读取失败')
        return {'error': 'unavailable'}


def ensure_default_snapshot(db):
    """存量库升级：最新口径尚无任何快照时自动建账。"""
    if db.query(PivotSnapshot).count():
        return
    policy = db.query(Policy).order_by(Policy.id.desc()).first()
    if policy is None:
        return
    create_snapshot(db, policy)


def pivot_tick(owner=None, budget=0.8):
    """worker 每轮调用：先保持 active 最新，再推进一个构建快照。"""
    owner = owner or own_name()
    started = time.time()
    with Session() as db:
        if not acquire_lease(db, owner):
            return {'lease': False}
        result = {'lease': True, 'active': 0, 'baseline': 0, 'events': 0, 'activated': None,
                  'error': None}
        try:
            heartbeat(db, owner)
            ensure_default_snapshot(db)
            # 1) 所有 active 快照保持最新
            for snap in db.query(PivotSnapshot).filter_by(status='active').order_by(PivotSnapshot.id).all():
                if time.time() - started > budget:
                    break
                if not renew_lease(db, owner):
                    return result
                try:
                    result['active'] += apply_pending(db, snap)
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    LOG.exception('active 透视追赶失败')
                    _failure(db, db.get(PivotSnapshot, snap.id), exc)
                    result['error'] = str(exc)
            # 2) 推进构建中快照：失败次数少的先推进，避免一个反复失败的建账永久堵住其它口径。
            build = (db.query(PivotSnapshot)
                     .filter(PivotSnapshot.status.in_(('building', 'catching')))
                     .order_by(PivotSnapshot.failures, PivotSnapshot.id).first())
            if build is not None and time.time() - started <= budget:
                if not renew_lease(db, owner):
                    return result
                try:
                    if not build.base_complete:
                        result['baseline'] = build_page(db, build)
                        build = db.get(PivotSnapshot, build.id)
                    if build.base_complete and build.status == 'building':
                        build.status = 'catching'
                        db.commit()
                    if build.base_complete:
                        result['events'] = apply_pending(db, build)
                        db.commit()
                        build = db.get(PivotSnapshot, build.id)
                        if int(build.change_cursor) >= int(lock_control(db).receive_seq):
                            activate(db, build)
                            result['activated'] = build.id
                except Exception as exc:
                    db.rollback()
                    LOG.exception('构建透视失败')
                    _failure(db, db.get(PivotSnapshot, build.id), exc)
                    result['error'] = str(exc)
            if time.time() - started <= budget:
                try:
                    prune_changes(db)
                except Exception:
                    db.rollback()
                    LOG.exception('清理变更日志失败')
            return result
        finally:
            try:
                release_lease(db, owner)
            except Exception:
                db.rollback()


def prune_changes(db, keep_recent=2000):
    """清理已不再需要的变更事件。

    任何 active 或构建中快照的游标以下、且不是最近一批的事件都不再会被读取；
    重建总是从当前接收水位开始，因此不受影响。保留最近事件便于排查。
    """
    snapshots = db.query(PivotSnapshot).filter(
        PivotSnapshot.status.in_(('active', 'building', 'catching'))).all()
    if not snapshots:
        return 0
    floor = min(int(s.change_cursor) for s in snapshots)
    newest = int(db.query(func.max(SourceChange.seq)).scalar() or 0)
    limit = min(floor, newest - keep_recent)
    if limit <= 0:
        return 0
    deleted = db.query(SourceChange).filter(SourceChange.seq <= limit).delete(
        synchronize_session=False)
    db.commit()
    return deleted


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def read_state(policy_id=None):
    """在一个事务快照内读取完整结果与对应水位，保证页面对应同一水位。"""
    db = Session()
    try:
        if engine.dialect.name == 'postgresql':
            # 同一事务快照内读取结果、水位与进度，避免页面拼出跨水位的半成品。
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
        ctrl = db.get(PivotControl, 1)
        receive_seq = int(ctrl.receive_seq) if ctrl else 0
        if policy_id is None:
            policy = db.query(Policy).order_by(Policy.id.desc()).first()
            policy_id = policy.id if policy else None
        active = db.query(PivotSnapshot).filter_by(status='active').first()
        building = db.query(PivotSnapshot).filter(
            PivotSnapshot.status.in_(('building', 'catching'))
        ).order_by(PivotSnapshot.failures, PivotSnapshot.id).first()
        history = (db.query(PivotSnapshot).order_by(PivotSnapshot.id.desc()).limit(30).all())
        state = {'receive_seq': receive_seq, 'policy_id': policy_id,
                 'active_policy_id': active.policy_id if active else None,
                 'snapshots': [_snapshot_view(s) for s in history],
                 'active': None, 'building': None,
                 'workers': _workers(db), 'issues': [], 'reason': ''}
        if active is not None:
            rows, summary = _rows(db, active)
            lag = max(0, receive_seq - int(active.watermark))
            state['active'] = {
                'id': active.id, 'policy_version': active.policy_version,
                'watermark': int(active.watermark), 'watermark_at': active.watermark_at,
                'activated': active.activated, 'lag': lag,
                'rows': rows, 'summary': summary,
                'issues_count': db.query(PivotIssue).filter_by(snapshot_id=active.id).count(),
                'last_error': active.last_error, 'last_error_at': active.last_error_at,
            }
            state['issues'] = [{'source_id': x.source_id, 'seq': int(x.seq),
                                'reason': x.reason, 'at': x.at}
                               for x in (db.query(PivotIssue)
                                         .filter_by(snapshot_id=active.id)
                                         .order_by(PivotIssue.id.desc()).limit(50).all())]
        if building is not None:
            state['building'] = {
                'id': building.id, 'policy_version': building.policy_version,
                'status': building.status, 'base_total': int(building.base_total),
                'base_done': int(building.base_done),
                'base_complete': bool(building.base_complete),
                'watermark': int(building.watermark),
                'backlog': backlog(db, building) if building.base_complete else None,
                'error': building.error, 'last_error': building.last_error,
            }
        state['failed'] = [{'id': s.id, 'policy_version': s.policy_version,
                            'error': s.error} for s in
                           db.query(PivotSnapshot).filter_by(status='failed')
                           .order_by(PivotSnapshot.id.desc()).limit(5).all()]
        state['reason'] = explain(state)
        return state
    finally:
        db.close()


def _snapshot_view(s):
    return {'id': s.id, 'policy_id': s.policy_id, 'policy_version': s.policy_version,
            'status': s.status, 'watermark': int(s.watermark),
            'base_total': int(s.base_total), 'base_done': int(s.base_done),
            'base_complete': bool(s.base_complete), 'created': s.created,
            'activated': s.activated, 'error': s.error,
            'last_error': s.last_error, 'last_error_at': s.last_error_at}


def _workers(db):
    now = time.time()
    return [{'name': w.name, 'age': round(now - w.heartbeat, 1),
             'alive': now - w.heartbeat < 15}
            for w in db.query(PivotWorker).order_by(PivotWorker.heartbeat.desc()).limit(10).all()]


def _rows(db, snap):
    cells = (db.query(PivotCell).filter_by(snapshot_id=snap.id)
             .order_by(PivotCell.period, PivotCell.department, PivotCell.priority).all())
    rows = []
    total_samples = total_micros = total_overdue = 0
    for c in cells:
        minutes = int(c.micros) / MICROS_PER_MINUTE
        rows.append({'月份': c.period, '小组': c.department, '优先级': c.priority,
                     '样本数': int(c.samples), '业务分钟合计': minutes,
                     '平均业务分钟': round(minutes / c.samples, 4) if c.samples else 0,
                     '超时数': int(c.overdue),
                     '超时率': round(c.overdue / c.samples, 4) if c.samples else 0})
        total_samples += int(c.samples)
        total_micros += int(c.micros)
        total_overdue += int(c.overdue)
    summary = {'样本数': total_samples, '业务分钟合计': total_micros / MICROS_PER_MINUTE,
               '超时数': total_overdue, '时区': snap.policy_data['timezone']}
    return rows, summary


def explain(state):
    parts = []
    alive = [w for w in state['workers'] if w['alive']]
    building = state['building']
    if building is not None:
        if not building['base_complete']:
            parts.append(f'新口径建账中：基线已装载 {building["base_done"]}/{building["base_total"]} 条；'
                         '期间的增删改已记入变更日志，不阻塞接收')
        else:
            left = building['backlog']
            parts.append(f'基线完成，正在追赶变更（剩余 {left if left is not None else "?"} 条），'
                         '追平接收水位后原子切换，切换前页面继续读取旧口径完整结果')
        if building['error']:
            parts.append('建账已失败：' + building['error'] + '；旧结果仍保留')
        elif building['last_error']:
            parts.append('建账最近错误（自动重试中）：' + building['last_error'])
    for failed in state.get('failed', [])[:1]:
        parts.append(f'{failed["policy_version"]} 建账失败，已保留旧结果：{failed["error"]}；'
                     '修正口径后可重建')
    active = state['active']
    if active is not None:
        if active['lag'] <= 0:
            parts.append(f'已追平接收水位 {state["receive_seq"]}，页面结果与水位一致')
        elif not alive:
            ages = [w['age'] for w in state['workers']]
            last = f'最后心跳 {max(ages)} 秒前' if ages else '从未收到心跳'
            parts.append(f'尚有 {active["lag"]} 条变更待计入；处理进程无有效心跳（{last}），'
                         '恢复后从水位继续，不会漏算')
        else:
            parts.append(f'处理进程正常，尚有 {active["lag"]} 条变更待计入')
        if active['last_error']:
            parts.append('最近一次应用失败（自动重试中）：' + active['last_error'])
        if active['issues_count']:
            parts.append(f'{active["issues_count"]} 条底稿因口径不完整未计入，见问题清单')
    elif not parts:
        parts.append('尚未建立透视快照，处理进程会自动建账')
    return '；'.join(parts)
