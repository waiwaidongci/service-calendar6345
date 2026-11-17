import time,traceback
from pathlib import Path
import redis
from sqlalchemy import or_
from src.config.database import Base,engine,Session
from src.config.settings import REDIS_URL
from src.models import identity,source,run,pivot
from src.models.run import Run
from src.services.calculation import calculate
from src.services.delivery import tick
from src.services import pivot as pivot_service
Base.metadata.create_all(engine)
pivot_service.ensure_control()
r=redis.Redis.from_url(REDIS_URL)
owner=pivot_service.own_name()

STALE_AFTER=120

def recover():
 """中断恢复：只把长时间停在 running 的任务重新排队；重复投递由状态判断吸收。"""
 cutoff=time.time()-STALE_AFTER
 with Session() as db:
  stuck=db.query(Run).filter(Run.status=='running',or_(Run.started==None,Run.started<cutoff)).all()
  for job in stuck:
   job.status='queued';job.started=None;job.error=None
   r.rpush('report-jobs',job.id)
  db.commit()

def process(item):
 with Session() as db:
  # 原子认领：多进程/重复投递时只有一个执行者，状态已不是 queued 的投递直接忽略。
  claimed=db.query(Run).filter(Run.id==int(item),Run.status=='queued').update(
   {'status':'running','started':time.time()},synchronize_session=False)
  db.commit()
  if not claimed: return
  job=db.get(Run,int(item))
  try:
   job.result=calculate(job.snapshot['sources'],job.snapshot['policy']);job.status='completed';job.error=None
  except Exception as exc:
   job.status='failed';job.error=str(exc);traceback.print_exc()
  db.commit()

recover()
while True:
 Path('/tmp/worker-heartbeat').write_text(str(time.time()))
 try:
  item=r.lpop('report-jobs')
  if item: process(item)
  with Session() as db: tick(db)
 except Exception: traceback.print_exc()
 try: pivot_service.pivot_tick(owner)
 except Exception: traceback.print_exc()
 time.sleep(1)
