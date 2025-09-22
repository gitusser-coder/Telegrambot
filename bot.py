import os, json, re, logging, threading, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, Response
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode, ChatType
from telegram.error import Forbidden
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters, ChatMemberHandler
)


# Nur diese User dürfen steuern (DEINE IDs hier eintragen!)
ALLOWED_USERS = {6911213901, 1007669571}  # <- ersetze/ergänze


def admin_only(func):
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        user = update.effective_user
        uid = user.id if user else None
        if uid not in ALLOWED_USERS:
            # In Gruppen still schweigen, im PN optional kurz meckern
            if update.effective_chat and update.effective_chat.type == "private" and getattr(update, "message", None):
                await update.message.reply_text("⛔ Keine Berechtigung.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper
# ---------- ENV ----------
TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_BASE = os.environ["WEBHOOK_BASE"]                     # z.B. https://deinservice.onrender.com
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "secret")   # exakt wie in Webhook-URL
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
GROUPS = json.loads(os.environ.get(
    "GROUPS_JSON",
    '{"bot_testen":0,"Bigbangbot":0,"BigBangBets":0,"BigBangBets VIP":0,"BigBangBets Sportschat":0}'
))

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webhook-bot")

# ---------- TELEGRAM APP ----------
application: Application = ApplicationBuilder().token(TOKEN).build()
app_loop = None  # wird nach Start gesetzt

ASK_TEXT, ASK_GROUPS, ASK_TIME = range(3)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

def next_run_local(hh:int, mm:int) -> datetime:
    now = datetime.now(TZ)
    run_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run_dt <= now: run_dt += timedelta(days=1)
    return run_dt

def group_keyboard(selected:set[str]) -> InlineKeyboardMarkup:
    rows=[]
    for name in GROUPS.keys():
        tick = "✅ " if name in selected else ""
        rows.append([InlineKeyboardButton(f"{tick}{name}", callback_data=f"toggle::{name}")])
    rows.append([
        InlineKeyboardButton("✅ Fertig", callback_data="done"),
        InlineKeyboardButton("✖️ Abbrechen", callback_data="cancel"),
    ])
    return InlineKeyboardMarkup(rows)

