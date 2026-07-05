# Mon File Store Bot

Bot Telegram simple qui stocke des fichiers dans un canal privé et génère
des liens de partage. Supporte un fichier unique ou un lot de plusieurs
fichiers sous un seul lien (mode batch).

## Variables à configurer dans Railway (onglet "Variables")

| Nom | Exemple | Où le trouver |
|---|---|---|
| `BOT_TOKEN` | `123456:ABC-DEF...` | Donné par @BotFather après `/newbot` |
| `DB_CHANNEL` | `-1001234567890` | ID de ton canal privé (voir ci-dessous) |
| `ADMIN_IDS` | `987654321` | Ton ID Telegram (voir ci-dessous), plusieurs ids possibles séparés par des virgules |

### Trouver l'ID du canal (DB_CHANNEL)
1. Crée un canal Telegram, mets-le en **privé**
2. Ajoute ton bot comme **administrateur** du canal
3. Poste un message dans le canal
4. Transfère ce message à **@userinfobot**
5. Il te donne l'ID du canal (nombre négatif, commence par `-100`)

### Trouver ton ID Telegram (ADMIN_IDS)
1. Parle à **@userinfobot**
2. Il te répond directement avec ton ID (nombre positif)

## Utilisation une fois le bot en ligne

- **Un seul fichier** : envoie-le simplement au bot en message privé → il répond avec un lien
- **Plusieurs fichiers sous un seul lien** :
  1. Envoie `/batch`
  2. Envoie tous les fichiers un par un
  3. Envoie `/done` → le bot donne un seul lien pour tout le lot
  4. `/cancel` annule un batch en cours
- Toute personne qui clique sur le lien reçoit automatiquement le(s) fichier(s)

## Notes importantes

- Seuls les IDs listés dans `ADMIN_IDS` peuvent envoyer des fichiers au bot
- Le canal `DB_CHANNEL` doit rester **privé**
- La base de données (`store.db`) est un simple fichier SQLite stocké sur le
  serveur. Sur Railway, si tu redéploies sans volume persistant, cette base
  peut être réinitialisée. Pour un usage sérieux, pense à ajouter un volume
  Railway sur le dossier du projet.
