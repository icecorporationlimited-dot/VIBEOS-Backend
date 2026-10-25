"""Hard output contract: story-only metadata, basic image/readability checks."""
from PIL import Image
import io


def validate(png, copy, photo_count, plan=None):
    im = Image.open(io.BytesIO(png))
    errors = []
    if im.size != (1080, 1920):
        errors.append("wrong_story_dimensions")
    if not 3 <= photo_count <= 5:
        errors.append("photo_count_out_of_range")
    if len(copy.get("text", "")) > 100:
        errors.append("copy_too_long")
    if not str(copy.get("text", "")).strip():
        errors.append("copy_empty")
    if plan:                      # slot plan from the renderer: safe zone, >= 24 px gaps, fixed ratios, orientation match
        from .collage_layout_planner import check_plan
        errors.extend(check_plan(plan))
    meta = {"content_type": "story_collage", "destination": "story", "feed_eligible": False, "post_eligible": False}
    if plan:
        meta["layout"] = {"name": plan.get("layout"), "contain_slots": plan.get("contain_slots"),
                          "slots": [{k: s.get(k) for k in ("x", "y", "w", "h", "ratio_name", "orientation", "asset_id", "mode")} for s in plan["slots"]]}
    return {"valid": not errors, "errors": errors, "metadata": meta}
