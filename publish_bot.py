"""
publish_bot.py — Telegram commands for Phase 2 (Instagram publishing).

  /connect     link an Instagram professional account (official OAuth)
  /post <id>   publish ONE specific photo now, as Post or Story
  /igstatus    account, token expiry and recent publish jobs
  /disconnect  forget the stored Instagram token

Phase 3 (autopilot):
  /schedule    put the latest ready photo in the next free calendar slot
  /queue       pending jobs (id, time, status)
  /cancel <id> cancel a pending job        /release <id>  let a held job through
  /pause       emergency stop for everything scheduled   /resume  continue
  /autopilot   daily limit, quiet hours, formats, blocked topics, auto-scheduling

Everything is optional: if the Meta settings are not filled in settings.py the bot keeps
working and these commands explain what is missing.
"""
import asyncio
import logging
from datetime import datetime, timezone

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

from publishing import autopilot as autopilot_mod
from publishing.accounts import AccountStore
from publishing.autopilot import AutopilotStore
from publishing.policy import PolicyEngine, get_zone, make_tz_for
from publishing.scheduler import Worker
from publishing.slots import Calendar
from publishing.config import load_config, missing_settings
from publishing.crypto import TokenCipher
from publishing.graph import GraphClient
from publishing.oauth import InstagramOAuth
from publishing.server import start_server
from publishing.oidc import OIDCLogin
from publishing.web_auth import WebSessionStore
from publishing.web_routes import WebLoginRoutes
from publishing.web_instagram import WebInstagramConnect
from publishing.web_config import load_web_auth_settings
from publishing.web_studio import WebStudio
from publishing.web_pipeline import WebIngest, WebPipeline
from publishing.service import PublishService, PublishError
from publishing.accounts import NotConnected

logger = logging.getLogger(__name__)

STATUS_ICON = {"queued": "🕒", "running": "⏳", "retry": "🔁", "published": "✅",
               "failed": "❌", "needs_reauth": "🔑", "needs_attention": "⚠️",
               "held": "⏸", "cancelled": "🚫"}


class Runtime:
    def __init__(self, cfg, oauth, accounts, service, httpd=None,
                 autopilot=None, calendar=None, worker=None, tz_for=None,
                 web_ingest=None):
        self.cfg, self.oauth, self.accounts = cfg, oauth, accounts
        self.service, self.httpd = service, httpd
        self.web_ingest = web_ingest
        self.autopilot, self.calendar, self.worker = autopilot, calendar, worker
        self.tz_for = tz_for


def send_telegram(bot_token, tg_id, text):
    requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                  json={"chat_id": tg_id, "text": text}, timeout=15)


def _local(dt, tz_name):
    """'Tue 14 Oct, 19:30' in the user's timezone."""
    if dt is None:
        return "—"
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(get_zone(tz_name)).strftime("%a %d %b, %H:%M")


