"""Minimal in-memory stand-in for pymongo/gridfs/bson — just enough for jobs.py,
content_buffer.py and worker.py tests (no MongoDB needed)."""
import copy
import io
import sys
import threading
import types
import uuid
from datetime import datetime, timezone

_lock = threading.RLock()


def _get(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _match_val(have, cond):
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        for op, v in cond.items():
            if op == "$in" and have not in v: return False
            if op == "$ne" and have == v: return False
            if op == "$lt" and not (have is not None and have < v): return False
            if op == "$lte" and not (have is not None and have <= v): return False
            if op == "$gt" and not (have is not None and have > v): return False
            if op == "$gte" and not (have is not None and have >= v): return False
            if op == "$exists" and (have is not None) != v: return False
        return True
    if isinstance(have, list) and not isinstance(cond, list):
        return cond in have
    return have == cond


def _match(doc, flt):
    for k, v in (flt or {}).items():
        if k == "$or":
            if not any(_match(doc, f) for f in v): return False
        elif k == "$and":
            if not all(_match(doc, f) for f in v): return False
        elif not _match_val(_get(doc, k), v):
            return False
    return True


def _apply(doc, upd):
    for k, v in upd.get("$set", {}).items():
        cur = doc
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    for k, v in upd.get("$unset", {}).items():
        doc.pop(k, None)
    for k, v in upd.get("$inc", {}).items():
        doc[k] = doc.get(k, 0) + v
    for k, v in upd.get("$push", {}).items():
        doc.setdefault(k, []).append(v)


class Result:
    def __init__(self, n): self.matched_count = self.modified_count = self.deleted_count = n


class Collection:
    def __init__(self, db, name):
        self.database, self.name, self.docs = db, name, []

    def create_index(self, *a, **k): return "idx"
    def insert_one(self, doc):
        with _lock: self.docs.append(copy.deepcopy(doc))
    def find_one(self, flt=None, projection=None, sort=None):
        with _lock:
            rows = [d for d in self.docs if _match(d, flt)]
            if sort: 
                for key, direction in reversed(sort): rows.sort(key=lambda d: d.get(key), reverse=direction < 0)
            return copy.deepcopy(rows[0]) if rows else None
    def find(self, flt=None, projection=None):
        with _lock: return [copy.deepcopy(d) for d in self.docs if _match(d, flt)]
    def count_documents(self, flt): return len(self.find(flt))
    def update_one(self, flt, upd, upsert=False):
        with _lock:
            for d in self.docs:
                if _match(d, flt):
                    _apply(d, upd); return Result(1)
            if upsert:
                d = {k: v for k, v in flt.items() if not k.startswith("$")}
                _apply(d, upd); self.docs.append(d); return Result(1)
            return Result(0)
    def update_many(self, flt, upd):
        with _lock:
            n = 0
            for d in self.docs:
                if _match(d, flt): _apply(d, upd); n += 1
            return Result(n)
    def delete_one(self, flt):
        with _lock:
            for i, d in enumerate(self.docs):
                if _match(d, flt): del self.docs[i]; return Result(1)
            return Result(0)
    def find_one_and_update(self, flt, upd, sort=None, return_document=False):
        with _lock:
            rows = [d for d in self.docs if _match(d, flt)]
            if sort:
                for key, direction in reversed(sort): rows.sort(key=lambda d: d.get(key), reverse=direction < 0)
            if not rows: return None
            _apply(rows[0], upd)
            return copy.deepcopy(rows[0])


class DB:
    def __init__(self):
        self._cols, self.blobs = {}, {}
    def __getitem__(self, name):
        return self._cols.setdefault(name, Collection(self, name))


def install(db):
    """Register fake `gridfs` and `bson` modules bound to `db`."""
    bson = types.ModuleType("bson"); bson.ObjectId = lambda s: str(s)

    class GridFS:
        def __init__(self, database, collection="fs"):
            self.store = database.blobs.setdefault(collection, {})
            self.files = database[collection + ".files"]
        def put(self, data, filename=""):
            oid = uuid.uuid4().hex[:24]
            self.store[oid] = bytes(data)
            self.files.insert_one({"_id": oid, "filename": filename, "uploadDate": datetime.now(timezone.utc)})
            return oid
        def get(self, oid):
            return io.BytesIO(self.store[oid])
        def delete(self, oid):
            self.store.pop(oid)
            self.files.delete_one({"_id": oid})

    grid = types.ModuleType("gridfs"); grid.GridFS = GridFS
    sys.modules["bson"], sys.modules["gridfs"] = bson, grid
