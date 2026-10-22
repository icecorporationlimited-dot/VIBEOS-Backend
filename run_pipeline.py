from memory import db
from story_engine import db as se, rank, context, writer, visuals, layouts, render
from story_engine.discovery import run

se.ensure_indexes(db)
layouts.sync(db)                                   # templates -> se_layouts

run.save(db, run.discover_all())                   # 1-2 discovery
rank.process(db)                                   # 3 verify + score
context.process(db, llm)                           # 4 mood + angles
writer.process(db, llm)                            # 5 copy
visuals.process(db)                                # 6 image
layouts.process(db)                                # 7 layout
with render.PlaywrightRenderer() as r:             # 8 PNG + upload
    print(render.process(db, r, render.R2Uploader()))