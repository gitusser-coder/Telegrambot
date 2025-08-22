import os, json, re, logging, threading, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, Response

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatType
from telegram.error import Forbidden
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# ---------- ENV ----------
TOKEN = os.environ["TELEGRAM_TOKEN"]                         # BotFather-Token
WEBHOOK_BASE = os.environ["WEBHOOK_BASE"]                    # z.B. https://deinservice.onrender.com
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "secret")  # beliebiger String
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))

# Gruppen: Namen -> Chat-IDs (negative ints). 0 = Platzhalter.
GROUPS = json.loads(os.environ.get(
    "GROUPS_JSON",
    '{"BigBangBets":0,"BigBangBets VIP":0,"BigBangBets Sportschat":0}'
))

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webhook-bot")

# ---------- TELEGRAM APP ----------
application: Application = ApplicationBuilder().token(TOKEN).build()

ASK_TEXT, ASK_GROUPS, ASK_TIME = range(3)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

def next_run_local(hh:int, mm:int) -> datetime:
    now = datetime.now(TZ)
    run_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run_dt <= now:
        run_dt += timedelta(days=1)
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
async def cmd_start(update: Update, _):
    await update.message.reply_text(
        "Befehle:\n"
        "/plan – Text → Gruppe(n) → Zeit HH:MM (einmalig)\n"
        "/id – Chat‑ID per PN (unterstützt Weiterleitungen)\n"
        "/now – Sofort‑Broadcast an alle Gruppen in GROUPS_JSON\n"
        "/cancel – Dialog abbrechen"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Variante A (Weiterleitung):
    - Wenn im PN eine weitergeleitete Gruppen-Nachricht vorliegt, nimm deren Chat-ID.
    - Sonst: aktuelle Chat-ID per PN schicken (Gruppe -> Gruppen-ID; PN -> User-ID).
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    if "bot_username" not in context.bot_data:
        me = await context.bot.get_me()
        context.bot_data["bot_username"] = me.username or ""

    try:
        fwd = getattr(msg, "forward_from_chat", None)
        if fwd:
            info = f"🔐 Chat-ID (aus Weiterleitung)\nName/Typ: {fwd.title or fwd.type}\nID: {fwd.id}"
            await context.bot.send_message(chat_id=user.id, text=info)
            if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                await msg.reply_text("✅ Gruppen‑ID wurde dir per PN geschickt (aus Weiterleitung).")
            else:
                await msg.reply_text("✅ Gruppen‑ID (aus Weiterleitung) per PN geschickt.")
            return

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

async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = [cid for cid in GROUPS.values() if isinstance(cid, int) and cid != 0]
    if not ids:
        await update.message.reply_text("⚠️ Keine Gruppen‑IDs in GROUPS_JSON gesetzt.")
        return
    for cid in ids:
        await context.bot.send_message(chat_id=cid, text="Test: Sofort-Broadcast ✅", disable_web_page_preview=True)
    await update.message.reply_text("Sofort‑Broadcast ausgelöst.")

# ---------- /plan Conversation ----------
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

# Handler registrieren
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

# ---------- FLASK (Webhook-Endpunkte) ----------
flask = Flask(__name__)

@flask.get("/health")
def health(): return "ok"

@flask.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        application.update_queue.put_nowait(update)
    except Exception as e:
        log.exception(f"webhook error: {e}")
    return Response(status=200)

def start_ptb():
    # Eigene Event-Loop im Thread erzeugen, sonst "There is no current event loop"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.start())
    loop.run_forever()

if __name__ == "__main__":
    # Telegram-App parallel zu Flask starten (eigener Loop, kein Polling!)
    threading.Thread(target=start_ptb, name="start_ptb", daemon=True).start()

    # Webhook setzen (idempotent, ohne 'requests')
    async def set_webhook():
        await application.bot.set_webhook(f"{WEBHOOK_BASE}/webhook/{WEBHOOK_SECRET}")
        info = await application.bot.get_webhook_info()
        log.info("Webhook gesetzt: %s", info.url)

    asyncio.run(set_webhook())

    flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))