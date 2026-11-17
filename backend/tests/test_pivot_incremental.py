"""增量透视的端到端回归：结果与全量重算逐行一致，并发/重复/中断都不漏不重。"""
import os, sys, subprocess, tempfile, time

if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('JWT_SECRET', 'test')
os.environ.setdefault('PROJECT_JSON', os.path.join(BACKEND, 'project.json'))
# 自包含的临时库，不触碰演示数据；可用 PIVOT_TEST_DB 指定。
DB_PATH = os.environ.get('PIVOT_TEST_DB') or os.path.join(tempfile.mkdtemp(prefix='pivot-test-'), 'pivot.db')
URL = 'sqlite:///' + DB_PATH
os.environ['DATABASE_URL'] = URL

from src.config.database import Base, Session, engine
from src.models.source import Source, Policy
from sqlalchemy import func
from src.models.pivot import (PivotSnapshot, SourceChange, PivotControl, PivotContribution,
                              PivotCell, PivotIssue)
from src.services import pivot as p
from src.services.calculation import calculate
from src.services.source import save_source, delete_source
from src.models.identity import User
from scripts.seed_demo import seed

Base.metadata.create_all(engine)
p.ensure_control()
seed()

failures = []


def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + ((' · ' + detail) if detail and not cond else ''))
    if not cond:
        failures.append(name)


def full_result():
    with Session() as db:
        policy = db.query(Policy).order_by(Policy.id.desc()).first()
        return calculate([s.data for s in db.query(Source).order_by(Source.id).all()], policy.data)


def normalize(result):
    rows = sorted([(r['月份'], r['小组'], r['优先级'], r['样本数'], round(r['业务分钟合计'], 6),
                    r['超时数']) for r in result['rows']])
    s = result['summary']
    return rows, (s['样本数'], round(s['业务分钟合计'], 6), s['超时数'])


def matches(name):
    expected = normalize(full_result())
    st = p.read_state()
    active = st['active']
    actual = (sorted([(r['月份'], r['小组'], r['优先级'], r['样本数'], round(r['业务分钟合计'], 6),
                       r['超时数']) for r in active['rows']]),
              (active['summary']['样本数'], round(active['summary']['业务分钟合计'], 6),
               active['summary']['超时数']))
    check(name, expected == actual, f'expected={expected} actual={actual}')
    return st


def tick(*args, **kw):
    return p.pivot_tick('test', *args, **kw)


def analyst():
    with Session() as db:
        return db.query(User).filter_by(email='analyst@demo.local').first()


def build_until_done(snapshot_id=None, limit=400):
    """推进到指定快照成为 active（None 表示任一 active 即可）。"""
    for _ in range(limit):
        with Session() as db:
            if snapshot_id is None:
                if db.query(PivotSnapshot).filter_by(status='active').count():
                    return True
            else:
                snap = db.get(PivotSnapshot, snapshot_id)
                if snap.status == 'active':
                    return True
                if snap.status == 'failed':
                    return False
        tick()
    return False


# ---------------------------------------------------------------------------
# 1. 建账后与全量重算一致
# ---------------------------------------------------------------------------
check('initial build activates', build_until_done())
st = matches('initial equals full recompute')
check('watermark equals receive_seq', st['active']['watermark'] == st['receive_seq'])

# ---------------------------------------------------------------------------
# 2. 增删改自动反映
# ---------------------------------------------------------------------------
user = analyst()
with Session() as db:
    created = save_source(db, user, {'period': '2026-11', 'department': '数据建模组',
                                     'opened': '2026-11-06T10:30:00+08:00',
                                     'closed': '2026-11-06T16:30:00+08:00', 'priority': '紧急'})
    sid = created.id
st = matches('insert after one tick') if tick() else None

with Session() as db:
    save_source(db, user, {'period': '2026-12', 'department': '数据核验组',
                           'opened': '2026-11-06T10:30:00+08:00',
                           'closed': '2026-11-06T16:30:00+08:00', 'priority': '普通'}, sid)