def build_runtime(db, content_buffer, bot_token):
    """Create the publishing stack, or None if Meta settings are missing."""
    cfg = load_config()
    if cfg is None:
        logger.warning("Instagram publishing disabled; missing: %s",
                       ", ".join(missing_settings()))
        return None
    http = requests.Session()
    graph = GraphClient(http, cfg.graph_version)
    oauth = InstagramOAuth(cfg, http)
    accounts = AccountStore(db["ig_accounts"], TokenCipher(cfg.encryption_key),
                            oauth, graph, cfg.refresh_before_days)
    service = PublishService(db["publish_jobs"], db["publish_events"],
                             content_buffer, accounts, graph, cfg)
    accounts.ensure_indexes()
    service.ensure_indexes()

    def notify(tg_id, acct):
        send_telegram(bot_token, tg_id,
                      f"✅ Instagram connected: @{acct.get('username')}\n"
                      "Use /post to publish a ready photo, or /schedule to queue it.")

    # ---- Phase 3: policy, calendar, worker ----
    import memory   # shared Mongo handle + per-user timezone
    autopilot = AutopilotStore(db["autopilot"])
    autopilot.ensure_indexes()
    # Telegram users keep their saved zone; web owners use their autopilot timezone.
    tz_for = make_tz_for(autopilot, lambda tg: memory.get_preferences(tg)["timezone"])
    service.gate = PolicyEngine(db["publish_jobs"], content_buffer, autopilot, tz_for)
    calendar = Calendar(db["publish_jobs"], autopilot, tz_for)
    from context_engine.planner import ContextPlanner as ContentPlanner   # drop-in upgrade
    import settings as app_settings
    planner = ContentPlanner(
        content_buffer, calendar,
        api_key=getattr(app_settings, "GROQ_API_KEY", ""),
        model=getattr(app_settings, "VISION_MODEL", "qwen/qwen3.8-27b"),
        tz_for=tz_for,
    )
    from engagement.openrouter_ai import OpenRouterAI
    engagement_ai = OpenRouterAI(
        getattr(app_settings, "OPENROUTER_API_KEY", ""),
        getattr(app_settings, "OPENROUTER_MODEL", "openai/gpt-4o-mini"))
    engagement_events = db["engagement_events"]
    # Web publishing is opt-in (WEB_PUBLISHING=1). Until then web-owned jobs are
    # never claimed, exactly as before.
    web_publishing = str(getattr(app_settings, "WEB_PUBLISHING", "")).strip().lower() in (
        "1", "true", "yes", "on")
    if web_publishing:
        logger.warning("Web publishing is ENABLED: the worker will post for web-owned accounts.")
    worker = Worker(service, accounts, autopilot, calendar, content_buffer,
                    notify=lambda tg, text: send_telegram(bot_token, tg, text),
                    refresh_days=cfg.refresh_before_days, planner=planner,
                    trend_every=600.0, engagement_ai=engagement_ai,
                    engagement_events=engagement_events, web_owners=web_publishing)
    worker.story_db = db     # lets the worker cancel story-library jobs whose trend expired (step 11)

    web_login = None
    web_auth_cfg = load_web_auth_settings()
    if web_auth_cfg:
        web_users = db["web_users"]
        oidc = OIDCLogin(
            web_auth_cfg["issuer"], web_auth_cfg["client_id"],
            web_auth_cfg["client_secret"], cfg.public_base_url.rstrip("/") + "/oidc/callback",
            db["oidc_login_transactions"], web_users,
        )
        sessions = WebSessionStore(db["web_sessions"], web_users)
        oidc.ensure_indexes()
        sessions.ensure_indexes()
        instagram_connect = WebInstagramConnect(
            cfg, oauth, accounts, sessions, db["web_ig_oauth_transactions"]
        )
        instagram_connect.ensure_indexes()
        web_login = WebLoginRoutes(cfg, oidc, sessions, instagram_connect, accounts)

    web_studio = web_ingest = None
    if web_login is not None:
        def _groq():
            from groq import Groq
            return Groq(api_key=getattr(app_settings, "GROQ_API_KEY", ""))
        pipeline = WebPipeline(
            content_buffer, _groq,
            getattr(app_settings, "VISION_MODEL", "qwen/qwen3.8-27b"),
            language=getattr(app_settings, "CAPTION_LANGUAGE", "english"),
            occasion_hint=getattr(planner, "occasion_hint", None))
        web_ingest = WebIngest(content_buffer, autopilot, pipeline)
        web_studio = WebStudio(
            cfg, sessions, content_buffer, autopilot, accounts=accounts,
            service=service, ingest=web_ingest,
            posting_engine=getattr(worker, "handles_web_owners", False),
            planner=planner, story_db=db)

    httpd = start_server(
        cfg, oauth, accounts, content_buffer.blobs, notify, web_login=web_login,
        content_buffer=content_buffer, web_studio=web_studio,
    )
    return Runtime(cfg, oauth, accounts, service, httpd,
                   autopilot=autopilot, calendar=calendar, worker=worker, tz_for=tz_for,
                   web_ingest=web_ingest)


def start_worker(rt):
    if rt is not None and rt.worker is not None:
        rt.worker.start()
    if rt is not None and rt.web_ingest is not None:
        rt.web_ingest.start()


