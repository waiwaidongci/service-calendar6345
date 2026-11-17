from fastapi import APIRouter, Depends, HTTPException
from src.config.database import database
from src.controllers.auth import current, admin
from src.models.source import Policy
from src.schemas.requests import PivotRebuildInput
from src.services import pivot

router = APIRouter()


@router.get('/pivot')
def pivot_state(policy_id: int | None = None, u=Depends(current)):
    return pivot.read_state(policy_id)


@router.post('/pivot/rebuild')
def rebuild(b: PivotRebuildInput, db=Depends(database), u=Depends(admin)):
    """按同一口径重新建账。旧结果在新快照追平水位并原子切换前持续可用。"""
    policy = db.get(Policy, b.policy_id)
    if not policy:
        raise HTTPException(404, '口径版本不存在')
    snap, created = pivot.create_snapshot(db, policy, return_created=True)
    return {'snapshot_id': snap.id, 'status': snap.status, 'already_running': not created}
