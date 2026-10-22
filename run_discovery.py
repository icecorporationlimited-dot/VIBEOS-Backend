from memory import db
from story_engine import db as se
from story_engine.discovery import run

se.ensure_indexes(db)          # safe, baar-baar chalne se kuch nahi bigadta
cands = run.discover_all()
print(len(run.save(db, cands)), "trends saved")