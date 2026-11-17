from fastapi import HTTPException
from src.models.source import Source, Policy, Period
from src.services.validation import validate_source, validate_policy
from src.services.audit import log
from src.services import pivot
import uuid


def _lock_source(db, id):
    obj = db.get(Source, id, with_for_update=True)
    if not obj:
        raise HTTPException(404, '底稿不存在')
    return obj


def save_source(db, user, data, id=None):
    validate_source(data)
    # 先取控制行写锁：所有底稿写入在同一串行点排队，事件序号才等于提交顺序。
    pivot.begin_write(db)
    if id:
        obj = _lock_source(db, id)
        obj.data = data
        obj.revision = int(obj.revision or 0) + 1
        action = 'source.update'
    else:
        obj = Source(natural_key='user-' + uuid.uuid4().hex, data=data, revision=1)
        db.add(obj)
        db.flush()
        action = 'source.create'
    # 变更事件与底稿写入同事务提交；seq 在控制行锁内分配，保证接收水位无空洞。
    seq = pivot.record_change(db, obj.id, 'upsert', data, user.email)
    if not db.query(Period).filter_by(name=data['period']).first():
        db.add(Period(name=data['period'], description='业务来源自动登记期间'))
    log(db, user.email, action, f'{obj.natural_key}#{obj.revision}@seq{seq}')
    db.commit()
    return obj


def delete_source(db, user, id):
    pivot.begin_write(db)
    obj = _lock_source(db, id)
    obj.revision = int(obj.revision or 0) + 1
    seq = pivot.record_change(db, obj.id, 'delete', None, user.email)
    log(db, user.email, 'source.delete', f'{obj.natural_key}@seq{seq}')
    db.delete(obj)
    db.commit()
    return id


def save_policy(db, user, data):
    validate_policy(data)
    if db.query(Policy).filter_by(version=data['version']).first():
        raise HTTPException(409, '版本名已存在，请另建版本保留历史')
    obj = Policy(version=data['version'], data=data)
    db.add(obj)
    log(db, user.email, 'policy.version', data['version'])
    db.commit()
    # 新口径需要独立建账；旧口径的 active 快照保持原值服务。
    pivot.create_snapshot(db, obj)
    return obj
