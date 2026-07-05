import os
import re
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

pending = {}

PLACEHOLDER_KEYS = ("mention", "date", "time", "bot_name", "count")


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
# HELPERS - ENTITÉS (gras, italique, citation, code, liens...)
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
# HELPERS - VARIABLES DYNAMIQUES ({mention}, {date}, {time}...)
# ==========================================
def build_mention(user):
    if user.username:
        return f"@{user.username}", None
    name = user.first_name or "Utilisateur"
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

    pieces = []
    last_end = 0
    mention_entities = []
    deltas = []
    for start, end, key in matches:
        pieces.append(text[last_end:start])
        cur_offset = sum(len(p) for p in pieces)
        if key == "mention":
            disp, mention_user = ctx["mention"]
            if mention_user is not None:
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
        for (start, _end, _key), delta in zip(matches, deltas):
            if start < pos:
                total += delta
        return pos + total

    result_entities = []
    for e in entities_dicts or []:
        result_entities.append(types.MessageEntity(
            type=e["type"], offset=shift(e["offset"]), length=e["length"], url=e.get("url")
        ))
    for offset, length, user in mention_entities:
        result_entities.append(types.MessageEntity(
            type="text_mention", offset=offset, length=length, user=user
        ))
    return new_text, (result_entities or None)


