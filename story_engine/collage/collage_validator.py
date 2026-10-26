"""Hard output contract: destination metadata (story XOR post), basic image/readability checks."""
from PIL import Image
import io


def validate(png, copy, photo_count, plan=None, destination="story"):
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
    is_post = destination == "post"
    meta = {"content_type": "post_collage" if is_post else "story_collage", "destination": "post" if is_post else "story",
            "feed_eligible": is_post, "post_eligible": is_post}
    if plan:
        meta["layout"] = {"name": plan.get("layout"), "contain_slots": plan.get("contain_slots"),
                          "slots": [{k: s.get(k) for k in ("x", "y", "w", "h", "ratio_name", "orientation", "asset_id", "mode")} for s in plan["slots"]]}
    return {"valid": not errors, "errors": errors, "metadata": meta}
