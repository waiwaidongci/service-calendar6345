import os,logging,threading,time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.config.database import Base,engine
from src.config.settings import CONFIG,REDIS_URL
from src.models import identity,source,run,pivot
from src.routes import auth,source as source_routes,reports,deliveries,audits,pivot as pivot_routes
from src.services import pivot as pivot_service
from src.middlewares.errors import Errors
from src.middlewares.rate import RateLimit
def _pivot_loop():
 owner=pivot_service.own_name()
 while True:
  try: pivot_service.pivot_tick(owner)
  except Exception: logging.exception('增量透视处理失败，稍后重试')
  time.sleep(1)

@asynccontextmanager
async def lifespan(app):
 Base.metadata.create_all(engine)
 pivot_service.ensure_control()
 if os.getenv('SEED_DEMO')=='true':
  try:
   from scripts.seed_demo import seed
   seed()
  except Exception: logging.exception('seed失败，可手动重试')
 # 后端进程也参与推进（与 worker 通过数据库租约互斥），页面在 worker 缺席时仍能看到进展。
 if os.getenv('PIVOT_INLINE','true').lower()!='false':
  threading.Thread(target=_pivot_loop,daemon=True,name='pivot-tick').start()
 yield
app=FastAPI(title=CONFIG['title'],lifespan=lifespan)
app.add_middleware(Errors);app.add_middleware(RateLimit)
for module in (auth,source_routes,reports,deliveries,audits,pivot_routes): app.include_router(module.router)
@app.get('/health')
def health():
 from sqlalchemy import text
 import redis
 with engine.connect() as c: c.execute(text('SELECT 1'))
 redis.Redis.from_url(REDIS_URL).ping()
 return {'status':'healthy'}
@app.get('/healthz/pivot')
def pivot_health():
 return pivot_service.status_summary()
@app.get('/meta')
def meta(): return CONFIG