def send_rich_message(chat_id, content, ctx=None, reply_markup=None):
    raw_text = content.get("text") or ""
    if ctx:
        text, entities = render_entities(raw_text, content.get("entities"), ctx)
    else:
        text, entities = raw_text, dicts_to_entities(content.get("entities"))
    if content.get("photo"):
        return bot.send_photo(
            chat_id, content["photo"], caption=text or None,
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
# HELPERS - LIVRAISON D'UN FICHIER (via file_id)
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
        return bot.send_document(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
    if ct == "photo":
        return bot.send_photo(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
    if ct == "video":
        return bot.send_video(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
    if ct == "audio":
        return bot.send_audio(chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
    if ct == "sticker":
        return bot.send_sticker(chat_id, fid, protect_content=protect)
    return None


def delete_many(chat_id, message_ids):
    for mid in message_ids:
        try:
            bot.delete_message(chat_id, mid)
        except Exception:
            pass


# ==========================================
# /start
# ==========================================
def cmd_start(user, chat_id, arg):
    register_user(user.id)

    if not is_subscribed(user.id):
        send_subscribe_prompt(chat_id, arg)
        return

    if arg:
        payload = get_link(arg)
        if not payload:
            bot.send_message(chat_id, "❌ Ce lien est invalide ou a expiré.")
            return
        bot.send_chat_action(chat_id, "typing")
        sent_ids = []
        for item in payload.get("items", []):
            try:
                sent = deliver_item(chat_id, item)
                if sent:
                    sent_ids.append(sent.message_id)
            except Exception:
                continue

        if get_setting("delete_enabled", False):
            del_cfg = get_setting("delete_msg")
            delay_min = del_cfg.get("delay_minutes") if del_cfg else None
            if del_cfg:
                ctx = build_ctx(user, delay=delay_min, count=len(sent_ids))
                send_rich_message(chat_id, del_cfg, ctx=ctx)
            if delay_min:
                threading.Timer(delay_min * 60, delete_many, args=(chat_id, sent_ids)).start()
        return

    kb = types.InlineKeyboardMarkup()
    row = []
    main_link = get_setting("main_link")
    if main_link:
        row.append(types.InlineKeyboardButton("🔎 Voir plus", url=main_link))
    row.append(types.InlineKeyboardButton("📋 Commandes", callback_data="menu_commands"))
    kb.row(*row)

    welcome = get_setting("welcome")
    if welcome:
        send_rich_message(chat_id, welcome, ctx=build_ctx(user), reply_markup=kb)
    else:
        bot.send_message(
            chat_id,
            "👋 Bienvenue !\n\nEnvoie-moi un fichier, je te donnerai un lien de partage.",
            reply_markup=kb,
        )


# ==========================================
# MENU "COMMANDES"
# ==========================================
def build_commands_text(user):
    text = (
        "📋 *Commandes*\n\n"
        "*Pour tout le monde*\n"
        "/start – démarrer le bot\n"
        "/help – afficher ce menu\n"
    )
    if is_admin(user.id):
        text += (
            "\n*Réservées aux admins*\n"
            "Envoyer un fichier (ou en forwarder un depuis le canal) – crée un lien permanent\n"
            "/batch – lien pour une plage de fichiers du canal\n"
            "/cancel – annuler l'opération en cours\n"
            "/setlink – définir le lien du bouton \"Voir plus\"\n"
            "/placeholders – variables utilisables dans tes messages\n"
        )
    return text


def send_commands_menu(chat_id, user, edit_message_id=None):
    text = build_commands_text(user)
    kb = types.InlineKeyboardMarkup()
    if is_admin(user.id):
        kb.row(types.InlineKeyboardButton("⚙️ Réglages admin", callback_data="menu_admin"))
    kb.row(
        types.InlineKeyboardButton("🔙 Retour", callback_data="nav_start"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
    )
    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id, edit_message_id, parse_mode="Markdown", reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


# ==========================================
# PANNEAU "RÉGLAGES ADMIN" (boutons ON/OFF)
# ==========================================
def build_admin_panel():
    protect_state = "✅ ON" if protection_enabled() else "❌ OFF"
    fs = get_setting("force_sub")
    fs_state = "✅ ON" if fs and fs.get("enabled") else "❌ OFF"
    del_state = "✅ ON" if get_setting("delete_enabled", False) else "❌ OFF"

    text = "⚙️ *Réglages admin*\n\nAppuie sur un bouton pour changer un réglage."
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(f"🔒 Force-sub : {fs_state}", callback_data="toggle_forcesub"))
    kb.row(types.InlineKeyboardButton(f"🛡️ Protection : {protect_state}", callback_data="toggle_protect"))
    kb.row(types.InlineKeyboardButton(f"🗑️ Auto-suppression : {del_state}", callback_data="toggle_autodelete"))
    kb.row(
        types.InlineKeyboardButton("📊 Stats", callback_data="show_stats"),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="start_broadcast"),
    )
    kb.row(
        types.InlineKeyboardButton("✏️ Accueil", callback_data="start_setwelcome"),
        types.InlineKeyboardButton("✏️ Suppression", callback_data="start_setdelete"),
    )
    kb.row(
        types.InlineKeyboardButton("🔙 Retour", callback_data="menu_commands"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
    )
    return text, kb


def send_admin_panel(chat_id, edit_message_id=None):
    text, kb = build_admin_panel()
    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id, edit_message_id, parse_mode="Markdown", reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


# ==========================================
# /placeholders
# ==========================================
def cmd_placeholders(message):
    if not is_admin(message.from_user.id):
        return
    text = (
        "🔧 *Variables disponibles*\n\n"
        "Utilisables dans le message d'accueil et le message de suppression. "
        "Tape-les telles quelles dans ton texte.\n\n"
        "`{mention}` – tague l'utilisateur (fonctionne même sans @pseudo)\n"
        "`{date}` – date du jour\n"
        "`{bot_name}` – nom d'utilisateur du bot\n"
        "`{time}` – délai avant suppression en minutes *(message de suppression)*\n"
        "`{count}` – nombre de fichiers envoyés *(message de suppression)*\n\n"
        "*Exemple :*\n"
        "`Bonjour {mention} ! Tes {count} fichier(s) seront supprimés dans {time} minutes.`"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")


# ==========================================
# /stats
# ==========================================
def cmd_stats(user, chat_id):
    if not is_admin(user.id):
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
    del_state = "Activée ✅" if get_setting("delete_enabled", False) else "Désactivée ❌"

    bot.send_message(
        chat_id,
        "📊 *Statistiques*\n\n"
        f"👤 Utilisateurs uniques : {total_users}\n"
        f"🔗 Liens créés : {total_links}\n"
        f"📁 Fichiers référencés : {total_files}\n"
        f"🔒 Abonnement obligatoire : {fs_state}\n"
        f"🛡️ Protection anti-transfert : {protect}\n"
        f"🗑️ Suppression automatique : {del_state}",
        parse_mode="Markdown",
    )


# ==========================================
# Démarrage des flux multi-étapes
# ==========================================
def cmd_setwelcome(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "setwelcome"}
    bot.reply_to(
        message,
        "✏️ Envoie maintenant le nouveau message d'accueil (texte, image, mise en forme).\n"
        "Astuce : /placeholders pour voir les variables disponibles."
    )


def cmd_setdelete(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "setdelete_msg"}
    bot.reply_to(
        message,
        "✏️ Envoie le message à afficher une seule fois, après l'envoi de tous les fichiers d'un lien.\n"
        "Astuce : /placeholders pour voir les variables disponibles (ex: {time}, {count})."
    )


def cmd_setlink(message, arg):
    if not is_admin(message.from_user.id):
        return
    link = arg.strip()
    if not link.startswith("http"):
        bot.reply_to(message, "❌ Envoie une URL valide, ex : /setlink https://t.me/kinemavf")
        return
    set_setting("main_link", link)
    bot.reply_to(message, "✅ Lien du bouton \"Voir plus\" mis à jour.")


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


def cmd_setforcesub(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "setforcesub_channel"}
    bot.reply_to(message, "📡 Transfère-moi (forward) n'importe quel message du canal à rendre obligatoire.")


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
        content = capture_rich_message(message)
        set_setting("welcome", content)
        pending.pop(uid)
        unknown = find_unknown_placeholders(content["text"])
        msg = "✅ Message d'accueil mis à jour."
        if unknown:
            msg += f"\n⚠️ Variable(s) inconnue(s) ignorée(s) : {', '.join('{' + u + '}' for u in unknown)}"
        bot.reply_to(message, msg)
        return True

    if action == "setdelete_msg":
        content = capture_rich_message(message)
        state["data"] = content
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
        unknown = find_unknown_placeholders(data["text"])
        msg = f"✅ Message et délai ({minutes} min) enregistrés."
        if unknown:
            msg += f"\n⚠️ Variable(s) inconnue(s) ignorée(s) : {', '.join('{' + u + '}' for u in unknown)}"
        if not get_setting("delete_enabled", False):
            msg += "\nℹ️ Rappel : la suppression automatique est actuellement désactivée."
        bot.reply_to(message, msg)
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
            bot.answer_callback_query(call.id, "✅ Abonnement confirmé !")
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            cmd_start(user, chat_id, arg)
        else:
            bot.answer_callback_query(call.id, "❌ Tu n'as pas encore rejoint le canal.", show_alert=True)
        return

    if data == "nav_start":
        bot.answer_callback_query(call.id)
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

    # À partir d'ici : réservé aux admins
    if not is_admin(user.id):
        bot.answer_callback_query(call.id, "⛔ Réservé aux admins.", show_alert=True)
        return

    if data == "menu_admin":
        bot.answer_callback_query(call.id)
        send_admin_panel(chat_id, edit_message_id=msg_id)
        return

    if data == "toggle_forcesub":
        fs = get_setting("force_sub")
        if not fs:
            bot.answer_callback_query(call.id, "⚠️ Configure d'abord via /setforcesub.", show_alert=True)
            return
        fs["enabled"] = not fs.get("enabled", False)
        set_setting("force_sub", fs)
        bot.answer_callback_query(call.id, f"Force-sub {'activé' if fs['enabled'] else 'désactivé'}")
        send_admin_panel(chat_id, edit_message_id=msg_id)
        return

    if data == "toggle_protect":
        value = not protection_enabled()
        set_setting("protect_content", value)
        bot.answer_callback_query(call.id, f"Protection {'activée' if value else 'désactivée'}")
        send_admin_panel(chat_id, edit_message_id=msg_id)
        return

    if data == "toggle_autodelete":
        value = not get_setting("delete_enabled", False)
        set_setting("delete_enabled", value)
        bot.answer_callback_query(call.id, f"Auto-suppression {'activée' if value else 'désactivée'}")
        send_admin_panel(chat_id, edit_message_id=msg_id)
        return

    if data == "show_stats":
        bot.answer_callback_query(call.id)
        cmd_stats(user, chat_id)
        return

    if data == "start_broadcast":
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "broadcast"}
        bot.send_message(chat_id, "📢 Envoie le message à diffuser à tous les utilisateurs.")
        return

    if data == "start_setwelcome":
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "setwelcome"}
        bot.send_message(chat_id, "✏️ Envoie maintenant le nouveau message d'accueil (texte, image, mise en forme).")
        return

    if data == "start_setdelete":
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "setdelete_msg"}
        bot.send_message(chat_id, "✏️ Envoie le message à afficher après l'envoi de tous les fichiers d'un lien.")
        return


# ==========================================
# ROUTAGE PRINCIPAL
# ==========================================
COMMANDS = {
    "start": lambda m, a: cmd_start(m.from_user, m.chat.id, a),
    "help": lambda m, a: send_commands_menu(m.chat.id, m.from_user),
    "placeholders": lambda m, a: cmd_placeholders(m),
    "stats": lambda m, a: cmd_stats(m.from_user, m.chat.id),
    "setwelcome": lambda m, a: cmd_setwelcome(m),
    "setdelete": lambda m, a: cmd_setdelete(m),
    "setlink": lambda m, a: cmd_setlink(m, a),
    "previewwelcome": lambda m, a: cmd_previewwelcome(m),
    "previewdelete": lambda m, a: cmd_previewdelete(m),
    "setforcesub": lambda m, a: cmd_setforcesub(m),
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