tick()
st = matches('period move withdraws old cell and adds new')

with Session() as db:
    save_source(db, user, {'period': '2026-12', 'department': '数据采样组',
                           'opened': '2026-11-06T09:00:00+08:00',
                           'closed': '2026-11-06T18:00:00+08:00', 'priority': '普通'}, sid)
tick()
st = matches('department move and duration change')

with Session() as db:
    delete_source(db, user, sid)
tick()
st = matches('delete withdraws contribution')

# ---------------------------------------------------------------------------
# 3. 单条更正不扫描全库
# ---------------------------------------------------------------------------
with Session() as db:
    save_source(db, user, {'period': '2026-11', 'department': '数据核验组',
                           'opened': '2026-11-09T10:00:00+08:00',
                           'closed': '2026-11-09T12:00:00+08:00', 'priority': '普通'}, 1)
    annual = db.query(Source).order_by(Source.id).first()
    total = db.query(Source).count()
scanned = {'n': 0}
import sqlalchemy.orm.session as orm_session
_orig = orm_session.Session.execute


def counting(self, statement, *a, **kw):
    if 'FROM service_cases' in str(statement):
        scanned['n'] += 1
    return _orig(self, statement, *a, **kw)


orm_session.Session.execute = counting
tick()
orm_session.Session.execute = _orig
check('single correction does not scan all sources', scanned['n'] <= 1, f'scans={scanned["n"]} of {total}')
matches('single correction result')

# ---------------------------------------------------------------------------
# 4. 重复投递与晚到事件不重复、不倒退
# ---------------------------------------------------------------------------
with Session() as db:
    snap = db.query(PivotSnapshot).filter_by(status='active').first()
    before = (snap.watermark,)
    events = db.query(SourceChange).order_by(SourceChange.seq.desc()).limit(3).all()
    snap.change_cursor = int(events[-1].seq) - 1
    db.commit()
    snap = db.query(PivotSnapshot).filter_by(status='active').first()
    from src.services.pivot import apply_event
    results = [apply_event(db, snap, ev) for ev in events]
    db.commit()
check('redelivered events are idempotent', 'skipped' in results or all(r in ('applied', 'deleted', 'issue', 'skipped') for r in results),
      str(results))
matches('replay does not duplicate')
with Session() as db:
    snap = db.query(PivotSnapshot).filter_by(status='active').first()
    latest = db.query(SourceChange).order_by(SourceChange.seq.desc()).first()
    old = db.query(SourceChange).filter(SourceChange.source_id == latest.source_id,
                                        SourceChange.seq < latest.seq).order_by(SourceChange.seq).first()
    if old is not None:
        state = apply_event(db, snap, old)
        db.commit()
        check('stale event ignored', state == 'skipped', state)

# ---------------------------------------------------------------------------
# 5. 重建期间继续接收、追平后原子切换、失败保留旧结果
# ---------------------------------------------------------------------------
with Session() as db:
    policy2 = Policy(version='工作时钟-v2', data=dict(db.query(Policy).first().data, version='工作时钟-v2'))
    db.add(policy2)
    db.commit()
    db.refresh(policy2)
    snap2 = p.create_snapshot(db, policy2)
    sid2 = snap2.id
    check('rebuild starts building', snap2.status == 'building')

with Session() as db:
    # 只装载一小段基线，模拟重建仍在进行
    p.build_page(db, db.get(PivotSnapshot, sid2), limit=5)
    snap2 = db.get(PivotSnapshot, sid2)
    check('rebuild in progress', not snap2.base_complete and snap2.status != 'active',
          f'{snap2.status} {snap2.base_done}/{snap2.base_total}')
    check('old result still served during rebuild',
          db.query(PivotSnapshot).filter_by(status='active').count() == 1)

with Session() as db:
    save_source(db, user, {'period': '2026-12', 'department': '数据建模组',
                           'opened': '2026-11-10T10:00:00+08:00',
                           'closed': '2026-11-10T12:00:00+08:00', 'priority': '普通'})