def shutdown(rt):
    """Stop the worker and web server cleanly (called from the bot's shutdown hook)."""
    if rt is None:
        return
    if rt.worker is not None:
        rt.worker.stop()
    if rt.web_ingest is not None:
        rt.web_ingest.stop()
    if rt.httpd is not None:
        rt.httpd.shutdown()


def register(app, rt, content_buffer):
    """Add handlers. Must be called BEFORE the generic CallbackQueryHandler."""

    async def _disabled(update):
        await update.effective_message.reply_text(
            "Instagram publishing isn't set up yet. Fill these in settings.py: "
            + ", ".join(missing_settings()))

    async def connect(update, context):
        if rt is None:
            return await _disabled(update)
        url = rt.oauth.authorize_url(update.effective_user.id)
        await update.message.reply_text(
            "🔗 Connect your Instagram professional (Business/Creator) account.\n"
            "Keep all permissions ticked. The link works for 10 minutes.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Connect Instagram", url=url)]]))

    async def disconnect(update, context):
        if rt is None:
            return await _disabled(update)
        await asyncio.to_thread(rt.accounts.disconnect, update.effective_user.id)
        await update.message.reply_text(
            "Disconnected. Your token was deleted from VibeOS. You can also "
            "remove VibeOS in Instagram → Settings → Apps and websites.")

    async def igstatus(update, context):
        if rt is None:
            return await _disabled(update)
        uid = update.effective_user.id
        acct = await asyncio.to_thread(rt.accounts.get, uid)
        if not acct or acct.get("status") == "disconnected":
            return await update.message.reply_text("Not connected. Use /connect.")
        exp = acct["token_expires_at"]
        days = (exp.replace(tzinfo=exp.tzinfo or timezone.utc)
                - datetime.now(timezone.utc)).days
        lines = [f"📸 @{acct.get('username')} ({acct.get('account_type')})",
                 f"• login: {acct['status']}, token valid ~{max(days, 0)} more days"]
        jobs = await asyncio.to_thread(rt.service.recent_jobs, uid)
        for j in jobs:
            line = f"{STATUS_ICON.get(j['status'], '•')} {j['format']} — {j['status']}"
            if j.get("permalink"):
                line += f"\n   {j['permalink']}"
            elif j.get("last_error"):
                line += f"\n   {j['last_error'][:120]}"
            lines.append(line)
        await update.message.reply_text("\n".join(lines))

    async def post(update, context):
        """/post <id>: publish ONE specific photo now (Post or Story only).

        <id> is a job id from /queue, or the start of a ready photo's id.
        """
        if rt is None:
            return await _disabled(update)
        uid = update.effective_user.id
        acct = await asyncio.to_thread(rt.accounts.get, uid)
        if not acct or acct.get("status") != "active":
            return await update.message.reply_text(
                "Connect (or reconnect) Instagram first: /connect")
        arg = (context.args[0] if context.args else "").strip().lower()
        if not arg:
            return await update.message.reply_text(
                "Which photo? Use /post <id> with an id from /queue, "
                "e.g. /post ac5021")

        asset_id = None
        jobs = await asyncio.to_thread(rt.service.find_by_prefix, uid, arg)
        jobs = [j for j in jobs if j["status"] in ("queued", "retry", "held",
                                                   "needs_reauth", "published")]
        if len(jobs) > 1:
            return await update.message.reply_text(
                "That id matches several jobs, use more characters.")
        if jobs:
            job = jobs[0]
            if job["format"] not in ("photo", "story") or len(job["asset_ids"]) != 1:
                return await update.message.reply_text(
                    f"Job {job['_id'][:6]} is a {job['format']}, /post only handles "
                    "single photos.")
            if job["status"] == "published":
                return await update.message.reply_text(
                    f"Job {job['_id'][:6]} is already published.")
            asset_id = job["asset_ids"][0]
        else:
            ready = await asyncio.to_thread(content_buffer.list_photo_ready, uid)
            found = [a for a in ready if str(a["_id"]).lower().startswith(arg)]
            if len(found) > 1:
                return await update.message.reply_text(
                    "That id matches several photos, use more characters.")
            if found:
                asset_id = found[0]["_id"]
        if asset_id is None:
            return await update.message.reply_text(
                "No queued job or ready photo with that id. Check /queue.")

        buttons = [[InlineKeyboardButton("📸 Post", callback_data=f"ig:p:{asset_id}"),
                    InlineKeyboardButton("📱 Story", callback_data=f"ig:s:{asset_id}")],
                   [InlineKeyboardButton("Cancel", callback_data="ig:x")]]
        try:
            data = await asyncio.to_thread(content_buffer.get_media, asset_id, "post")
            asset = await asyncio.to_thread(content_buffer.assets.find_one, {"_id": asset_id})
        except KeyError:
            return await update.message.reply_text("That photo no longer exists.")
        cap = ((asset or {}).get("creative", {}).get("caption") or "")[:300]
        await update.message.reply_photo(
            photo=data, caption=f"Publish this photo to @{acct['username']} now?\n\n{cap}",
            reply_markup=InlineKeyboardMarkup(buttons))

    async def _run_and_report(query, uid, job):
        note = "(already queued earlier) " if job.get("duplicate") else ""
        await query.edit_message_caption(caption=f"⏳ {note}Publishing to Instagram…")
        done = await asyncio.to_thread(rt.service.run, job["_id"])
        st = done["status"]
        if st == "published":
            text = "✅ Published!" + (f"\n{done['permalink']}" if done.get("permalink") else "")
            markup = None
        else:
            hints = {
                "retry": "Instagram had a temporary problem. Tap Retry in a minute.",
                "failed": "Couldn't publish. " + (done.get("last_error") or ""),
                "needs_reauth": "Instagram login expired. Use /connect, then Retry.",
                "needs_attention": done.get("last_error") or "Please check your profile.",
                "held": f"Held by your autopilot rules: {done.get('hold_reason')}.\n"
                        f"/release {done['_id'][:6]} to post anyway.",
                "queued": "Waiting for the next allowed time. See /queue.",
                "cancelled": "This job was cancelled.",
            }
            text = f"{STATUS_ICON.get(st, '•')} {hints.get(st, st)}"
            markup = None
            if st in ("retry", "failed", "needs_reauth"):
                markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                    "🔁 Retry", callback_data=f"ig:r:{done['_id']}")]])
        await query.edit_message_caption(caption=text, reply_markup=markup)

    async def buttons(update, context):
        query = update.callback_query
        await query.answer()
        if rt is None:
            return
        uid = query.from_user.id
        parts = query.data.split(":", 2)
        action, arg = parts[1], (parts[2] if len(parts) > 2 else "")

        if action == "x":
            return await query.edit_message_caption(caption="Cancelled.")
        try:
            if action == "r":
                ok = await asyncio.to_thread(rt.service.retry, arg, uid)
                if not ok:
                    return await query.edit_message_caption(
                        caption="Nothing to retry for this one.")
                job = {"_id": arg}
            elif action in ("p", "s"):
                fmt = "photo" if action == "p" else "story"
                job = await asyncio.to_thread(rt.service.publish_now, uid, arg, fmt)
            else:
                return
        except (PublishError, NotConnected) as exc:
            return await query.edit_message_caption(caption=f"❌ {exc}")
        await _run_and_report(query, uid, job)

    # ---------------- Phase 3: autopilot controls ----------------
    async def _need_ap(update):
        if rt is None or rt.autopilot is None:
            await _disabled(update)
            return False
        return True

    async def pause(update, context):
        if not await _need_ap(update):
            return
        uid = update.effective_user.id
        await asyncio.to_thread(rt.autopilot.set_paused, uid, True)
        n = len(await asyncio.to_thread(rt.service.queue, uid))
        await update.message.reply_text(
            f"⏸ Autopilot paused. Nothing scheduled will be published "
            f"({n} job(s) waiting). /resume to continue.")

    async def resume(update, context):
        if not await _need_ap(update):
            return
        await asyncio.to_thread(rt.autopilot.set_paused, update.effective_user.id, False)
        await update.message.reply_text("▶️ Autopilot resumed. See /queue.")

    async def queue(update, context):
        if not await _need_ap(update):
            return
        uid = update.effective_user.id
        jobs = await asyncio.to_thread(rt.service.queue, uid)
        s = await asyncio.to_thread(rt.autopilot.get, uid)
        tz = await asyncio.to_thread(rt.tz_for, uid)
        head = "⏸ PAUSED\n" if s["paused"] else ""
        if not jobs:
            return await update.message.reply_text(head + "📭 Nothing queued. Use /schedule.")
        lines = []
        for j in jobs[:15]:
            when = _local(j.get("scheduled_at") or j.get("next_attempt_at"), tz)
            line = f"{STATUS_ICON.get(j['status'], '•')} {j['_id'][:6]} · {j['format']} · {when} · {j['status']}"
            extra = j.get("hold_reason") or j.get("defer_reason") if j["status"] in ("held", "queued") \
                else j.get("last_error")
            if extra:
                line += f"\n   {str(extra)[:90]}"
            lines.append(line)
        await update.message.reply_text(head + "\n".join(lines) +
                                        "\n\n/post <id> (publish now) · /cancel <id> · /release <id>")

    async def _one_job(update, context):
        uid = update.effective_user.id
        arg = context.args[0] if context.args else ""
        found = await asyncio.to_thread(rt.service.find_by_prefix, uid, arg)
        if len(found) != 1:
            await update.message.reply_text(
                "Give the job id from /queue, e.g. /cancel a1b2c3." if not found
                else "That id matches several jobs — use more characters.")
            return None
        return found[0]["_id"]

    async def cancel(update, context):
        if not await _need_ap(update):
            return
        jid = await _one_job(update, context)
        if jid:
            ok = await asyncio.to_thread(rt.service.cancel, jid, update.effective_user.id)
            await update.message.reply_text(
                "🚫 Cancelled." if ok else
                "Can't cancel that one (already running, published or finished).")

    async def release(update, context):
        if not await _need_ap(update):
            return
        jid = await _one_job(update, context)
        if jid:
            ok = await asyncio.to_thread(rt.service.release, jid, update.effective_user.id)
            await update.message.reply_text(
                "▶️ Released — it will go out at the next allowed time." if ok
                else "That job isn't on hold.")

    async def autopilot_cmd(update, context):
        if not await _need_ap(update):
            return
        text = await asyncio.to_thread(autopilot_mod.handle_command, rt.autopilot,
                                       update.effective_user.id, context.args)
        await update.message.reply_text(text)

    async def schedule(update, context):
        if not await _need_ap(update):
            return
        uid = update.effective_user.id
        ready = await asyncio.to_thread(content_buffer.list_photo_ready, uid)
        if not ready:
            return await update.message.reply_text(
                "📭 Nothing publish-ready. Send photos and tap Approve first.")
        slot = await asyncio.to_thread(rt.calendar.next_slot, uid)
        if slot is None:
            return await update.message.reply_text(
                "No free slot in the next 7 days. Check /autopilot (limit, quiet hours, slots).")
        try:
            job = await asyncio.to_thread(rt.service.enqueue, uid, [ready[-1]["_id"]],
                                          "photo", slot, "scheduled", True)
        except (PublishError, NotConnected) as exc:
            return await update.message.reply_text(f"❌ {exc}")
        tz = await asyncio.to_thread(rt.tz_for, uid)
        if job.get("duplicate"):
            return await update.message.reply_text(
                f"Already in the queue ({job['status']}). See /queue.")
        await update.message.reply_text(
            f"🗓 Scheduled for {_local(slot, tz)}. Job {job['_id'][:6]} — /queue to review.")

    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("queue", queue))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("release", release))
    app.add_handler(CommandHandler("autopilot", autopilot_cmd))
    app.add_handler(CommandHandler("schedule", schedule))

    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(CommandHandler("disconnect", disconnect))
    app.add_handler(CommandHandler("igstatus", igstatus))
    app.add_handler(CommandHandler("post", post))
    app.add_handler(CallbackQueryHandler(buttons, pattern=r"^ig:"))
