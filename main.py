import os
import re
import io
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
DB_PATH = os.environ.get("DB_PATH", "store.db")

if not BOT_TOKEN:
    raise SystemExit("Variable BOT_TOKEN manquante.")
if not DB_CHANNEL:
    raise SystemExit("Variable DB_CHANNEL manquante.")

DB_CHANNEL = int(DB_CHANNEL)
ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip()]
if not ADMIN_IDS:
    raise SystemExit("Variable ADMIN_IDS manquante (ton user ID Telegram).")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ==========================================
# BASE DE DONNÉES
# ==========================================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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

pending = {}

PLACEHOLDER_KEYS = ("mention", "date", "time", "bot_name", "count")


# ==========================================
# SETTINGS
# ==========================================
def get_setting(key, default=None):
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    return json.loads(row[0]) if row else default


def set_setting(key, value):
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


# ==========================================
# USERS
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
# LIENS
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
# ENTITÉS (gras, italique, citation, code, liens, mentions...)
# ==========================================
def entities_to_dicts(entities):
    if not entities:
        return []
    result = []
    for e in entities:
        item = {"type": e.type, "offset": e.offset, "length": e.length}
        if e.type == "text_link" and e.url:
            item["url"] = e.url
        if e.type == "text_mention" and e.user:
            item["user_id"] = e.user.id
            item["user_first_name"] = e.user.first_name or "Utilisateur"
        if e.type == "pre" and getattr(e, "language", None):
            item["language"] = e.language
        if e.type == "custom_emoji" and getattr(e, "custom_emoji_id", None):
            item["custom_emoji_id"] = e.custom_emoji_id
        result.append(item)
    return result


def dicts_to_entities(items):
    if not items:
        return None
    entities = []
    for it in items:
        kwargs = dict(type=it["type"], offset=it["offset"], length=it["length"])
        if it.get("url"):
            kwargs["url"] = it["url"]
        if it.get("user_id"):
            kwargs["user"] = types.User(id=it["user_id"], is_bot=False, first_name=it.get("user_first_name") or "Utilisateur")
        if it.get("language"):
            kwargs["language"] = it["language"]
        if it.get("custom_emoji_id"):
            kwargs["custom_emoji_id"] = it["custom_emoji_id"]
        entities.append(types.MessageEntity(**kwargs))
    return entities


def capture_rich_message(message):
    photo_id = message.photo[-1].file_id if message.photo else None
    text = message.text or message.caption or ""
    entities = message.entities if message.text else message.caption_entities
    return {
        "text": text,
        "entities": entities_to_dicts(entities),
        "photo": photo_id,
    }


def find_unknown_placeholders(text):
    found = set(re.findall(r"\{([a-zA-Z_]+)\}", text))
    return sorted(found - set(PLACEHOLDER_KEYS))


# ==========================================
# VARIABLES DYNAMIQUES ({mention}, {date}, {time}, {bot_name}, {count})
# ==========================================
def build_mention(user):
    name = (user.first_name or "").strip()
    if getattr(user, "last_name", None):
        name = f"{name} {user.last_name}".strip()
    if not name:
        name = "Utilisateur"
    mu = types.User(id=user.id, is_bot=False, first_name=name)
    return name, mu


def build_ctx(user, delay=None, count=None):
    return {
        "mention": build_mention(user),
        "date": time.strftime("%d/%m/%Y"),
        "time": str(delay) if delay is not None else "",
        "bot_name": bot.get_me().username,
        "count": str(count) if count is not None else "",
    }


def render_entities(text, entities_dicts, ctx):
    pattern = re.compile(r"\{(" + "|".join(PLACEHOLDER_KEYS) + r")\}")
    matches = [(m.start(), m.end(), m.group(1)) for m in pattern.finditer(text)]
    if not matches:
        return text, dicts_to_entities(entities_dicts)

    pieces, last_end, mention_entities, deltas = [], 0, [], []
    for start, end, key in matches:
        pieces.append(text[last_end:start])
        cur_offset = sum(len(p) for p in pieces)
        if key == "mention":
            disp, mention_user = ctx["mention"]
            mention_entities.append((cur_offset, len(disp), mention_user))
            replacement = disp
        else:
            replacement = str(ctx.get(key, ""))
        pieces.append(replacement)
        deltas.append(len(replacement) - (end - start))
        last_end = end
    pieces.append(text[last_end:])
    new_text = "".join(pieces)

    def shift(pos):
        total = 0
        for (start, _e, _k), delta in zip(matches, deltas):
            if start < pos:
                total += delta
        return pos + total

    result_entities = []
    for e in entities_dicts or []:
        result_entities.append(types.MessageEntity(
            type=e["type"], offset=shift(e["offset"]), length=e["length"], url=e.get("url")
        ))
    for offset, length, user in mention_entities:
        result_entities.append(types.MessageEntity(type="text_mention", offset=offset, length=length, user=user))
    return new_text, (result_entities or None)


