import os, logging, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode, ChatType
from telegram.error import Forbidden
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]  # bei Render setzen
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))

# Gruppen per ENV: GROUPS_JSON='{"BigBangBets":-100111,"BigBangBets VIP":-100222,"BigBangBets Sportschat":-100333}'
import json
GROUPS = json.loads(os.environ.get("GROUPS_JSON", '{"bot_testen": 4817569522,"BigBangBets VIP":0,"BigBangBets Sportschat":0}'))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

ASK_TEXT, ASK_GROUPS, ASK_TIME = range(3)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

async def _broadcast(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    text = data.get("text","")
    for cid in data.get("chat_ids", []):
        try:
            await context.bot.send_message(cid, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            log.error(f"senden an {cid} fehlgeschlagen: {e}")

def next_run(h,m):
    now = datetime.now(TZ)
    run_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if run_dt <= now: run_dt += timedelta(days=1)
    return run_dt

def kb(selected:set[str]):
    rows=[]
    for name in GROUPS.keys():
        tick = "✅ " if name in selected else ""
        rows.append([InlineKeyboardButton(f"{tick}{name}", callback_data=f"toggle::{name}")])
    rows.append([InlineKeyboardButton("✅ Fertig", callback_data="done"),
                 InlineKeyboardButton("✖️ Abbrechen", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

async def cmd_start(update: Update, _): 
    await update.message.reply_text("Nutze /plan (Text → Gruppe(n) → Zeit HH:MM), /id, /now, /cancel.")

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if "bot_username" not in context.bot_data:
        me = await context.bot.get_me(); context.bot_data["bot_username"]=me.username or ""
    try:
        await context.bot.send_message(user.id, f"🔐 Chat-ID\nName/Typ: {chat.title or chat.type}\nID: {chat.id}")
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            await update.message.reply_text("✅ Chat‑ID wurde dir per Privatnachricht geschickt.")
        else:
            await update.message.reply_text("✅ Chat‑ID steht in deiner Privatnachricht.")
    except Forbidden:
        link = f"https://t.me/{context.bot_data.get('bot_username','')}"
        await update.message.reply_text(f"Ich darf dir (noch) keine PN schicken. Öffne den Bot und sende /start: {link}")
    except Exception as e:
        log.error(f"/id DM fail: {e}")
        await update.message.reply_text("❌ Konnte keine PN senden.")

async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids=[cid for cid in GROUPS.values() if isinstance(cid,int) and cid!=0]
    if not ids: return await update.message.reply_text("⚠️ Keine Gruppen‑IDs in GROUPS_JSON gesetzt.")
    context.job_queue.run_once(_broadcast, when=0, data={"text":"Test: Sofort-Broadcast ✅","chat_ids":ids})
    await update.message.reply_text("Sofort‑Broadcast ausgelöst.")

async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Schick den **Nachrichtentext**.", parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=ReplyKeyboardRemove())
    return ASK_TEXT

async def plan_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt=(update.message.text or "").strip()
    if not txt: return await update.message.reply_text("Leer. Bitte Text senden.") or ASK_TEXT
    context.user_data["planned_text"]=txt; context.user_data["selected_groups"]=set()
    await update.message.reply_text("Wähle Gruppe(n), dann **✅ Fertig**.", reply_markup=kb(context.user_data["selected_groups"]))
    return ASK_GROUPS

async def plan_groups_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    sel: set[str]=context.user_data.get("selected_groups", set())
    data=q.data
    if data.startswith("toggle::"):
        name=data.split("::",1)[1]
        if name in GROUPS:
            sel.remove(name) if name in sel else sel.add(name)
            context.user_data["selected_groups"]=sel
        return await q.edit_message_reply_markup(reply_markup=kb(sel)) or ASK_GROUPS
    if data=="cancel":
        await q.edit_message_text("Abgebrochen."); context.user_data.clear(); return ConversationHandler.END
    if data=="done":
        if not sel:
            await q.edit_message_text("Mindestens **eine Gruppe** wählen."); 
            await q.message.reply_text("Wähle Gruppe(n) → **✅ Fertig**.", reply_markup=kb(sel))
            return ASK_GROUPS
        await q.edit_message_text("Uhrzeit `HH:MM` (24h) senden.", parse_mode=ParseMode.MARKDOWN)
        return ASK_TIME
    return ASK_GROUPS

async def plan_got_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t=(update.message.text or "").strip(); m=TIME_RE.match(t)
    if not m: return await update.message.reply_text("Ungültig. Beispiel: 18:45") or ASK_TIME
    hh,mm=int(m.group(1)),int(m.group(2))
    text=context.user_data.get("planned_text","").strip()
    names: set[str]=context.user_data.get("selected_groups", set())
    if not text or not names: 
        await update.message.reply_text("Fehler im Dialog. /plan neu starten."); context.user_data.clear(); return ConversationHandler.END
    ids=[]; missing=[]
    for n in names:
        cid=GROUPS.get(n,0)
        (ids.append(cid) if isinstance(cid,int) and cid!=0 else missing.append(n))
    if missing:
        await update.message.reply_text("⚠️ IDs fehlen für:\n- "+"\n- ".join(missing)+"\nTrag sie in GROUPS_JSON ein.")
        context.user_data.clear(); return ConversationHandler.END
    run_dt=next_run(hh,mm); delay=(run_dt-datetime.now(TZ)).total_seconds()
    context.job_queue.run_once(_broadcast, when=delay, data={"text":text,"chat_ids":ids})
    await update.message.reply_text(f"✅ Geplant für {run_dt:%d.%m.%Y %H:%M} ({TZ.key}) in: {', '.join(sorted(names))}\n\n{text}")
    context.user_data.clear(); return ConversationHandler.END

async def plan_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear(); await update.message.reply_text("Abgebrochen."); return ConversationHandler.END

def main():
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("cancel", plan_cancel))
    conv=ConversationHandler(
        entry_points=[CommandHandler("plan", plan_start)],
        states={
            ASK_TEXT:[MessageHandler(filters.TEXT & ~filters.COMMAND, plan_got_text)],
            ASK_GROUPS:[CallbackQueryHandler(plan_groups_cb)],
            ASK_TIME:[MessageHandler(filters.TEXT & ~filters.COMMAND, plan_got_time)],
        },
        fallbacks=[CommandHandler("cancel", plan_cancel)],
        conversation_timeout=300,
    )
    app.add_handler(conv)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