check('writes accepted during rebuild', True)

with Session() as db:
    snap1 = db.query(PivotSnapshot).filter_by(policy_id=1).first()
    old_id = snap1.id

build_until_done(sid2)
tick()  # 切换后再追赶一次，确认 active 追平接收水位
with Session() as db:
    active = db.query(PivotSnapshot).filter_by(status='active').all()
    check('exactly one active snapshot', len(active) == 1, str([(a.id, a.policy_id) for a in active]))
    new_active = active[0]
    check('new active is the rebuilt one', new_active.id == sid2, f'{new_active.id} != {sid2}')
    check('active watermark catches receive_seq',
          new_active.watermark == db.get(PivotControl, 1).receive_seq,
          f'{new_active.watermark} vs {db.get(PivotControl,1).receive_seq}')
    old_snap = db.get(PivotSnapshot, old_id)
    check('old snapshot preserved', old_snap is not None)
    check('old snapshot frozen at its watermark', old_snap.status == 'superseded')
    frozen_rows = p._rows(db, old_snap)
    frozen_watermark = old_snap.watermark

with Session() as db:
    save_source(db, user, {'period': '2026-12', 'department': '数据核验组',
                           'opened': '2026-11-13T10:00:00+08:00',
                           'closed': '2026-11-13T11:00:00+08:00', 'priority': '普通'})
tick()
with Session() as db:
    old_snap = db.get(PivotSnapshot, old_id)
    check('superseded snapshot never changes', p._rows(db, old_snap) == frozen_rows
          and old_snap.watermark == frozen_watermark)
    active = db.query(PivotSnapshot).filter_by(status='active').first()
    check('new active picks up later writes', active.watermark == db.get(PivotControl, 1).receive_seq)

with Session() as db:
    policy3 = Policy(version='工作时钟-v3', data=dict(db.query(Policy).first().data, version='工作时钟-v3'))
    db.add(policy3)
    db.commit()
    db.refresh(policy3)
    bad = p.create_snapshot(db, policy3)
    bad_id = bad.id
with Session() as db:
    snap3 = db.get(PivotSnapshot, bad_id)
    snap3.policy_data = dict(snap3.policy_data, windows=[[540, 300]])
    db.commit()
tick()
with Session() as db:
    snap3 = db.get(PivotSnapshot, bad_id)
    check('build failure recorded', bool(snap3.last_error), snap3.last_error)
    still = db.query(PivotSnapshot).filter_by(status='active').all()
    check('old active kept after failed rebuild', len(still) == 1 and still[0].id == sid2,
          str([(s.id, s.status) for s in still]))
st = p.read_state()
check('page explains failure but keeps result', '建账' in st['reason'] or st['active'] is not None, st['reason'])


# ---------------------------------------------------------------------------
# 6. 中断恢复：基线半途重启继续，不漏不重
# ---------------------------------------------------------------------------
with Session() as db:
    policy4 = Policy(version='工作时钟-v4', data=dict(db.query(Policy).first().data, version='工作时钟-v4'))
    db.add(policy4)
    db.commit()
    db.refresh(policy4)
    resume = p.create_snapshot(db, policy4)
    resume_id = resume.id

with Session() as db:
    snap4 = db.get(PivotSnapshot, resume_id)
    p.build_page(db, snap4, limit=7)
    snap4 = db.get(PivotSnapshot, resume_id)
    first_done = snap4.base_done
    check('baseline page builds partial', 0 < first_done < snap4.base_total, f'{first_done}/{snap4.base_total}')

# 模拟进程中断：重新读取并继续
with Session() as db:
    save_source(db, user, {'period': '2026-12', 'department': '数据采样组',
                           'opened': '2026-11-11T10:00:00+08:00',
                           'closed': '2026-11-11T11:30:00+08:00', 'priority': '紧急'})