def send_rich_message(chat_id, content, ctx=None, reply_markup=None):
    raw_text = content.get("text") or ""
    if ctx:
        text, entities = render_entities(raw_text, content.get("entities"), ctx)
    else:
        text, entities = raw_text, dicts_to_entities(content.get("entities"))

    try:
        if content.get("photo"):
            return bot.send_photo(chat_id, content["photo"], caption=text or None,
                                   caption_entities=entities, reply_markup=reply_markup)
        return bot.send_message(chat_id, text or " ", entities=entities, reply_markup=reply_markup)
    except Exception:
        try:
            if content.get("photo"):
                return bot.send_photo(chat_id, content["photo"], caption=text or None, reply_markup=reply_markup)
            return bot.send_message(chat_id, text or " ", reply_markup=reply_markup)
        except Exception:
            return None


# ==========================================
# ÉDITION EN PLACE (pour garder l'interaction dans le même message)
# ==========================================
def edit_or_send(chat_id, msg_id, text, reply_markup=None, parse_mode=None):
    """Tente d'éditer le message existant (texte ou légende) ; sinon envoie un nouveau message."""
    try:
        bot.edit_message_text(text, chat_id, msg_id, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    except Exception:
        pass
    try:
        bot.edit_message_caption(caption=text, chat_id=chat_id, message_id=msg_id,
                                  parse_mode=parse_mode, reply_markup=reply_markup)
        return
    except Exception:
        pass
    bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)


