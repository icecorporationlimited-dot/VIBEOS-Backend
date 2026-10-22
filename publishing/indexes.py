"""publishing/indexes.py - index creation that survives a changed definition.

MongoDB refuses `create_index` when an index with the same name/keys already
exists with different options (IndexOptionsConflict 85 / IndexKeySpecsConflict
86). Without this, one old definition crashes the whole service at start-up.
`ensure_index` repairs exactly that case: it drops the same-key index and
recreates it with the requested options. Any other error is raised unchanged.
"""
import logging

from pymongo.errors import OperationFailure

logger = logging.getLogger(__name__)

_CONFLICT_CODES = (85, 86)   # IndexOptionsConflict, IndexKeySpecsConflict
_NAMESPACE_NOT_FOUND = (26, 27)   # IndexNotFound: someone else already dropped it


def _key_doc(keys):
    if isinstance(keys, str):
        return {keys: 1}
    return {field: direction for field, direction in keys}


def ensure_index(col, keys, **options):
    try:
        return col.create_index(keys, **options)
    except OperationFailure as exc:
        if exc.code not in _CONFLICT_CODES or not hasattr(col, "list_indexes"):
            raise
    want = _key_doc(keys)
    for index in list(col.list_indexes()):
        if index.get("name") != "_id_" and dict(index.get("key", {})) == want:
            logger.warning("Replacing index %s on %s (definition changed): %s -> %s",
                           index["name"], getattr(col, "name", "?"), dict(index), options)
            try:
                col.drop_index(index["name"])
            except OperationFailure as exc:
                if exc.code not in _NAMESPACE_NOT_FOUND:
                    raise
    return col.create_index(keys, **options)
