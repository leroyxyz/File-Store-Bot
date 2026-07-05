import os
import sqlite3
import uuid
import telebot

# ==========================================
# CONFIGURATION (à remplir dans Railway > Variables)
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

bot = telebot.TeleBot(BOT_TOKEN)

# ==========================================
# BASE DE DONNÉES (fichier local store.db)
# ==========================================
conn = sqlite3.connect("store.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS links (
    code TEXT PRIMARY KEY,
    message_ids TEXT NOT NULL
)
""")
conn.commit()

# Sessions de batch en mémoire : {admin_id: [message_ids]}
batch_sessions = {}


def is_admin(user_id):
    return user_id in ADMIN_IDS


def save_link(message_ids):
    code = uuid.uuid4().hex[:8]
    cur.execute(
        "INSERT INTO links (code, message_ids) VALUES (?, ?)",
        (code, ",".join(str(m) for m in message_ids)),
    )
    conn.commit()
    return code


def get_message_ids(code):
    cur.execute("SELECT message_ids FROM links WHERE code = ?", (code,))
    row = cur.fetchone()
    if not row:
        return None
    return [int(x) for x in row[0].split(",")]


def make_link(code):
    bot_username = bot.get_me().username
    return f"https://t.me/{bot_username}?start={code}"


# ==========================================
# COMMANDES
# ==========================================

@bot.message_handler(commands=["start"])
def handle_start(message):
    parts = message.text.split(maxsplit=1)

    # Cas 1 : quelqu'un clique sur un lien de partage (/start CODE)
    if len(parts) == 2:
        code = parts[1].strip()
        message_ids = get_message_ids(code)
        if not message_ids:
            bot.reply_to(message, "❌ Ce lien est invalide ou a expiré.")
            return
        bot.send_chat_action(message.chat.id, "typing")
        for msg_id in message_ids:
            try:
                bot.copy_message(message.chat.id, DB_CHANNEL, msg_id)
            except Exception as e:
                bot.send_message(message.chat.id, f"⚠️ Erreur sur un fichier : {e}")
        return

    # Cas 2 : simple /start
    bot.reply_to(
        message,
        "👋 Bienvenue !\n\n"
        "Envoie-moi un fichier, je te donnerai un lien de partage.\n"
        "Pour regrouper plusieurs fichiers sous un seul lien : /batch"
    )


@bot.message_handler(commands=["batch"])
def handle_batch_start(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Tu n'es pas autorisé à faire ça.")
        return
    batch_sessions[message.from_user.id] = []
    bot.reply_to(
        message,
        "📦 Mode batch activé.\n"
        "Envoie maintenant tous les fichiers à regrouper.\n"
        "Quand tu as fini, envoie /done."
    )


@bot.message_handler(commands=["done"])
def handle_batch_done(message):
    if not is_admin(message.from_user.id):
        return
    session = batch_sessions.pop(message.from_user.id, None)
    if not session:
        bot.reply_to(message, "❌ Aucun batch en cours ou aucun fichier envoyé. Utilise /batch pour recommencer.")
        return
    code = save_link(session)
    bot.reply_to(message, f"✅ Lien créé pour {len(session)} fichier(s) :\n{make_link(code)}")


@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    if message.from_user.id in batch_sessions:
        batch_sessions.pop(message.from_user.id)
        bot.reply_to(message, "🚫 Batch annulé.")


# ==========================================
# RÉCEPTION DE FICHIERS
# ==========================================

@bot.message_handler(content_types=["document", "photo", "video", "audio"])
def handle_file(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Tu n'es pas autorisé à uploader de fichiers.")
        return

    # On copie le fichier dans le canal privé (base de données)
    copied = bot.copy_message(DB_CHANNEL, message.chat.id, message.message_id)

    # Si un batch est en cours, on ajoute juste le fichier, pas de lien tout de suite
    if message.from_user.id in batch_sessions:
        batch_sessions[message.from_user.id].append(copied.message_id)
        count = len(batch_sessions[message.from_user.id])
        bot.reply_to(message, f"➕ Fichier ajouté au batch ({count} au total). Envoie /done quand tu as fini.")
        return

    # Sinon, un fichier = un lien immédiat
    code = save_link([copied.message_id])
    bot.reply_to(message, f"✅ Lien créé :\n{make_link(code)}")


# ==========================================
# LANCEMENT
# ==========================================
if __name__ == "__main__":
    print("Bot démarré, en attente de messages...")
    bot.infinity_polling()