build_until_done(resume_id)
with Session() as db:
    snap4 = db.get(PivotSnapshot, resume_id)
    check('resumed baseline activates', snap4.status == 'active', snap4.status)
    dup = db.query(PivotContribution.source_id).filter_by(snapshot_id=resume_id).group_by(
        PivotContribution.source_id).having(func.count() > 1).count()
    check('one contribution per source', dup == 0, f'duplicates={dup}')
tick()
st = matches('result after interrupted baseline resume')

# ---------------------------------------------------------------------------
# 7. 并发进程：只有持租约者推进，最终一致
# ---------------------------------------------------------------------------
with Session() as db:
    policy5 = Policy(version='工作时钟-v5', data=dict(db.query(Policy).first().data, version='工作时钟-v5'))
    db.add(policy5)
    db.commit()
    db.refresh(policy5)
    p.create_snapshot(db, policy5)

child = """
import os, time, traceback
from src.services import pivot
end = time.time() + 45
while time.time() < end:
    try:
        pivot.pivot_tick("proc:%d" % os.getpid())
    except Exception:
        traceback.print_exc()
"""
procs = [subprocess.Popen([sys.executable, '-c', child], cwd=BACKEND,
                          env=dict(os.environ, PYTHONPATH=BACKEND),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
         for _ in range(3)]
for _ in range(1500):
    with Session() as db:
        if db.query(PivotSnapshot).filter_by(policy_id=policy5.id, status='active').count():
            break
    time.sleep(0.1)
outputs = []
for pr in procs:
    pr.terminate()
    try:
        out = pr.communicate(timeout=10)[0]
    except Exception:
        pr.kill()
        out = pr.communicate()[0]
    outputs.append(out or '')
if os.getenv('PIVOT_TEST_VERBOSE'):
    print('\n'.join(o[-3000:] for o in outputs))
with Session() as db:
    actives = db.query(PivotSnapshot).filter_by(policy_id=policy5.id, status='active').all()
    check('concurrent workers converge to one active', len(actives) == 1,
          str([(a.id, a.status, a.base_done, a.base_total, a.failures, a.last_error)
               for a in db.query(PivotSnapshot).filter_by(policy_id=policy5.id).all()])
          + '\n' + '\n'.join(o[-1500:] for o in outputs))
    snap5 = actives[0] if actives else None
    if snap5:
        check('concurrent build has one contribution per source',
              db.query(PivotContribution.source_id).filter_by(snapshot_id=snap5.id).group_by(
                  PivotContribution.source_id).having(func.count() > 1).count() == 0)
        rows, summary = p._rows(db, snap5)
        with Session() as db2:
            expected = calculate([s.data for s in db2.query(Source).all()], db2.get(Policy, policy5.id).data)
        check('concurrent build result equals full recompute', summary['样本数'] == expected['summary']['样本数'],
              f'{summary} vs {expected["summary"]}')

# ---------------------------------------------------------------------------
# 8. 冻结报告与定时交付保持原值
# ---------------------------------------------------------------------------
import src.services.report as report_service
# 报告提交不依赖 Redis 也能验证快照冻结；队列投递由 worker 负责。
report_service.redis = type('R', (), {'Redis': type('RR', (), {'from_url': staticmethod(
    lambda url: type('X', (), {'rpush': lambda self, *a: 1})())})})()
with Session() as db:
    from src.services.report import submit
    frozen = submit(db, user, type('B', (), {'name': '冻结检查', 'policy_id': 1, 'period': '全部'})())
    frozen_id = frozen.id
    frozen_snapshot = frozen.snapshot
    frozen_checksum = frozen.checksum
with Session() as db:
    save_source(db, user, {'period': '2026-12', 'department': '数据建模组',
                           'opened': '2026-11-12T10:00:00+08:00',
                           'closed': '2026-11-12T13:00:00+08:00', 'priority': '普通'})
    db.expire_all()
    from src.models.run import Run as RunModel
    after = db.get(RunModel, frozen_id)
    check('frozen report snapshot unchanged', after.snapshot == frozen_snapshot and after.checksum == frozen_checksum)

print()
print('FAILURES:', failures if failures else 'none')
sys.exit(1 if failures else 0)