def panel_reply(message, state, text, reply_markup=None):
    """
    Termine une étape d'un flux déclenché par un bouton (menu) en éditant le
    panneau d'origine au lieu d'envoyer un nouveau message, et supprime le
    message que l'utilisateur vient d'envoyer (Telegram autorise un bot à
    supprimer les messages entrants dans une conversation privée). 🧹
    Si le flux n'a pas été déclenché par un bouton (ex: /setwelcome tapé en
    commande), on retombe simplement sur une réponse classique.
    """
    panel_chat_id = state.get("panel_chat_id")
    panel_msg_id = state.get("panel_msg_id")
    if panel_chat_id and panel_msg_id:
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        edit_or_send(panel_chat_id, panel_msg_id, text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        bot.reply_to(message, text, parse_mode="HTML", reply_markup=reply_markup)


# ==========================================
# EXTRACTION DE FICHIER (file_id permanent)
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
# ABONNEMENT OBLIGATOIRE (multi-canaux, liens auto-générés)
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


def missing_channels(user_id):
    fs = get_setting("force_sub")
    if not fs or not fs.get("enabled") or not fs.get("channels"):
        return []
    missing = []
    for ch in fs["channels"]:
        try:
            member = bot.get_chat_member(ch["channel_id"], user_id)
            if member.status not in ("member", "administrator", "creator"):
                missing.append(ch)
        except Exception:
            missing.append(ch)
    return missing


def is_subscribed(user_id):
    return len(missing_channels(user_id)) == 0


def send_subscribe_prompt(chat_id, user_id, start_arg):
    missing = missing_channels(user_id)
    kb = types.InlineKeyboardMarkup()
    for ch in missing:
        kb.row(types.InlineKeyboardButton(f"📡 {ch.get('title', 'Canal')}", url=ch["invite_link"]))
    kb.row(types.InlineKeyboardButton("✅ J'ai rejoint", callback_data=f"checksub:{start_arg or '_'}"))
    bot.send_message(
        chat_id,
        "🔒 <b>Accès restreint</b>\n\nRejoins le(s) canal ci-dessous avant de continuer.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ==========================================
# LIVRAISON D'UN FICHIER (via file_id, permanent)
# ==========================================
def protection_enabled():
    return bool(get_setting("protect_content", False))


def deliver_item(chat_id, item):
    protect = protection_enabled()
    entities = dicts_to_entities(item.get("entities"))
    caption = item.get("caption") or None
    ct, fid = item["type"], item["file_id"]
    try:
        if ct == "document":
            return bot.send_document(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
        if ct == "photo":
            return bot.send_photo(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
        if ct == "video":
            return bot.send_video(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
        if ct == "audio":
            return bot.send_audio(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
        if ct == "sticker":
            return bot.send_sticker(chat_id, fid, protect_content=protect)
    except Exception:
        try:
            if ct == "document":
                return bot.send_document(chat_id, fid, caption=caption, protect_content=protect)
            if ct == "photo":
                return bot.send_photo(chat_id, fid, caption=caption, protect_content=protect)
            if ct == "video":
                return bot.send_video(chat_id, fid, caption=caption, protect_content=protect)
            if ct == "audio":
                return bot.send_audio(chat_id, fid, caption=caption, protect_content=protect)
        except Exception:
            return None
    return None


def delete_many(chat_id, message_ids):
    for mid in message_ids:
        try:
            bot.delete_message(chat_id, mid)
        except Exception:
            pass


def delete_and_offer_resend(chat_id, message_ids, code):
    delete_many(chat_id, message_ids)
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🔄 Renvoyer les fichiers", callback_data=f"resend:{code}"))
    bot.send_message(
        chat_id,
        "🗑️ <b>Fichiers supprimés !</b> ⏳\n\n📤 Tu peux les redemander à tout moment en appuyant ci-dessous 👇",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ==========================================
# /start
# ==========================================
def cmd_start(user, chat_id, arg):
    register_user(user.id)

    if not is_subscribed(user.id):
        send_subscribe_prompt(chat_id, user.id, arg)
        return

    if arg:
        payload = get_link(arg)
        if not payload:
            bot.send_message(chat_id, "❌ <b>Lien invalide ou expiré.</b>", parse_mode="HTML")
            return
        bot.send_chat_action(chat_id, "typing")
        sent_ids = []
        for item in payload.get("items", []):
            sent = deliver_item(chat_id, item)
            if sent:
                sent_ids.append(sent.message_id)

        if get_setting("delete_enabled", False):
            del_cfg = get_setting("delete_msg")
            delay_min = del_cfg.get("delay_minutes") if del_cfg else None
            if del_cfg:
                ctx = build_ctx(user, delay=delay_min, count=len(sent_ids))
                send_rich_message(chat_id, del_cfg, ctx=ctx)
            if delay_min and sent_ids:
                threading.Timer(delay_min * 60, delete_and_offer_resend, args=(chat_id, sent_ids, arg)).start()
        return

    kb = types.InlineKeyboardMarkup()
    row = []
    main_link = get_setting("main_link")
    if main_link:
        row.append(types.InlineKeyboardButton("➤ Voir plus", url=main_link))
    row.append(types.InlineKeyboardButton("☰ Commandes", callback_data="menu_commands"))
    kb.row(*row)

    welcome = get_setting("welcome")
    if welcome:
        send_rich_message(chat_id, welcome, ctx=build_ctx(user), reply_markup=kb)
    else:
        bot.send_message(
            chat_id,
            "🎉 <b>Bienvenue !</b>\n\nEnvoie un fichier pour obtenir un lien de partage.",
            parse_mode="HTML",
            reply_markup=kb,
        )


# ==========================================
# MENU "COMMANDES"
# ==========================================
def build_commands_text(user):
    text = (
        "📖 <b>Commandes</b>\n\n"
        "<u>👤 Utilisateurs</u>\n"
        "🚀 /start — démarrer le bot\n"
        "❓ /help — afficher ce menu\n"
    )
    if is_admin(user.id):
        text += (
            "\n<u>🛠️ Administration</u>\n"
            "📎 Envoyer un fichier (ou le transférer depuis le canal) crée un lien permanent\n"
            "📦 /batch — lien pour une plage de fichiers du canal\n"
            "🚫 /cancel — annuler l'opération en cours\n"
            "🔗 /setlink — définir le lien du bouton « Voir plus »\n"
            "🔧 /placeholders — variables disponibles\n"
            "💾 /export — sauvegarder la configuration\n"
            "📥 /import — restaurer une sauvegarde\n"
        )
    return text


def send_commands_menu(chat_id, user, edit_message_id=None):
    text = build_commands_text(user)
    kb = types.InlineKeyboardMarkup()
    if is_admin(user.id):
        kb.row(types.InlineKeyboardButton("⚙️ Réglages", callback_data="menu_admin"))
    kb.row(
        types.InlineKeyboardButton("◄ Retour", callback_data="nav_start"),
        types.InlineKeyboardButton("✕ Fermer", callback_data="close_menu"),
    )
    if edit_message_id:
        edit_or_send(chat_id, edit_message_id, text, reply_markup=kb, parse_mode="HTML")
        return
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


# ==========================================
# PANNEAU RÉGLAGES — SOUS-MENUS
# ==========================================
def build_admin_menu():
    text = "⚙️ <b>Réglages</b>\n\nChoisis une catégorie."
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🔐 Statuts", callback_data="menu_status"))
    kb.row(types.InlineKeyboardButton("🗂️ Contenu", callback_data="menu_content"))
    kb.row(types.InlineKeyboardButton("📊 Statistiques", callback_data="menu_stats"))
    kb.row(types.InlineKeyboardButton("📢 Diffusion", callback_data="menu_broadcast"))
    kb.row(
        types.InlineKeyboardButton("◄ Retour", callback_data="menu_commands"),
        types.InlineKeyboardButton("✕ Fermer", callback_data="close_menu"),
    )
    return text, kb


def build_status_menu():
    fs = get_setting("force_sub") or {}
    fs_on = bool(fs.get("enabled") and fs.get("channels"))
    protect_on = protection_enabled()
    del_on = get_setting("delete_enabled", False)

    text = "🔐 <b>Statuts</b>\n\nActive ou désactive une fonction en appuyant dessus."
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(f"{'🟢' if fs_on else '🔴'} Force-sub", callback_data="fs_toggle"))
    kb.row(types.InlineKeyboardButton("📡 Gérer les canaux", callback_data="menu_manage_channels"))
    kb.row(types.InlineKeyboardButton(f"{'🟢' if protect_on else '🔴'} Protection anti-transfert", callback_data="toggle_protect"))
    kb.row(types.InlineKeyboardButton(f"{'🟢' if del_on else '🔴'} Auto-suppression", callback_data="toggle_autodelete"))
    kb.row(
        types.InlineKeyboardButton("◄ Retour", callback_data="menu_admin"),
        types.InlineKeyboardButton("✕ Fermer", callback_data="close_menu"),
    )
    return text, kb


def build_channels_menu():
    fs = get_setting("force_sub") or {"enabled": False, "channels": []}
    channels = fs.get("channels", [])
    lines = ["📡 <b>Canaux obligatoires</b>", ""]
    kb = types.InlineKeyboardMarkup()
    if not channels:
        lines.append("<i>Aucun canal configuré.</i>")
    for idx, ch in enumerate(channels):
        title = ch.get("title") or "Canal"
        lines.append(f"📡 {title}")
        kb.row(types.InlineKeyboardButton(f"✕ Retirer « {title} »", callback_data=f"fs_remove:{idx}"))
    kb.row(types.InlineKeyboardButton("➕ Ajouter un canal", callback_data="fs_add_channel_start"))
    kb.row(
        types.InlineKeyboardButton("◄ Retour", callback_data="menu_status"),
        types.InlineKeyboardButton("✕ Fermer", callback_data="close_menu"),
    )
    return "\n".join(lines), kb


def build_content_menu():
    text = "🗂️ <b>Contenu</b>\n\nPersonnalise les messages envoyés par le bot."
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✎ Modifier l'accueil", callback_data="edit_welcome"),
        types.InlineKeyboardButton("👁️ Aperçu", callback_data="preview_welcome"),
    )
    kb.row(
        types.InlineKeyboardButton("✎ Modifier la suppression", callback_data="edit_delete"),
        types.InlineKeyboardButton("👁️ Aperçu", callback_data="preview_delete"),
    )
    kb.row(
        types.InlineKeyboardButton("◄ Retour", callback_data="menu_admin"),
        types.InlineKeyboardButton("✕ Fermer", callback_data="close_menu"),
    )
    return text, kb


def build_broadcast_menu():
    text = "📢 <b>Diffusion</b>\n\nEnvoie un message à tous les utilisateurs ayant démarré le bot."
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("📝 Nouvelle diffusion", callback_data="new_broadcast"))
    kb.row(
        types.InlineKeyboardButton("◄ Retour", callback_data="menu_admin"),
        types.InlineKeyboardButton("✕ Fermer", callback_data="close_menu"),
    )
    return text, kb


def build_stats_screen():
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM links")
    total_links = cur.fetchone()[0]
    total_files = 0
    cur.execute("SELECT data FROM links")
    for (raw,) in cur.fetchall():
        total_files += len(json.loads(raw).get("items", []))

    fs = get_setting("force_sub") or {}
    fs_state = "🟢 Activé" if fs.get("enabled") and fs.get("channels") else "🔴 Désactivé"
    protect_state = "🟢 Activée" if protection_enabled() else "🔴 Désactivée"
    del_state = "🟢 Activée" if get_setting("delete_enabled", False) else "🔴 Désactivée"

    text = (
        "📊 <b>Statistiques</b>\n\n"
        f"👥 Utilisateurs — <b>{total_users}</b>\n"
        f"🔗 Liens créés — <b>{total_links}</b>\n"
        f"📁 Fichiers référencés — <b>{total_files}</b>\n\n"
        f"Force-sub — {fs_state}\n"
        f"Protection — {protect_state}\n"
        f"Auto-suppression — {del_state}"
    )
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("◄ Retour", callback_data="menu_admin"),
        types.InlineKeyboardButton("✕ Fermer", callback_data="close_menu"),
    )
    return text, kb


def show_menu(chat_id, build_fn, edit_message_id=None):
    text, kb = build_fn()
    if edit_message_id:
        edit_or_send(chat_id, edit_message_id, text, reply_markup=kb, parse_mode="HTML")
        return
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


# ==========================================
# /placeholders
# ==========================================
def cmd_placeholders(message):
    if not is_admin(message.from_user.id):
        return
    text = (
        "🔧 <b>Variables disponibles</b>\n\n"
        "✨ Insère ces balises n'importe où dans tes messages personnalisés.\n\n"
        "👋 <b>{mention}</b> — identifie l'utilisateur par son nom complet\n"
        "📅 <b>{date}</b> — date du jour\n"
        "🤖 <b>{bot_name}</b> — nom du bot\n"
        "⏱️ <b>{time}</b> — délai avant suppression <i>(message de suppression)</i>\n"
        "🔢 <b>{count}</b> — nombre de fichiers envoyés <i>(message de suppression)</i>\n\n"
        "<blockquote>Bonjour {mention} 👋, tes {count} fichier(s) seront supprimés dans {time} minutes ⏳.</blockquote>"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


# ==========================================
# Flux multi-étapes (commandes texte)
# ==========================================
def cmd_setwelcome(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "setwelcome"}
    bot.reply_to(message, "✏️ Envoie le nouveau message d'accueil (texte, image, mise en forme). Astuce : /placeholders")


def cmd_setdelete(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "setdelete_msg"}
    bot.reply_to(message, "✏️ Envoie le message affiché après l'envoi de tous les fichiers d'un lien. Astuce : /placeholders")


def cmd_setlink(message, arg):
    if not is_admin(message.from_user.id):
        return
    link = arg.strip()
    if not link.startswith("http"):
        bot.reply_to(message, "❌ Envoie une URL valide, ex : /setlink https://t.me/kinemavf")
        return
    set_setting("main_link", link)
    bot.reply_to(message, "✅ <b>Lien mis à jour.</b>", parse_mode="HTML")


def cmd_previewwelcome(message):
    if not is_admin(message.from_user.id):
        return
    welcome = get_setting("welcome")
    if not welcome:
        bot.reply_to(message, "ℹ️ Aucun message d'accueil configuré pour l'instant.")
        return
    send_rich_message(message.chat.id, welcome, ctx=build_ctx(message.from_user))


def cmd_previewdelete(message):
    if not is_admin(message.from_user.id):
        return
    del_cfg = get_setting("delete_msg")
    if not del_cfg:
        bot.reply_to(message, "ℹ️ Aucun message de suppression configuré pour l'instant.")
        return
    ctx = build_ctx(message.from_user, delay=del_cfg.get("delay_minutes", 0), count=1)
    send_rich_message(message.chat.id, del_cfg, ctx=ctx)


def cmd_broadcast(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "broadcast_content"}
    bot.reply_to(message, "📝 Envoie le texte/image à diffuser à tous les utilisateurs.")


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


def cmd_export(message):
    if not is_admin(message.from_user.id):
        return
    data = {
        "settings": {k: json.loads(v) for k, v in cur.execute("SELECT key, value FROM settings").fetchall()},
        "links": [
            {"code": c, "data": json.loads(d), "created_at": t}
            for c, d, t in cur.execute("SELECT code, data, created_at FROM links").fetchall()
        ],
        "users": [u for (u,) in cur.execute("SELECT user_id FROM users").fetchall()],
    }
    buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.name = "backup.json"
    bot.send_document(message.chat.id, buf, caption="💾 <b>Sauvegarde générée.</b>", parse_mode="HTML")


def cmd_import_start(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "import_wait_file"}
    bot.reply_to(message, "📥 Envoie le fichier .json généré par /export.")


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
        content = capture_rich_message(message)
        set_setting("welcome", content)
        unknown = find_unknown_placeholders(content["text"])
        msg = "✅ <b>Accueil mis à jour avec succès !</b> 🎉"
        if unknown:
            msg += f"\n⚠️ <i>Variable(s) inconnue(s) ignorée(s) :</i> {', '.join('{' + u + '}' for u in unknown)}"
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("👁️ Voir l'aperçu", callback_data="preview_welcome"),
            types.InlineKeyboardButton("◄ Retour", callback_data="menu_content"),
        )
        panel_reply(message, state, msg, reply_markup=kb)
        pending.pop(uid)
        return True

    if action == "setdelete_msg":
        state["data"] = capture_rich_message(message)
        state["action"] = "setdelete_time"
        panel_reply(message, state, "⏱️ Après combien de minutes les fichiers doivent-ils être supprimés ?")
        return True

    if action == "setdelete_time":
        try:
            minutes = int((message.text or "").strip())
        except ValueError:
            panel_reply(message, state, "❌ Envoie un nombre entier de minutes (ex : 10). 🔢")
            return True
        data = state["data"]
        data["delay_minutes"] = minutes
        set_setting("delete_msg", data)
        unknown = find_unknown_placeholders(data["text"])
        msg = f"✅ <b>Message et délai enregistrés</b> ⏱️ ({minutes} min)."
        if unknown:
            msg += f"\n⚠️ <i>Variable(s) inconnue(s) ignorée(s) :</i> {', '.join('{' + u + '}' for u in unknown)}"
        if not get_setting("delete_enabled", False):
            msg += "\nℹ️ <i>Rappel : la suppression automatique est actuellement désactivée.</i> 🔴"
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("👁️ Voir l'aperçu", callback_data="preview_delete"),
            types.InlineKeyboardButton("◄ Retour", callback_data="menu_content"),
        )
        panel_reply(message, state, msg, reply_markup=kb)
        pending.pop(uid)
        return True

    if action == "fs_add_channel":
        chat_id2, _ = get_forward_channel_msgid(message)
        if not chat_id2:
            panel_reply(message, state, "❌ Ce n'est pas un message transféré depuis un canal. Réessaie. 🔁")
            return True
        fs_existing = get_setting("force_sub") or {"enabled": False, "channels": []}
        if any(c["channel_id"] == chat_id2 for c in fs_existing.get("channels", [])):
            panel_reply(message, state, "ℹ️ Ce canal est déjà dans la liste. 📡")
            pending.pop(uid)
            return True

        try:
            title = bot.get_chat(chat_id2).title
        except Exception:
            title = "Canal"

        try:
            invite_link = bot.create_chat_invite_link(chat_id2, name="File-Store-Bot").invite_link
        except Exception:
            state["data"] = {"channel_id": chat_id2, "title": title}
            state["action"] = "fs_add_link"
            panel_reply(
                message, state,
                "⚠️ Impossible de générer un lien automatiquement (le bot doit être admin de ce canal avec le "
                "droit « Inviter des utilisateurs via un lien »).\n\n"
                "✏️ Envoie-moi manuellement le lien d'invitation public de ce canal (https://t.me/...)."
            )
            return True

        fs = get_setting("force_sub") or {"enabled": False, "channels": []}
        fs.setdefault("channels", []).append({"channel_id": chat_id2, "invite_link": invite_link, "title": title})
        fs["enabled"] = True
        set_setting("force_sub", fs)
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("➕ Ajouter un autre canal", callback_data="fs_add_channel_start"))
        kb.row(types.InlineKeyboardButton("✅ Terminé", callback_data="menu_status"))
        panel_reply(message, state, f"✅ <b>Canal ajouté avec succès</b> — {title} 📡🎉", reply_markup=kb)
        pending.pop(uid)
        return True

    if action == "fs_add_link":
        link = (message.text or "").strip()
        if not link.startswith("http"):
            panel_reply(message, state, "❌ Ce n'est pas un lien valide. Réessaie. 🔗")
            return True
        channel_id = state["data"]["channel_id"]
        title = state["data"].get("title", "Canal")
        fs = get_setting("force_sub") or {"enabled": False, "channels": []}
        fs.setdefault("channels", []).append({"channel_id": channel_id, "invite_link": link, "title": title})
        fs["enabled"] = True
        set_setting("force_sub", fs)
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("➕ Ajouter un autre canal", callback_data="fs_add_channel_start"))
        kb.row(types.InlineKeyboardButton("✅ Terminé", callback_data="menu_status"))
        panel_reply(message, state, f"✅ <b>Canal ajouté avec succès</b> — {title} 📡🎉", reply_markup=kb)
        pending.pop(uid)
        return True

    if action == "broadcast_content":
        content = capture_rich_message(message)
        state["data"] = content
        state["action"] = "broadcast_button"
        panel_reply(
            message, state,
            "🔘 Veux-tu ajouter un bouton ?\n"
            "✏️ Envoie-le sous la forme : Texte - https://lien.com\n"
            "Ou tape /skip pour diffuser sans bouton. ⏭️"
        )
        return True

    if action == "broadcast_button":
        content = state["data"]

        kb = None
        warning = ""
        raw = (message.text or "").strip()
        if raw and raw.lower() != "/skip":
            if " - http" in raw:
                label, url = raw.split(" - ", 1)
                label, url = label.strip(), url.strip()
                if url.startswith("http"):
                    kb = types.InlineKeyboardMarkup()
                    kb.row(types.InlineKeyboardButton(label or "➤ Voir plus", url=url))
                else:
                    warning = "\n⚠️ <i>Lien invalide, diffusion envoyée sans bouton.</i>"
            else:
                warning = "\n⚠️ <i>Format non reconnu, diffusion envoyée sans bouton.</i>"

        cur.execute("SELECT user_id FROM users")
        user_ids = [row[0] for row in cur.fetchall()]
        sent, failed = 0, 0
        for target in user_ids:
            try:
                if send_rich_message(target, content, reply_markup=kb):
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        msg = f"📬 <b>Diffusion terminée !</b> 🎉\n✅ Envoyés : {sent} — ❌ Échecs : {failed}{warning}"
        kb_back = types.InlineKeyboardMarkup()
        kb_back.row(types.InlineKeyboardButton("◄ Retour", callback_data="menu_broadcast"))
        panel_reply(message, state, msg, reply_markup=kb_back)
        pending.pop(uid)
        return True

    if action == "batch_first":
        chat_id2, msg_id2 = get_forward_channel_msgid(message)
        if chat_id2 != DB_CHANNEL:
            bot.reply_to(message, "❌ Ce message ne vient pas du canal privé configuré. Réessaie.")
            return True
        state["data"] = {"first": msg_id2}
        state["action"] = "batch_last"
        bot.reply_to(message, "📦 Reçu. Transfère-moi maintenant le DERNIER fichier de la plage.")
        return True

    if action == "batch_last":
        chat_id2, msg_id2 = get_forward_channel_msgid(message)
        if chat_id2 != DB_CHANNEL:
            bot.reply_to(message, "❌ Ce message ne vient pas du canal privé configuré. Réessaie.")
            return True
        first = state["data"]["first"]
        start, end = min(first, msg_id2), max(first, msg_id2)
        pending.pop(uid)
        bot.reply_to(message, f"⏳ Récupération de {end - start + 1} message(s), merci de patienter...")
        items = resolve_batch_range(message.chat.id, start, end)
        if not items:
            bot.reply_to(message, "❌ Aucun fichier valide trouvé dans cette plage.")
            return True
        code = save_link({"type": "files", "items": items})
        bot.reply_to(message, f"✅ <b>Lien permanent créé</b> pour {len(items)} fichier(s) :\n{make_link(code)}", parse_mode="HTML")
        return True

    if action == "import_wait_file":
        if message.content_type != "document":
            bot.reply_to(message, "📥 Envoie le fichier .json en tant que document.")
            return True
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            data = json.loads(downloaded.decode("utf-8"))
            for k, v in data.get("settings", {}).items():
                set_setting(k, v)
            for link in data.get("links", []):
                cur.execute(
                    "INSERT OR REPLACE INTO links (code, data, created_at) VALUES (?, ?, ?)",
                    (link["code"], json.dumps(link["data"]), link.get("created_at", "")),
                )
            for uid2 in data.get("users", []):
                register_user(uid2)
            conn.commit()
            pending.pop(uid)
            bot.reply_to(message, "✅ <b>Sauvegarde restaurée.</b>", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ Échec de la restauration : {e}")
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
    bot.reply_to(message, f"✅ <b>Lien permanent créé :</b>\n{make_link(code)}", parse_mode="HTML")


# ==========================================
# CALLBACKS (boutons)
# ==========================================
@bot.callback_query_handler(func=lambda c: True)
def callback_router(call):
    data = call.data
    user = call.from_user
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data.startswith("checksub:"):
        arg = data.split(":", 1)[1]
        arg = None if arg == "_" else arg
        if is_subscribed(user.id):
            bot.answer_callback_query(call.id, "✅ Abonnement confirmé.")
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            cmd_start(user, chat_id, arg)
        else:
            bot.answer_callback_query(call.id, "❌ Tu n'as pas encore rejoint le(s) canal(aux).", show_alert=True)
        return

    if data.startswith("resend:"):
        code = data.split(":", 1)[1]
        bot.answer_callback_query(call.id, "🔄 Renvoi en cours...")
        cmd_start(user, chat_id, code)
        return

    if data == "nav_start":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        cmd_start(user, chat_id, None)
        return

    if data == "menu_commands":
        bot.answer_callback_query(call.id)
        send_commands_menu(chat_id, user, edit_message_id=msg_id)
        return

    if data == "close_menu":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        return

    if not is_admin(user.id):
        bot.answer_callback_query(call.id, "⛔ Réservé aux admins.", show_alert=True)
        return

    if data == "menu_admin":
        bot.answer_callback_query(call.id)
        show_menu(chat_id, build_admin_menu, msg_id)
        return

    if data == "menu_status":
        bot.answer_callback_query(call.id)
        show_menu(chat_id, build_status_menu, msg_id)
        return

    if data == "menu_content":
        bot.answer_callback_query(call.id)
        show_menu(chat_id, build_content_menu, msg_id)
        return

    if data == "menu_stats":
        bot.answer_callback_query(call.id)
        show_menu(chat_id, build_stats_screen, msg_id)
        return

    if data == "menu_broadcast":
        bot.answer_callback_query(call.id)
        show_menu(chat_id, build_broadcast_menu, msg_id)
        return

    if data == "menu_manage_channels":
        bot.answer_callback_query(call.id)
        show_menu(chat_id, build_channels_menu, msg_id)
        return

    if data == "fs_toggle":
        fs = get_setting("force_sub") or {"enabled": False, "channels": []}
        if not fs.get("channels"):
            bot.answer_callback_query(call.id)
            pending[user.id] = {"action": "fs_add_channel", "panel_chat_id": chat_id, "panel_msg_id": msg_id}
            edit_or_send(chat_id, msg_id, "📡 Transfère un message du canal à rendre obligatoire (le bot doit déjà en être admin ✅).")
            return
        fs["enabled"] = not fs.get("enabled", False)
        set_setting("force_sub", fs)
        bot.answer_callback_query(call.id, "Force-sub " + ("activé ✅" if fs["enabled"] else "désactivé ❌"))
        show_menu(chat_id, build_status_menu, msg_id)
        return

    if data == "fs_add_channel_start":
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "fs_add_channel", "panel_chat_id": chat_id, "panel_msg_id": msg_id}
        edit_or_send(chat_id, msg_id, "📡 Transfère un message du canal à ajouter (le bot doit déjà en être admin ✅).")
        return

    if data.startswith("fs_remove:"):
        idx = int(data.split(":", 1)[1])
        fs = get_setting("force_sub") or {"enabled": False, "channels": []}
        channels = fs.get("channels", [])
        if 0 <= idx < len(channels):
            channels.pop(idx)
        fs["channels"] = channels
        if not channels:
            fs["enabled"] = False
        set_setting("force_sub", fs)
        bot.answer_callback_query(call.id, "🗑️ Canal retiré.")
        show_menu(chat_id, build_channels_menu, msg_id)
        return

    if data == "toggle_protect":
        value = not protection_enabled()
        set_setting("protect_content", value)
        bot.answer_callback_query(call.id, "Protection " + ("activée ✅" if value else "désactivée ❌"))
        show_menu(chat_id, build_status_menu, msg_id)
        return

    if data == "toggle_autodelete":
        value = not get_setting("delete_enabled", False)
        set_setting("delete_enabled", value)
        bot.answer_callback_query(call.id, "Auto-suppression " + ("activée ✅" if value else "désactivée ❌"))
        show_menu(chat_id, build_status_menu, msg_id)
        return

    if data == "edit_welcome":
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "setwelcome", "panel_chat_id": chat_id, "panel_msg_id": msg_id}
        edit_or_send(chat_id, msg_id, "✏️ Envoie le nouveau message d'accueil (texte, image, mise en forme). 🖼️")
        return

    if data == "preview_welcome":
        bot.answer_callback_query(call.id)
        welcome = get_setting("welcome")
        if not welcome:
            bot.send_message(chat_id, "ℹ️ <i>Aucun message d'accueil configuré.</i> ✏️", parse_mode="HTML")
        else:
            kb_close = types.InlineKeyboardMarkup()
            kb_close.row(types.InlineKeyboardButton("🗑️ Fermer l'aperçu", callback_data="close_menu"))
            send_rich_message(chat_id, welcome, ctx=build_ctx(user), reply_markup=kb_close)
        return

    if data == "edit_delete":
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "setdelete_msg", "panel_chat_id": chat_id, "panel_msg_id": msg_id}
        edit_or_send(chat_id, msg_id, "✏️ Envoie le message qui doit s'afficher après l'envoi de tous les fichiers d'un lien. 📩")
        return

    if data == "preview_delete":
        bot.answer_callback_query(call.id)
        del_cfg = get_setting("delete_msg")
        if not del_cfg:
            bot.send_message(chat_id, "ℹ️ <i>Aucun message de suppression configuré.</i> ✏️", parse_mode="HTML")
        else:
            ctx = build_ctx(user, delay=del_cfg.get("delay_minutes", 0), count=1)
            kb_close = types.InlineKeyboardMarkup()
            kb_close.row(types.InlineKeyboardButton("🗑️ Fermer l'aperçu", callback_data="close_menu"))
            send_rich_message(chat_id, del_cfg, ctx=ctx, reply_markup=kb_close)
        return

    if data == "new_broadcast":
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "broadcast_content", "panel_chat_id": chat_id, "panel_msg_id": msg_id}
        edit_or_send(chat_id, msg_id, "📝 Envoie le texte/image à diffuser à tous les utilisateurs. 📢")
        return


# ==========================================
# ROUTAGE PRINCIPAL
# ==========================================
COMMANDS = {
    "start": lambda m, a: cmd_start(m.from_user, m.chat.id, a),
    "help": lambda m, a: send_commands_menu(m.chat.id, m.from_user),
    "placeholders": lambda m, a: cmd_placeholders(m),
    "setwelcome": lambda m, a: cmd_setwelcome(m),
    "setdelete": lambda m, a: cmd_setdelete(m),
    "setlink": lambda m, a: cmd_setlink(m, a),
    "previewwelcome": lambda m, a: cmd_previewwelcome(m),
    "previewdelete": lambda m, a: cmd_previewdelete(m),
    "broadcast": lambda m, a: cmd_broadcast(m),
    "batch": lambda m, a: cmd_batch(m),
    "cancel": lambda m, a: cmd_cancel(m),
    "export": lambda m, a: cmd_export(m),
    "import": lambda m, a: cmd_import_start(m),
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