async def _broadcast(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    text = data.get("text","")
    for cid in data.get("chat_ids", []):
        try:
            await context.bot.send_message(
                chat_id=cid,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.error(f"Senden an {cid} fehlgeschlagen: {e}")

# ---------- COMMANDS ----------

@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("/start from chat %s", update.effective_chat.id)
    await update.message.reply_text(
        "Befehle:\n"
        "/plan – Text → Gruppe(n) → Zeit HH:MM (einmalig)\n"
        "/id – Chat‑ID per PN (unterstützt Weiterleitungen)\n"
        "/now – Sofort‑Broadcast an alle Gruppen in GROUPS_JSON\n"
        "/cancel – Dialog abbrechen"
    )
@admin_only
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    if "bot_username" not in context.bot_data:
        me = await context.bot.get_me()
        context.bot_data["bot_username"] = me.username or ""

    try:
        # Weiterleitung aus Gruppe/Kanal?
        fwd = getattr(msg, "forward_from_chat", None)
        if fwd:
            info = f"🔐 Chat-ID (aus Weiterleitung)\nName/Typ: {fwd.title or fwd.type}\nID: {fwd.id}"
            await context.bot.send_message(chat_id=user.id, text=info)
            if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                await msg.reply_text("✅ Gruppen‑ID wurde dir per PN geschickt (aus Weiterleitung).")
            else:
                await msg.reply_text("✅ Gruppen‑ID (aus Weiterleitung) per PN geschickt.")
            return

        # sonst: aktueller Chat
        info = f"🔐 Chat-ID\nName/Typ: {chat.title or chat.type}\nID: {chat.id}"
        await context.bot.send_message(chat_id=user.id, text=info)
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            await msg.reply_text("✅ Chat‑ID wurde dir per Privatnachricht geschickt.")
        else:
            await msg.reply_text("✅ Chat‑ID steht in deiner Privatnachricht.")
    except Forbidden:
        link = f"https://t.me/{context.bot_data.get('bot_username','')}"
        await update.message.reply_text(
            f"Ich darf dir noch keine PN schicken. Öffne den Bot und sende /start: {link}"
        )
@admin_only
async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = [cid for cid in GROUPS.values() if isinstance(cid, int) and cid != 0]
    if not ids:
        await update.message.reply_text("⚠️ Keine Gruppen‑IDs in GROUPS_JSON gesetzt.")
        return
    for cid in ids:
        await context.bot.send_message(chat_id=cid, text="Test: Sofort-Broadcast ✅", disable_web_page_preview=True)
    await update.message.reply_text("Sofort‑Broadcast ausgelöst.")

# ---------- /plan Conversation ----------
@admin_only
async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Schick den **Nachrichtentext**.", parse_mode=ParseMode.MARKDOWN)
    return ASK_TEXT

async def plan_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt:
        await update.message.reply_text("Leer. Bitte Text senden.")
        return ASK_TEXT
    context.user_data["planned_text"] = txt
    context.user_data["selected_groups"] = set()
    await update.message.reply_text(
        "Wähle Gruppe(n) und tippe danach **✅ Fertig**.",
        reply_markup=group_keyboard(context.user_data["selected_groups"])
    )
    return ASK_GROUPS

async def plan_groups_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sel: set[str] = context.user_data.get("selected_groups", set())
    data = q.data

    if data.startswith("toggle::"):
        name = data.split("::", 1)[1]
        if name in GROUPS:
            (sel.remove(name) if name in sel else sel.add(name))
            context.user_data["selected_groups"] = sel
        await q.edit_message_reply_markup(reply_markup=group_keyboard(sel))
        return ASK_GROUPS

    if data == "cancel":
        await q.edit_message_text("Abgebrochen.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "done":
        if not sel:
            await q.edit_message_text("Mindestens **eine Gruppe** wählen.")
            return ConversationHandler.END
        await q.edit_message_text("Uhrzeit `HH:MM` (24h) senden.", parse_mode=ParseMode.MARKDOWN)
        return ASK_TIME

    return ASK_GROUPS

async def plan_got_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    m = TIME_RE.match(t)
    if not m:
        await update.message.reply_text("Ungültig. Beispiel: 18:45")
        return ASK_TIME

    hh, mm = int(m.group(1)), int(m.group(2))
    text = context.user_data.get("planned_text", "").strip()
    names: set[str] = context.user_data.get("selected_groups", set())

    if not text or not names:
        await update.message.reply_text("Fehler. /plan neu starten.")
        context.user_data.clear()
        return ConversationHandler.END

    ids, missing = [], []
    for n in names:
        cid = GROUPS.get(n, 0)
        (ids.append(cid) if isinstance(cid, int) and cid != 0 else missing.append(n))

    if missing:
        await update.message.reply_text(
            "⚠️ IDs fehlen für:\n- " + "\n- ".join(missing) + "\nTrag sie in GROUPS_JSON ein und redeploye."
        )
        context.user_data.clear()
        return ConversationHandler.END

    run_dt = next_run_local(hh, mm)
    delay = (run_dt - datetime.now(TZ)).total_seconds()
    context.job_queue.run_once(_broadcast, when=delay, data={"text": text, "chat_ids": ids})

    await update.message.reply_text(
        f"✅ Geplant für {run_dt:%d.%m.%Y %H:%M} ({TZ.key}) in: {', '.join(sorted(names))}\n\n{text}"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def plan_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Abgebrochen.")
    return ConversationHandler.END

@admin_only
async def cmd_resolve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Nutzung: /resolve @Name   oder   /resolve https://t.me/Name
    if not context.args:
        await update.message.reply_text("Nutze: /resolve @ChannelName")
        return

    raw = context.args[0].strip()

    # URL -> @name ziehen
    if raw.startswith("https://t.me/"):
        raw = raw[len("https://t.me/"):]
    if not raw.startswith("@"):
        raw = "@" + raw

    try:
        chat = await context.bot.get_chat(raw)
        await update.message.reply_text(
            f"🔐 Chat gefunden:\nTitel: {chat.title}\nTyp: {chat.type}\nID: {chat.id}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Konnte {raw} nicht auflösen: {e}")

MAIN_ADMIN_IDS = {6911213901}  # einer deiner Admins

async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if not cm or not cm.new_chat_member:
        return

    # ist es unser Bot, dessen Status sich geändert hat?
    me = await context.bot.get_me()
    if cm.new_chat_member.user.id != me.id:
        return

    chat = cm.chat  # hier steckt die ID drin
    text = f"🔔 Bot-Status geändert\nTitel: {chat.title}\nTyp: {chat.type}\nID: {chat.id}"
    for admin_id in MAIN_ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text)
        except Forbidden:
            # Du hast dem Bot evtl. noch nie /start geschickt
            pass
        except Exception as e:
            logging.exception("PM an Admin fehlgeschlagen: %s", e)

# Registrieren (einmal, neben deinen anderen add_handler-Aufrufen)
application.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
# Handler registrieren
application.add_handler(CommandHandler("resolve", cmd_resolve))
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("id", cmd_id))
application.add_handler(CommandHandler("now", cmd_now))
application.add_handler(CommandHandler("cancel", plan_cancel))
application.add_handler(ConversationHandler(
    entry_points=[CommandHandler("plan", plan_start)],
    states={
        ASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_got_text)],
        ASK_GROUPS: [CallbackQueryHandler(plan_groups_cb)],
        ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_got_time)],
    },
    fallbacks=[CommandHandler("cancel", plan_cancel)],
    conversation_timeout=300,
))

# ---------- FLASK ----------
flask = Flask(__name__)

@flask.get("/")
def root(): return "ok"

@flask.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    log.info("Webhook hit")
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        # Update im PTB-Loop verarbeiten
        fut = asyncio.run_coroutine_threadsafe(application.process_update(update), app_loop)
        fut.result(timeout=5)  # Exceptions sofort sichtbar machen
    except Exception as e:
        log.exception(f"webhook error: {e}")
        return Response(status=500)
    return Response(status=200)

def run_ptb_loop():
    global app_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app_loop = loop
    async def runner():
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(f"{WEBHOOK_BASE}/webhook/{WEBHOOK_SECRET}")
        info = await application.bot.get_webhook_info()
        log.info("Webhook gesetzt: %s", info.url)
    loop.run_until_complete(runner())
    loop.run_forever()

if __name__ == "__main__":
    threading.Thread(target=run_ptb_loop, name="ptb", daemon=True).start()
    flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
