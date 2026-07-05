import os
import json
import sqlite3
import time
import uuid
import threading

import telebot
from telebot import types

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_CHANNEL = os.environ.get("DB_CHANNEL")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")

if not BOT_TOKEN:
    raise SystemExit("❌ Variable BOT_TOKEN manquante.")
if not DB_CHANNEL:
    raise SystemExit("❌ Variable DB_CHANNEL manquante.")

DB_CHANNEL = int(DB_CHANNEL)
ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip()]
if not ADMIN_IDS:
    raise SystemExit("❌ Variable ADMIN_IDS manquante (ton user ID Telegram).")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ==========================================
# BASE DE DONNÉES
# ==========================================
conn = sqlite3.connect("store.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS links (
    code TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_seen TEXT
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)""")
conn.commit()

# États temporaires en mémoire (flux multi-étapes par admin)
pending = {}


# ==========================================
# HELPERS - SETTINGS
# ==========================================
def get_setting(key, default=None):
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if not row:
        return default
    return json.loads(row[0])


def set_setting(key, value):
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


# ==========================================
# HELPERS - USERS
# ==========================================
def register_user(user_id):
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
        (user_id, str(int(time.time()))),
    )
    conn.commit()


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ==========================================
# HELPERS - LIENS
# ==========================================
def save_link(payload):
    code = uuid.uuid4().hex[:8]
    cur.execute(
        "INSERT INTO links (code, data, created_at) VALUES (?, ?, ?)",
        (code, json.dumps(payload), str(int(time.time()))),
    )
    conn.commit()
    return code


def get_link(code):
    cur.execute("SELECT data FROM links WHERE code = ?", (code,))
    row = cur.fetchone()
    return json.loads(row[0]) if row else None


def make_link(code):
    username = bot.get_me().username
    return f"https://t.me/{username}?start={code}"


# ==========================================
# HELPERS - ENTITÉS (gras, italique, liens, etc.)
# ==========================================
def entities_to_dicts(entities):
    if not entities:
        return []
    result = []
    for e in entities:
        item = {"type": e.type, "offset": e.offset, "length": e.length}
        if e.type == "text_link" and e.url:
            item["url"] = e.url
        result.append(item)
    return result


def dicts_to_entities(items):
    if not items:
        return None
    entities = []
    for it in items:
        entities.append(types.MessageEntity(
            type=it["type"], offset=it["offset"], length=it["length"],
            url=it.get("url")
        ))
    return entities


def capture_rich_message(message):
    """Capture texte/légende + mise en forme + photo éventuelle d'un message admin."""
    photo_id = message.photo[-1].file_id if message.photo else None
    text = message.text or message.caption or ""
    entities = message.entities if message.text else message.caption_entities
    return {
        "text": text,
        "entities": entities_to_dicts(entities),
        "photo": photo_id,
    }


def send_rich_message(chat_id, content, extra_text="", reply_markup=None):
    text = (content.get("text") or "") + extra_text
    entities = dicts_to_entities(content.get("entities"))
    if content.get("photo"):
        return bot.send_photo(
            chat_id, content["photo"], caption=text,
            caption_entities=entities, reply_markup=reply_markup
        )
    return bot.send_message(
        chat_id, text or " ", entities=entities, reply_markup=reply_markup
    )


# ==========================================
# HELPERS - EXTRACTION DE FICHIER (file_id permanent)
# ==========================================
def extract_file_info(message):
    ct = message.content_type
    file_id = None
    if ct == "document" and message.document:
        file_id = message.document.file_id
    elif ct == "photo" and message.photo:
        file_id = message.photo[-1].file_id
    elif ct == "video" and message.video:
        file_id = message.video.file_id
    elif ct == "audio" and message.audio:
        file_id = message.audio.file_id
    elif ct == "sticker" and message.sticker:
        file_id = message.sticker.file_id
    return {
        "type": ct,
        "file_id": file_id,
        "caption": message.caption or "",
        "entities": entities_to_dicts(message.caption_entities),
    }


# ==========================================
# HELPERS - ABONNEMENT OBLIGATOIRE
# ==========================================
def get_forward_channel_msgid(message):
    if message.forward_from_chat and message.forward_from_message_id:
        return message.forward_from_chat.id, message.forward_from_message_id
    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "type", None) == "channel":
        chat = getattr(origin, "chat", None)
        msgid = getattr(origin, "message_id", None)
        if chat and msgid:
            return chat.id, msgid
    return None, None


