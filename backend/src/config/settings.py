import os,json
from pathlib import Path
CONFIG_PATH=os.getenv('PROJECT_JSON') or ('/app/project.json' if Path('/app/project.json').exists() else str(Path(__file__).resolve().parents[2]/'project.json'))
CONFIG=json.loads(Path(CONFIG_PATH).read_text())
DATABASE_URL=os.environ['DATABASE_URL']
REDIS_URL=os.getenv('REDIS_URL','redis://redis:6379/0')
SECRET=os.environ['JWT_SECRET']
