import os
import re
import io
import json
import sqlite3
import time
import uuid
import threading
from collections import deque

import telebot
from telebot import types, apihelper

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
DB_PATH = os.environ.get("DB_PATH", "store.db")

if not BOT_TOKEN:
    raise SystemExit("Variable BOT_TOKEN manquante.")

ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip()]
if not ADMIN_IDS:
    raise SystemExit("Variable ADMIN_IDS manquante (ton user ID Telegram).")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)


# ==========================================
# DÉBIT VERS L'API TELEGRAM (nécessaire à grande échelle : Telegram tolère
# environ 30 requêtes/seconde au global et ~1/seconde par utilisateur — sans
# ça, diffuser à des dizaines de milliers d'utilisateurs déclenche des
# "429 Too Many Requests" et une partie des envois se perd silencieusement).
# ==========================================
class RateLimiter:
    def __init__(self, max_per_second=25):
        self.max_per_second = max_per_second
        self.lock = threading.Lock()
        self.timestamps = deque()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            while self.timestamps and now - self.timestamps[0] > 1:
                self.timestamps.popleft()
            if len(self.timestamps) >= self.max_per_second:
                sleep_for = 1 - (now - self.timestamps[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
            self.timestamps.append(now)


rate_limiter = RateLimiter(max_per_second=25)


def safe_api_call(fn, *args, max_retries=3, **kwargs):
    """Appelle l'API Telegram en respectant le débit global ci-dessus, et réessaie
    automatiquement (avec la pause exigée par Telegram) en cas de '429 Too Many Requests'."""
    for attempt in range(max_retries + 1):
        rate_limiter.wait()
        try:
            return fn(*args, **kwargs)
        except apihelper.ApiTelegramException as e:
            if e.error_code == 429 and attempt < max_retries:
                retry_after = 3
                try:
                    retry_after = e.result_json.get("parameters", {}).get("retry_after", 3)
                except Exception:
                    pass
                print(f"⚠️ [safe_api_call] 429 reçu, pause {retry_after}s (tentative {attempt + 1}/{max_retries + 1})")
                time.sleep(retry_after + 0.5)
                continue
            raise
    return None


# ==========================================
# BASE DE DONNÉES
# ==========================================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
# WAL : les lectures (délivrance de fichiers) ne bloquent plus les écritures (nouveaux
# utilisateurs, nouveaux liens) et inversement — important dès que le trafic grossit.
cur.execute("PRAGMA journal_mode=WAL")
cur.execute("PRAGMA synchronous=NORMAL")
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
cur.execute("""CREATE TABLE IF NOT EXISTS countdowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    message_id INTEGER,
    has_photo INTEGER,
    raw_text TEXT,
    entities TEXT,
    sent_ids TEXT,
    code TEXT,
    user_id INTEGER,
    user_first_name TEXT,
    user_last_name TEXT,
    remaining_minutes INTEGER
)""")
conn.commit()

pending = {}

PLACEHOLDER_KEYS = ("mention", "date", "time", "bot_name", "count")

CONTENT_LABELS = {
    "welcome": "Message d'accueil",
    "delete_msg": "Message de suppression",
    "resend_msg": "Message de renvoi de fichier",
}
CONTENT_NUMBERING = {"welcome": "1️⃣", "delete_msg": "2️⃣", "resend_msg": "3️⃣"}


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


def empty_content():
    return {"text": "", "entities": [], "photo": None}


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


def entity_kwargs(e, offset, length):
    kwargs = dict(type=e["type"], offset=offset, length=length)
    if e.get("url"):
        kwargs["url"] = e["url"]
    if e.get("user_id"):
        kwargs["user"] = types.User(id=e["user_id"], is_bot=False, first_name=e.get("user_first_name") or "Utilisateur")
    if e.get("language"):
        kwargs["language"] = e["language"]
    if e.get("custom_emoji_id"):
        kwargs["custom_emoji_id"] = e["custom_emoji_id"]
    return kwargs


def dicts_to_entities(items):
    if not items:
        return None
    return [types.MessageEntity(**entity_kwargs(it, it["offset"], it["length"])) for it in items]


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


def format_countdown(seconds):
    """Format 'HHhMMm' (sans secondes), pour un compte à rebours qui avance minute par minute."""
    total_minutes = max(0, int(round(seconds))) // 60
    h, m = divmod(total_minutes, 60)
    return f"{h:02d}h{m:02d}m"


def build_ctx(user, delay=None, count=None):
    return {
        "mention": build_mention(user),
        "date": time.strftime("%d/%m/%Y"),
        "time": format_countdown(delay * 60) if delay is not None else "",
        "bot_name": bot.get_me().username,
        "count": str(count) if count is not None else "",
    }


def utf16_len(s):
    """Longueur d'une chaîne en unités UTF-16, comme l'exige l'API Telegram pour offset/length
    (un point de code Python peut valoir 1 ou 2 unités UTF-16, ex: certains emojis)."""
    return len(s.encode("utf-16-le")) // 2


def render_entities(text, entities_dicts, ctx):
    pattern = re.compile(r"\{(" + "|".join(PLACEHOLDER_KEYS) + r")\}")
    matches = [(m.start(), m.end(), m.group(1)) for m in pattern.finditer(text)]
    if not matches:
        return text, dicts_to_entities(entities_dicts)

    pieces, last_end, mention_entities, deltas = [], 0, [], []
    for start, end, key in matches:
        pieces.append(text[last_end:start])
        cur_offset = sum(utf16_len(p) for p in pieces)
        if key == "mention":
            disp, mention_user = ctx["mention"]
            mention_entities.append((cur_offset, utf16_len(disp), mention_user))
            replacement = disp
        else:
            replacement = str(ctx.get(key, ""))
        pieces.append(replacement)
        deltas.append(utf16_len(replacement) - utf16_len(text[start:end]))
        last_end = end
    pieces.append(text[last_end:])
    new_text = "".join(pieces)

    # `matches` donne des positions en points de code Python : on les convertit
    # une fois en offsets UTF-16 pour comparer correctement avec les offsets
    # (déjà en UTF-16) stockés dans entities_dicts.
    match_offsets_utf16 = [utf16_len(text[:start]) for start, _e, _k in matches]

    def shift_and_grow(offset, length):
        """Décale l'offset d'une entité selon les remplacements qui la précèdent,
        ET agrandit/rétrécit sa longueur si un remplacement a lieu À L'INTÉRIEUR
        de son intervalle (ex: {mention} dans une citation) — sans ce 2e correctif,
        une citation/gras contenant {mention} se termine au mauvais endroit dès que
        le nom remplacé n'a pas exactement la même longueur que "{mention}"."""
        new_offset = offset
        growth = 0
        for match_offset, delta in zip(match_offsets_utf16, deltas):
            if match_offset < offset:
                new_offset += delta
            elif offset <= match_offset < offset + length:
                growth += delta
        return new_offset, length + growth

    result_entities = []
    for e in entities_dicts or []:
        new_off, new_len = shift_and_grow(e["offset"], e["length"])
        result_entities.append(types.MessageEntity(**entity_kwargs(e, new_off, new_len)))
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
            return safe_api_call(bot.send_photo, chat_id, content["photo"], caption=text or None,
                                  caption_entities=entities, reply_markup=reply_markup)
        return safe_api_call(bot.send_message, chat_id, text or " ", entities=entities, reply_markup=reply_markup)
    except Exception as e:
        print(f"⚠️ [send_rich_message] Envoi AVEC mise en forme échoué pour chat_id={chat_id} : {e!r}")
        try:
            if content.get("photo"):
                return safe_api_call(bot.send_photo, chat_id, content["photo"], caption=text or None, reply_markup=reply_markup)
            return safe_api_call(bot.send_message, chat_id, text or " ", reply_markup=reply_markup)
        except Exception as e2:
            print(f"⚠️ [send_rich_message] Envoi SANS mise en forme aussi échoué pour chat_id={chat_id} : {e2!r}")
            return None


# ==========================================
# ÉDITION EN PLACE (pour garder l'interaction dans le même message)
# ==========================================
def edit_or_send(chat_id, msg_id, text, reply_markup=None, parse_mode=None):
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
    """Termine une étape d'un flux déclenché par un bouton en éditant le panneau
    d'origine au lieu d'envoyer un nouveau message, et nettoie le message entrant."""
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


def back_to_content_kb(key=None):
    kb = types.InlineKeyboardMarkup()
    if key:
        kb.row(types.InlineKeyboardButton("👁️ Aperçu", callback_data=f"cprev:{key}"))
    kb.row(types.InlineKeyboardButton("⬅️ Retour", callback_data="menu_content"))
    return kb


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
# BOUTONS URL D'UN MESSAGE TRANSFÉRÉ (pour le broadcast)
# ==========================================
def extract_url_keyboard(message):
    """Si le message transféré a déjà des boutons URL (ex: post de canal),
    on les récupère pour les reproduire tels quels dans la diffusion."""
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "keyboard", None) if markup else None
    if not rows:
        return None
    kb = types.InlineKeyboardMarkup()
    found = False
    for row in rows:
        new_row = []
        for btn in row:
            url = getattr(btn, "url", None)
            if url:
                new_row.append(types.InlineKeyboardButton(btn.text, url=url))
                found = True
        if new_row:
            kb.row(*new_row)
    return kb if found else None


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
            return safe_api_call(bot.send_document, chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
        if ct == "photo":
            return safe_api_call(bot.send_photo, chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
        if ct == "video":
            return safe_api_call(bot.send_video, chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
        if ct == "audio":
            return safe_api_call(bot.send_audio, chat_id, fid, caption=caption, caption_entities=entities, protect_content=protect)
        if ct == "sticker":
            return safe_api_call(bot.send_sticker, chat_id, fid, protect_content=protect)
    except Exception as e:
        print(f"⚠️ [deliver_item] Envoi AVEC entités échoué (chat_id={chat_id}, type={ct}) : {e!r}")
        try:
            if ct == "document":
                return safe_api_call(bot.send_document, chat_id, fid, caption=caption, protect_content=protect)
            if ct == "photo":
                return safe_api_call(bot.send_photo, chat_id, fid, caption=caption, protect_content=protect)
            if ct == "video":
                return safe_api_call(bot.send_video, chat_id, fid, caption=caption, protect_content=protect)
            if ct == "audio":
                return safe_api_call(bot.send_audio, chat_id, fid, caption=caption, protect_content=protect)
        except Exception as e2:
            print(f"⚠️ [deliver_item] Envoi SANS entités aussi échoué (chat_id={chat_id}, type={ct}) : {e2!r}")
            return None
    return None


def delete_many(chat_id, message_ids):
    for mid in message_ids:
        try:
            bot.delete_message(chat_id, mid)
        except Exception:
            pass


# ==========================================
# COMPTE À REBOURS EN DIRECT (même message du début à la fin)
# ==========================================
def format_duration(total_minutes):
    h, m = divmod(int(total_minutes), 60)
    parts = []
    if h:
        parts.append(f"{h} h")
    if m or not parts:
        parts.append(f"{m} min")
    return " ".join(parts)


def finalize_deleted_message(chat_id, msg_id, code, user, had_photo):
    resend_cfg = get_setting("resend_msg")
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🔄 Renvoyer les fichiers", callback_data=f"resend:{code}"))

    if resend_cfg and (resend_cfg.get("text") or resend_cfg.get("photo")):
        text2, ents2 = render_entities(resend_cfg.get("text") or "", resend_cfg.get("entities"), build_ctx(user))
        new_photo = resend_cfg.get("photo")
    else:
        text2, ents2 = "🗑️ <b>Fichiers supprimés !</b>\n\n📤 Tu peux les redemander ci-dessous.", None
        new_photo = None

    try:
        if new_photo and new_photo != had_photo:
            media = types.InputMediaPhoto(new_photo, caption=text2, caption_entities=ents2)
            bot.edit_message_media(media, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
        elif had_photo:
            bot.edit_message_caption(caption=text2, chat_id=chat_id, message_id=msg_id,
                                      caption_entities=ents2, reply_markup=kb)
        else:
            bot.edit_message_text(text2, chat_id, msg_id, entities=ents2, reply_markup=kb)
    except Exception:
        try:
            bot.send_message(chat_id, text2, entities=ents2, reply_markup=kb)
        except Exception:
            pass


# Registre partagé de tous les comptes à rebours actifs, traités par UN SEUL thread
# planificateur (countdown_scheduler_loop, démarré une fois au lancement du bot) plutôt
# que par un thread système par utilisateur — indispensable pour tenir à grande échelle :
# des dizaines de milliers de threads Python simultanés épuiseraient vite la mémoire/CPU
# du serveur, alors qu'une seule boucle peut gérer un nombre bien plus grand d'entrées.
active_countdowns = {}
countdown_lock = threading.Lock()
_countdown_seq = 0


def schedule_countdown(chat_id, sent_ids, code, user, total_minutes):
    """Envoie LE texte personnalisé (menu Contenu ➜ Message de suppression) tel quel, puis
    l'enregistre dans le planificateur partagé pour qu'il soit ré-affiché en boucle avec
    {time} qui décompte en direct (minute par minute) — sans rien ajouter derrière.
    Place {time} n'importe où dans ton texte pour afficher le chronomètre à cet endroit."""
    global _countdown_seq
    del_cfg = get_setting("delete_msg") or empty_content()
    raw_text = del_cfg.get("text") or ""
    entities_dicts = del_cfg.get("entities")
    photo = del_cfg.get("photo")
    total_minutes = max(1, int(total_minutes))

    def render(remaining_minutes):
        ctx = {
            "mention": build_mention(user),
            "date": time.strftime("%d/%m/%Y"),
            "bot_name": bot.get_me().username,
            "count": str(len(sent_ids)),
            "time": format_countdown(remaining_minutes * 60),
        }
        return render_entities(raw_text, entities_dicts, ctx)

    text, entities = render(total_minutes)
    try:
        if photo:
            msg = safe_api_call(bot.send_photo, chat_id, photo, caption=text or None, caption_entities=entities)
        else:
            msg = safe_api_call(bot.send_message, chat_id, text or " ", entities=entities)
    except Exception as e:
        print(f"⚠️ [schedule_countdown] Envoi initial échoué : {e!r}")
        msg = None

    if not msg:
        # Le message n'a pas pu être envoyé du tout : on programme quand même la suppression.
        threading.Timer(total_minutes * 60, delete_many, args=(chat_id, sent_ids)).start()
        return

    with countdown_lock:
        _countdown_seq += 1
        active_countdowns[_countdown_seq] = {
            "chat_id": chat_id, "msg_id": msg.message_id, "sent_ids": sent_ids, "code": code,
            "user": user, "photo": photo, "raw_text": raw_text, "entities_dicts": entities_dicts,
            "remaining_minutes": total_minutes, "render": render,
        }


def countdown_scheduler_loop():
    """UN SEUL thread, démarré au lancement du bot, qui fait avancer TOUS les comptes à
    rebours actifs, une passe par minute (voir schedule_countdown ci-dessus)."""
    while True:
        cycle_start = time.monotonic()
        with countdown_lock:
            keys = list(active_countdowns.keys())
        for key in keys:
            with countdown_lock:
                entry = active_countdowns.get(key)
            if not entry:
                continue
            entry["remaining_minutes"] -= 1
            if entry["remaining_minutes"] <= 0:
                delete_many(entry["chat_id"], entry["sent_ids"])
                finalize_deleted_message(entry["chat_id"], entry["msg_id"], entry["code"], entry["user"], entry["photo"])
                with countdown_lock:
                    active_countdowns.pop(key, None)
                continue
            new_text, new_entities = entry["render"](entry["remaining_minutes"])
            try:
                if entry["photo"]:
                    safe_api_call(bot.edit_message_caption, caption=new_text, chat_id=entry["chat_id"],
                                  message_id=entry["msg_id"], caption_entities=new_entities)
                else:
                    safe_api_call(bot.edit_message_text, new_text, entry["chat_id"], entry["msg_id"], entities=new_entities)
            except Exception as e:
                print(f"⚠️ [countdown_scheduler_loop] Édition du compteur échouée (clé {key}) : {e!r}")
        # Si une passe complète a pris plus d'une minute (gros volume simultané), on repart
        # immédiatement sur la suivante au lieu d'accumuler du retard.
        elapsed = time.monotonic() - cycle_start
        if elapsed < 60:
            time.sleep(60 - elapsed)


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

        if get_setting("delete_enabled", False) and sent_ids:
            del_cfg = get_setting("delete_msg")
            delay_min = del_cfg.get("delay_minutes") if del_cfg else None
            if delay_min:
                run_countdown_and_delete(chat_id, sent_ids, arg, user, delay_min)
            elif del_cfg and (del_cfg.get("text") or del_cfg.get("photo")):
                ctx = build_ctx(user, count=len(sent_ids))
                send_rich_message(chat_id, del_cfg, ctx=ctx)
        return

    kb = types.InlineKeyboardMarkup()
    row = []
    main_link = get_setting("main_link")
    if main_link:
        row.append(types.InlineKeyboardButton("➤ Voir plus", url=main_link))
    row.append(types.InlineKeyboardButton("☰ Commandes", callback_data="menu_commands"))
    kb.row(*row)

    welcome = get_setting("welcome")
    if welcome and (welcome.get("text") or welcome.get("photo")):
        send_rich_message(chat_id, welcome, ctx=build_ctx(user), reply_markup=kb)
    else:
        bot.send_message(
            chat_id,
            "🎉 <b>Bienvenue !</b>\n\nEnvoie /link pour obtenir un lien de partage sur un fichier.",
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
            "🔗 /link — créer un lien pour un seul fichier\n"
            "📦 /batch — regrouper plusieurs fichiers dans un seul lien\n"
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
        types.InlineKeyboardButton("⬅️ Retour", callback_data="nav_start"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
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
        types.InlineKeyboardButton("⬅️ Retour", callback_data="menu_commands"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
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
        types.InlineKeyboardButton("⬅️ Retour", callback_data="menu_admin"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
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
        types.InlineKeyboardButton("⬅️ Retour", callback_data="menu_status"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
    )
    return "\n".join(lines), kb


def build_content_menu():
    kb = types.InlineKeyboardMarkup()
    for key in ("welcome", "delete_msg", "resend_msg"):
        kb.row(types.InlineKeyboardButton(f"{CONTENT_NUMBERING[key]} {CONTENT_LABELS[key]}", callback_data="noop"))
        kb.row(
            types.InlineKeyboardButton("🖼️ Image", callback_data=f"cimg:{key}"),
            types.InlineKeyboardButton("✏️ Texte", callback_data=f"ctxt:{key}"),
            types.InlineKeyboardButton("👁️ Aperçu", callback_data=f"cprev:{key}"),
        )
    kb.row(
        types.InlineKeyboardButton("⬅️ Retour", callback_data="menu_admin"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
    )
    text = "🗂️ <b>Contenu</b>\n\nChoisis un élément à modifier :"
    return text, kb


def build_broadcast_menu():
    text = "📢 <b>Diffusion</b>\n\nEnvoie un message à tous les utilisateurs ayant démarré le bot."
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("📝 Nouvelle diffusion", callback_data="new_broadcast"))
    kb.row(
        types.InlineKeyboardButton("⬅️ Retour", callback_data="menu_admin"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
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
        types.InlineKeyboardButton("⬅️ Retour", callback_data="menu_admin"),
        types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"),
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
        "⏱️ <b>{time}</b> — compteur EN DIRECT avant suppression, format <code>00h00m</code> "
        "<i>(message de suppression, se met à jour tout seul dans le même message)</i>\n"
        "🔢 <b>{count}</b> — nombre de fichiers envoyés <i>(message de suppression)</i>\n\n"
        "<blockquote>⚠️ Enregistre vite tes {count} fichier(s), suppression dans {time} ⏳.</blockquote>"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


# ==========================================
# Commandes texte (raccourcis)
# ==========================================
def cmd_link(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "await_single_file"}
    bot.reply_to(message, "📎 Envoie ou transfère le fichier pour lequel générer un lien.")


def cmd_setwelcome(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "set_text", "key": "welcome"}
    bot.reply_to(message, "✏️ Envoie le nouveau texte d'accueil (mise en forme conservée). Astuce : /placeholders")


def cmd_setdelete(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "set_text", "key": "delete_msg"}
    bot.reply_to(
        message,
        "✏️ Envoie le texte du message de suppression. Astuce : /placeholders\n\n"
        "⏳ Ajoute <b>{time}</b> où tu veux voir le compteur (format <code>00h00m</code>), "
        "il décomptera en direct dans ce même message.",
        parse_mode="HTML",
    )


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
    content = get_setting("welcome")
    if not content or not (content.get("text") or content.get("photo")):
        bot.reply_to(message, "ℹ️ Aucun message d'accueil configuré pour l'instant.")
        return
    send_rich_message(message.chat.id, content, ctx=build_ctx(message.from_user))


def cmd_previewdelete(message):
    if not is_admin(message.from_user.id):
        return
    content = get_setting("delete_msg")
    if not content or not (content.get("text") or content.get("photo")):
        bot.reply_to(message, "ℹ️ Aucun message de suppression configuré pour l'instant.")
        return
    ctx = build_ctx(message.from_user, delay=content.get("delay_minutes", 0), count=1)
    send_rich_message(message.chat.id, content, ctx=ctx)


def cmd_broadcast(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "broadcast_content"}
    bot.reply_to(message, "📝 Envoie le texte/image à diffuser à tous les utilisateurs (ou transfère un post de canal avec ses boutons).")


def cmd_batch(message):
    if not is_admin(message.from_user.id):
        return
    pending[message.from_user.id] = {"action": "batch_collect", "data": [], "source_channel_id": None}
    bot.reply_to(
        message,
        "📦 Transfère-moi les fichiers un par un, <b>tous depuis le même canal</b> (peu importe lequel). "
        "Tape /done quand tu as fini, ou /cancel pour annuler.",
        parse_mode="HTML",
    )


def cmd_done(message):
    uid = message.from_user.id
    state = pending.get(uid)
    if not state or state.get("action") != "batch_collect":
        bot.reply_to(message, "ℹ️ Aucun lot en cours. Utilise /batch pour en commencer un.")
        return
    items = state.get("data") or []
    if not items:
        bot.reply_to(message, "❌ Aucun fichier reçu pour l'instant. Envoie-en au moins un avant /done, ou /cancel.")
        return
    pending.pop(uid)
    code = save_link({"type": "files", "items": items})
    bot.reply_to(message, f"✅ <b>Lien permanent créé</b> pour {len(items)} fichier(s) :\n{make_link(code)}", parse_mode="HTML")


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
def extract_retry_after(exc):
    """Essaie de lire le délai d'attente imposé par Telegram (erreur 429) quelle que soit
    la version de la librairie utilisée."""
    resp = getattr(exc, "result_json", None)
    if isinstance(resp, dict):
        params = resp.get("parameters") or {}
        ra = params.get("retry_after")
        if ra:
            return int(ra)
    m = re.search(r"retry after (\d+)", str(exc), re.IGNORECASE)
    return int(m.group(1)) if m else None


def send_broadcast_item(target, content, kb):
    """Envoie à UN utilisateur, en réessayant automatiquement si Telegram demande de patienter
    (erreur 429 / flood control) au lieu de compter direct un échec."""
    for _ in range(3):
        try:
            return bool(send_rich_message(target, content, reply_markup=kb))
        except Exception as e:
            retry_after = extract_retry_after(e)
            if retry_after:
                time.sleep(retry_after + 1)
                continue
            print(f"⚠️ [broadcast] Échec envoi vers {target} : {e!r}")
            return False
    return False


def run_broadcast(admin_chat_id, panel_chat_id, panel_msg_id, content, kb, warning):
    cur.execute("SELECT user_id FROM users")
    user_ids = [row[0] for row in cur.fetchall()]
    sent, failed = 0, 0
    for target in user_ids:
        if send_broadcast_item(target, content, kb):
            sent += 1
        else:
            failed += 1
        # Cadence volontairement sous la limite globale Telegram (~30 messages/seconde),
        # pour ne pas déclencher de flood control sur un gros volume d'utilisateurs.
        time.sleep(0.05)
    msg = f"📬 <b>Diffusion terminée !</b> 🎉\n✅ Envoyés : {sent} — ❌ Échecs : {failed}{warning}"
    kb_back = types.InlineKeyboardMarkup()
    kb_back.row(types.InlineKeyboardButton("⬅️ Retour", callback_data="menu_broadcast"))
    if panel_chat_id and panel_msg_id:
        edit_or_send(panel_chat_id, panel_msg_id, msg, reply_markup=kb_back, parse_mode="HTML")
    else:
        bot.send_message(admin_chat_id, msg, parse_mode="HTML", reply_markup=kb_back)


def perform_broadcast(message, state, content, kb, warning=""):
    """Lance la diffusion EN ARRIÈRE-PLAN (thread dédié) : le bot reste réactif pour tout
    le monde pendant qu'elle tourne, même si elle dure longtemps sur une grosse base."""
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    panel_chat_id = state.get("panel_chat_id")
    panel_msg_id = state.get("panel_msg_id")
    panel_reply(message, state, f"🚀 <b>Diffusion lancée</b> pour {total} utilisateur(s)... ⏳\nJe te préviens ici une fois terminé.")
    threading.Thread(
        target=run_broadcast,
        args=(message.chat.id, panel_chat_id, panel_msg_id, content, kb, warning),
        daemon=True,
    ).start()


# ==========================================
# GESTION DES FLUX EN COURS (pending)
# ==========================================
def handle_pending(message):
    uid = message.from_user.id
    state = pending.get(uid)
    if not state:
        return False

    action = state["action"]

    # ---------- lien pour un seul fichier (/link) ----------
    if action == "await_single_file":
        item = extract_file_info(message)
        if not item["file_id"]:
            bot.reply_to(message, "❌ Ce n'est pas un fichier pris en charge. Réessaie, ou /cancel.")
            return True
        pending.pop(uid)
        code = save_link({"type": "files", "items": [item]})
        bot.reply_to(message, f"✅ <b>Lien permanent créé :</b>\n{make_link(code)}", parse_mode="HTML")
        return True

    # ---------- image d'un des 3 messages personnalisables ----------
    if action == "set_image":
        key = state["key"]
        content = get_setting(key) or empty_content()
        if message.content_type == "photo":
            content["photo"] = message.photo[-1].file_id
        elif (message.text or "").strip().lower() == "/remove":
            content["photo"] = None
        else:
            panel_reply(message, state, "❌ Envoie une image, ou tape /remove pour retirer l'image actuelle.")
            return True
        set_setting(key, content)
        pending.pop(uid)
        panel_reply(message, state, f"✅ Image mise à jour — {CONTENT_LABELS[key]}.", reply_markup=back_to_content_kb(key))
        return True

    # ---------- texte d'un des 3 messages personnalisables ----------
    if action == "set_text":
        key = state["key"]
        content = get_setting(key) or empty_content()
        captured = capture_rich_message(message)
        content["text"] = captured["text"]
        content["entities"] = captured["entities"]
        set_setting(key, content)
        unknown = find_unknown_placeholders(content["text"])

        if key == "delete_msg":
            state["action"] = "set_delete_hours"
            panel_reply(message, state, "⏱️ Dans combien d'HEURES les fichiers doivent-ils être supprimés ? (0 si aucune)")
            return True

        pending.pop(uid)
        msg = f"✅ Texte mis à jour — {CONTENT_LABELS[key]}."
        if unknown:
            msg += f"\n⚠️ <i>Variable(s) inconnue(s) ignorée(s) :</i> {', '.join('{' + u + '}' for u in unknown)}"
        panel_reply(message, state, msg, reply_markup=back_to_content_kb(key))
        return True

    if action == "set_delete_hours":
        try:
            hours = int((message.text or "").strip())
        except ValueError:
            panel_reply(message, state, "❌ Envoie un nombre entier d'heures (ex : 1). 🔢")
            return True
        state["hours"] = hours
        state["action"] = "set_delete_minutes"
        panel_reply(message, state, "⏱️ Et combien de MINUTES en plus ? (0 à 59)")
        return True

    if action == "set_delete_minutes":
        try:
            minutes = int((message.text or "").strip())
        except ValueError:
            panel_reply(message, state, "❌ Envoie un nombre entier de minutes (ex : 30). 🔢")
            return True
        total = state.get("hours", 0) * 60 + minutes
        content = get_setting("delete_msg") or empty_content()
        content["delay_minutes"] = total
        set_setting("delete_msg", content)
        pending.pop(uid)
        msg = f"✅ Délai enregistré : {format_duration(total)}."
        if not get_setting("delete_enabled", False):
            msg += "\nℹ️ <i>Rappel : la suppression automatique est actuellement désactivée.</i> 🔴"
        panel_reply(message, state, msg, reply_markup=back_to_content_kb("delete_msg"))
        return True

    # ---------- force-sub ----------
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

    # ---------- broadcast ----------
    if action == "broadcast_content":
        content = capture_rich_message(message)
        detected_kb = extract_url_keyboard(message)
        if detected_kb:
            pending.pop(uid)
            perform_broadcast(message, state, content, detected_kb, warning="\nℹ️ <i>Boutons détectés et conservés.</i>")
            return True
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
        pending.pop(uid)
        perform_broadcast(message, state, content, kb, warning=warning)
        return True

    # ---------- batch (un canal au choix, mais le même pour tout le lot) ----------
    if action == "batch_collect":
        src_chat_id, _ = get_forward_channel_msgid(message)
        if not src_chat_id:
            bot.reply_to(message, "❌ Transfère le fichier depuis un canal (peu importe lequel, mais toujours le même pour un lot). Réessaie, ou /cancel.")
            return True
        if state["data"] and src_chat_id != state["source_channel_id"]:
            bot.reply_to(
                message,
                "❌ Ce fichier vient d'un canal différent des précédents. Tous les fichiers d'un même lot "
                "doivent venir du même canal.\nTape /done pour clôturer le lot actuel avec ce que tu as déjà "
                "envoyé, ou continue avec des fichiers du même canal que le premier."
            )
            return True
        item = extract_file_info(message)
        if not item["file_id"]:
            bot.reply_to(message, "❌ Type de fichier non pris en charge. Envoie autre chose, ou /done pour terminer, ou /cancel.")
            return True
        state["source_channel_id"] = src_chat_id
        state["data"].append(item)
        bot.reply_to(message, f"✅ Fichier {len(state['data'])} ajouté au lot. Envoie-en d'autres du même canal, ou tape /done pour créer le lien. 📦")
        return True

    # ---------- import ----------
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
# CALLBACKS (boutons)
# ==========================================
@bot.callback_query_handler(func=lambda c: True)
def callback_router(call):
    data = call.data
    user = call.from_user
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "noop":
        bot.answer_callback_query(call.id)
        return

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

    if data.startswith("cimg:"):
        key = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "set_image", "key": key, "panel_chat_id": chat_id, "panel_msg_id": msg_id}
        edit_or_send(chat_id, msg_id, f"🖼️ Envoie l'image pour « {CONTENT_LABELS[key]} » (ou /remove pour la retirer).")
        return

    if data.startswith("ctxt:"):
        key = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "set_text", "key": key, "panel_chat_id": chat_id, "panel_msg_id": msg_id}
        hint = f"✏️ Envoie le texte pour « {CONTENT_LABELS[key]} » (mise en forme conservée). Astuce : /placeholders"
        if key == "delete_msg":
            hint += (
                "\n\n⏳ Ajoute <b>{time}</b> à l'endroit où tu veux voir le compteur — il décomptera "
                "en direct dans CE message, au format <code>00h00m</code>. Ex :\n"
                "<i>⚠️ Enregistre vite tes {count} fichier(s), suppression dans {time} !</i>"
            )
        edit_or_send(chat_id, msg_id, hint)
        return

    if data.startswith("cprev:"):
        key = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        content = get_setting(key)
        if not content or not (content.get("text") or content.get("photo")):
            bot.send_message(chat_id, f"ℹ️ <i>Aucun contenu configuré pour « {CONTENT_LABELS[key]} ».</i>", parse_mode="HTML")
            return
        ctx = build_ctx(user, delay=content.get("delay_minutes", 0), count=1) if key == "delete_msg" else build_ctx(user)
        kb_close = types.InlineKeyboardMarkup()
        kb_close.row(types.InlineKeyboardButton("❌ Fermer", callback_data="close_menu"))
        send_rich_message(chat_id, content, ctx=ctx, reply_markup=kb_close)
        return

    if data == "new_broadcast":
        bot.answer_callback_query(call.id)
        pending[user.id] = {"action": "broadcast_content", "panel_chat_id": chat_id, "panel_msg_id": msg_id}
        edit_or_send(chat_id, msg_id, "📝 Envoie le texte/image à diffuser (ou transfère un post de canal avec ses boutons). 📢")
        return


# ==========================================
# ROUTAGE PRINCIPAL
# ==========================================
COMMANDS = {
    "start": lambda m, a: cmd_start(m.from_user, m.chat.id, a),
    "help": lambda m, a: send_commands_menu(m.chat.id, m.from_user),
    "placeholders": lambda m, a: cmd_placeholders(m),
    "link": lambda m, a: cmd_link(m),
    "setwelcome": lambda m, a: cmd_setwelcome(m),
    "setdelete": lambda m, a: cmd_setdelete(m),
    "setlink": lambda m, a: cmd_setlink(m, a),
    "previewwelcome": lambda m, a: cmd_previewwelcome(m),
    "previewdelete": lambda m, a: cmd_previewdelete(m),
    "broadcast": lambda m, a: cmd_broadcast(m),
    "batch": lambda m, a: cmd_batch(m),
    "done": lambda m, a: cmd_done(m),
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

    # Aucune réaction automatique aux fichiers hors flux en cours :
    # tout passe désormais par /link, /batch, ou les flux Image/Texte du menu Contenu.
    handle_pending(message)


# ==========================================
# LANCEMENT
# ==========================================
if __name__ == "__main__":
    print("Bot démarré, en attente de messages...")
    bot.infinity_polling()