def is_subscribed(user_id):
    fs = get_setting("force_sub")
    if not fs or not fs.get("enabled"):
        return True
    try:
        member = bot.get_chat_member(fs["channel_id"], user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def send_subscribe_prompt(chat_id, start_arg):
    fs = get_setting("force_sub")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➡️ Rejoindre le canal", url=fs["invite_link"]))
    kb.add(types.InlineKeyboardButton("✅ J'ai rejoint", callback_data=f"checksub:{start_arg or '_'}"))
    bot.send_message(
        chat_id,
        "🔒 Tu dois rejoindre notre canal avant d'accéder à ce contenu.",
        reply_markup=kb,
    )


# ==========================================
# HELPERS - LIVRAISON D'UN FICHIER (via file_id, pas via le canal)
# ==========================================
def protection_enabled():
    return bool(get_setting("protect_content", False))


def deliver_item(chat_id, item):
    protect = protection_enabled()
    entities = dicts_to_entities(item.get("entities"))
    caption = item.get("caption") or None
    ct = item["type"]
    fid = item["file_id"]

    if ct == "document":
        sent = bot.send_document(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
    elif ct == "photo":
        sent = bot.send_photo(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
    elif ct == "video":
        sent = bot.send_video(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
    elif ct == "audio":
        sent = bot.send_audio(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
    elif ct == "sticker":
        sent = bot.send_sticker(chat_id, fid, protect_content=protect)
    else:
        return None

    del_cfg = get_setting("delete_msg")
    if del_cfg and del_cfg.get("delay_minutes"):
        delay_min = del_cfg["delay_minutes"]
        extra = f"\n\n⏳ Ce fichier sera supprimé dans {delay_min} min. Enregistre-le vite !"
        send_rich_message(chat_id, del_cfg, extra_text=extra)
        threading.Timer(delay_min * 60, delete_later, args=(chat_id, sent.message_id)).start()

    return sent


def delete_later(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ==========================================
# /start
# ==========================================
def cmd_start(message, arg):
    register_user(message.from_user.id)

    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id, arg)
        return

    if arg:
        payload = get_link(arg)
        if not payload:
            bot.reply_to(message, "❌ Ce lien est invalide ou a expiré.")
            return
        bot.send_chat_action(message.chat.id, "typing")
        for item in payload.get("items", []):
            try:
                deliver_item(message.chat.id, item)
            except Exception:
                continue
        return

    welcome = get_setting("welcome")
    if welcome:
        send_rich_message(message.chat.id, welcome)
    else:
        bot.reply_to(
            message,
            "👋 Bienvenue !\n\nEnvoie-moi un fichier, je te donnerai un lien de partage.\n"
            "Tape /help pour voir toutes les commandes.",
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("checksub:"))
def cb_checksub(call):
    arg = call.data.split(":", 1)[1]
    arg = None if arg == "_" else arg
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Abonnement confirmé !")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        cmd_start(call.message, arg)
    else:
        bot.answer_callback_query(call.id, "❌ Tu n'as pas encore rejoint le canal.", show_alert=True)


# ==========================================
# /help
# ==========================================
def cmd_help(message):
    text = (
        "📖 *Commandes disponibles*\n\n"
        "*Pour tout le monde*\n"
        "/start – démarrer le bot ou récupérer un fichier via un lien\n"
        "/help – afficher ce message\n"
    )
    if is_admin(message.from_user.id):
        text += (
            "\n*Réservées aux admins*\n"
            "Envoyer un fichier (ou en forwarder un depuis le canal) – crée un lien permanent\n"
            "/batch – créer un lien pour une plage de fichiers du canal\n"
            "/cancel – annuler l'opération en cours\n"
            "/stats – statistiques d'utilisation du bot\n"
            "/setwelcome – modifier le message d'accueil (/start)\n"
            "/setdelete – modifier le message + délai d'auto-suppression\n"
            "/setforcesub – configurer le canal d'abonnement obligatoire\n"
            "/forcesub on|off – activer/désactiver l'abonnement obligatoire\n"
            "/protect on|off – activer/désactiver la protection anti-transfert\n"
            "/broadcast – envoyer un message à tous les utilisateurs\n"
        )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")


# ==========================================
# /stats
# ==========================================
def cmd_stats(message):
    if not is_admin(message.from_user.id):
        return
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM links")
    total_links = cur.fetchone()[0]

    total_files = 0
    cur.execute("SELECT data FROM links")
    for (raw,) in cur.fetchall():
        payload = json.loads(raw)
        total_files += len(payload.get("items", []))

    fs = get_setting("force_sub")
    protect = "Activée ✅" if protection_enabled() else "Désactivée ❌"
    fs_state = "Activé ✅" if fs and fs.get("enabled") else "Désactivé ❌"

    bot.send_message(
        message.chat.id,
        "📊 *Statistiques*\n\n"
        f"👤 Utilisateurs uniques : {total_users}\n"
        f"🔗 Liens créés : {total_links}\n"
        f"📁 Fichiers référencés : {total_files}\n"
        f"🔒 Abonnement obligatoire : {fs_state}\n"
        f"🛡️ Protection anti-transfert : {protect}",
        parse_mode="Markdown",
    )


# ==========================================
# /setwelcome, /setdelete, /setforcesub, /broadcast (démarrage des flux)
# ==========================================
def cmd_setwelcome(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "setwelcome"}
    bot.reply_to(message, "✏️ Envoie maintenant le nouveau message d'accueil (texte, image, mise en forme).")


def cmd_setdelete(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "setdelete_msg"}
    bot.reply_to(message, "✏️ Envoie le message d'avertissement à afficher après chaque fichier (texte/image/style).")


def cmd_setforcesub(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "setforcesub_channel"}
    bot.reply_to(message, "📡 Transfère-moi (forward) n'importe quel message du canal à rendre obligatoire.")


def cmd_forcesub_toggle(message, arg):
    if not is_admin(message.from_user.id):
        return
    fs = get_setting("force_sub")
    if not fs:
        bot.reply_to(message, "⚠️ Configure d'abord le canal avec /setforcesub.")
        return
    fs["enabled"] = arg.strip().lower() == "on"
    set_setting("force_sub", fs)
    bot.reply_to(message, f"✅ Abonnement obligatoire {'activé' if fs['enabled'] else 'désactivé'}.")


def cmd_protect_toggle(message, arg):
    if not is_admin(message.from_user.id):
        return
    value = arg.strip().lower() == "on"
    set_setting("protect_content", value)
    bot.reply_to(
        message,
        f"✅ Protection anti-transfert {'activée' if value else 'désactivée'}.\n"
        "ℹ️ Cela empêche le transfert et l'enregistrement direct, mais ne bloque pas les captures d'écran "
        "(Telegram ne le permet pas techniquement)."
    )


def cmd_broadcast(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "broadcast"}
    bot.reply_to(message, "📢 Envoie le message à diffuser à tous les utilisateurs.")


def cmd_batch(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "batch_first"}
    bot.reply_to(message, "📦 Transfère-moi le PREMIER fichier de la plage (depuis le canal privé).")


def cmd_cancel(message):
    if message.from_user.id in pending:
        pending.pop(message.from_user.id)
        bot.reply_to(message, "🚫 Opération annulée.")
    else:
        bot.reply_to(message, "ℹ️ Aucune opération en cours.")


# ==========================================
# RÉSOLUTION D'UNE PLAGE DE MESSAGES DU CANAL EN file_id PERMANENTS
# ==========================================
def resolve_batch_range(admin_chat_id, start, end):
    items = []
    for mid in range(start, end + 1):
        try:
            fwd = bot.forward_message(admin_chat_id, DB_CHANNEL, mid)
        except Exception:
            continue
        item = extract_file_info(fwd)
        try:
            bot.delete_message(admin_chat_id, fwd.message_id)
        except Exception:
            pass
        if item["file_id"]:
            items.append(item)
        time.sleep(0.05)
    return items


# ==========================================
# GESTION DES FLUX EN COURS (pending)
# ==========================================
def handle_pending(message):
    uid = message.from_user.id
    state = pending.get(uid)
    if not state:
        return False

    action = state["action"]

    if action == "setwelcome":
        set_setting("welcome", capture_rich_message(message))
        pending.pop(uid)
        bot.reply_to(message, "✅ Message d'accueil mis à jour.")
        return True

    if action == "setdelete_msg":
        state["data"] = capture_rich_message(message)
        state["action"] = "setdelete_time"
        bot.reply_to(message, "⏱️ Après combien de minutes les fichiers doivent-ils être supprimés ?")
        return True

    if action == "setdelete_time":
        try:
            minutes = int((message.text or "").strip())
        except ValueError:
            bot.reply_to(message, "❌ Envoie un nombre entier de minutes (ex: 10).")
            return True
        data = state["data"]
        data["delay_minutes"] = minutes
        set_setting("delete_msg", data)
        pending.pop(uid)
        bot.reply_to(message, f"✅ Message et délai ({minutes} min) enregistrés.")
        return True

    if action == "setforcesub_channel":
        chat_id, _ = get_forward_channel_msgid(message)
        if not chat_id:
            bot.reply_to(message, "❌ Ce n'est pas un message transféré depuis un canal. Réessaie.")
            return True
        state["data"] = {"channel_id": chat_id}
        state["action"] = "setforcesub_link"
        bot.reply_to(message, "🔗 Envoie maintenant le lien d'invitation public du canal (https://t.me/...).")
        return True

    if action == "setforcesub_link":
        link = (message.text or "").strip()
        if not link.startswith("http"):
            bot.reply_to(message, "❌ Ça ne ressemble pas à un lien valide. Réessaie.")
            return True
        data = state["data"]
        data["invite_link"] = link
        data["enabled"] = True
        set_setting("force_sub", data)
        pending.pop(uid)
        bot.reply_to(message, "✅ Abonnement obligatoire configuré et activé.")
        return True

    if action == "broadcast":
        content = capture_rich_message(message)
        pending.pop(uid)
        cur.execute("SELECT user_id FROM users")
        user_ids = [row[0] for row in cur.fetchall()]
        sent, failed = 0, 0
        for target in user_ids:
            try:
                send_rich_message(target, content)
                sent += 1
            except Exception:
                failed += 1
        bot.reply_to(message, f"📢 Diffusion terminée. Envoyés : {sent} | Échecs : {failed}")
        return True

    if action == "batch_first":
        chat_id, msg_id = get_forward_channel_msgid(message)
        if chat_id != DB_CHANNEL:
            bot.reply_to(message, "❌ Ce message ne vient pas du canal privé configuré. Réessaie.")
            return True
        state["data"] = {"first": msg_id}
        state["action"] = "batch_last"
        bot.reply_to(message, "📦 Reçu. Transfère-moi maintenant le DERNIER fichier de la plage.")
        return True

    if action == "batch_last":
        chat_id, msg_id = get_forward_channel_msgid(message)
        if chat_id != DB_CHANNEL:
            bot.reply_to(message, "❌ Ce message ne vient pas du canal privé configuré. Réessaie.")
            return True
        first = state["data"]["first"]
        start, end = min(first, msg_id), max(first, msg_id)
        pending.pop(uid)
        bot.reply_to(message, f"⏳ Récupération de {end - start + 1} message(s), merci de patienter...")
        items = resolve_batch_range(message.chat.id, start, end)
        if not items:
            bot.reply_to(message, "❌ Aucun fichier valide trouvé dans cette plage.")
            return True
        code = save_link({"type": "files", "items": items})
        bot.reply_to(message, f"✅ Lien permanent créé pour {len(items)} fichier(s) :\n{make_link(code)}")
        return True

    return False


# ==========================================
# NOUVEAU FICHIER (envoyé directement OU forwardé depuis le canal)
# ==========================================
def handle_new_file(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Tu n'es pas autorisé à uploader de fichiers.")
        return
    item = extract_file_info(message)
    if not item["file_id"]:
        bot.reply_to(message, "❌ Type de fichier non pris en charge.")
        return
    code = save_link({"type": "files", "items": [item]})
    bot.reply_to(message, f"✅ Lien permanent créé :\n{make_link(code)}")


# ==========================================
# ROUTAGE PRINCIPAL
# ==========================================
COMMANDS = {
    "start": lambda m, a: cmd_start(m, a),
    "help": lambda m, a: cmd_help(m),
    "stats": lambda m, a: cmd_stats(m),
    "setwelcome": lambda m, a: cmd_setwelcome(m),
    "setdelete": lambda m, a: cmd_setdelete(m),
    "setforcesub": lambda m, a: cmd_setforcesub(m),
    "forcesub": lambda m, a: cmd_forcesub_toggle(m, a),
    "protect": lambda m, a: cmd_protect_toggle(m, a),
    "broadcast": lambda m, a: cmd_broadcast(m),
    "batch": lambda m, a: cmd_batch(m),
    "cancel": lambda m, a: cmd_cancel(m),
}


@bot.message_handler(content_types=["text", "photo", "document", "video", "audio", "sticker"])
def router(message):
    raw = message.text or message.caption or ""

    if raw.startswith("/"):
        parts = raw.split(maxsplit=1)
        cmd = parts[0][1:].split("@")[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        handler = COMMANDS.get(cmd)
        if handler:
            handler(message, arg)
        return

    register_user(message.from_user.id)

    if handle_pending(message):
        return

    if message.content_type in ("document", "photo", "video", "audio", "sticker"):
        handle_new_file(message)


# ==========================================
# LANCEMENT
# ==========================================
if __name__ == "__main__":
    print("Bot démarré, en attente de messages...")
    bot.infinity_polling()
