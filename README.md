# File Store Bot Pro

Bot Telegram de stockage et partage de fichiers, entièrement personnalisable
depuis Telegram.

## Commandes

### Utilisateurs
- `/start` — démarre le bot ou récupère un fichier via un lien
- `/help` — liste des commandes

### Admin — Contenu
- Envoyer un fichier (document, photo, vidéo, audio, sticker) → lien immédiat
- `/batch` → transfère le **premier** puis le **dernier** fichier de la plage
  depuis le canal privé. Le bot génère un lien couvrant tout l'intervalle.
- `/cancel` — annule un flux en cours (batch, réglage, broadcast...)

### Admin — Personnalisation (sans GitHub)
- `/setwelcome` — envoie le nouveau message `/start` (texte, image, gras,
  italique, citation, liens, etc. — tout ce que Telegram permet de mettre
  en forme est conservé)
- `/setdelete` — envoie le message d'avertissement affiché après chaque
  fichier, puis indique le délai en minutes avant suppression automatique
- `/setforcesub` — transfère un message du canal à rendre obligatoire, puis
  colle son lien d'invitation public
- `/forcesub on` / `/forcesub off` — active ou coupe l'abonnement obligatoire
- `/protect on` / `/protect off` — active ou coupe la protection anti-transfert
  (bloque le transfert/l'enregistrement direct ; ne bloque pas les captures
  d'écran, Telegram ne le permet pas)

### Admin — Suivi
- `/stats` — utilisateurs uniques, liens créés, fichiers référencés, état
  des options
- `/broadcast` — envoie un message (texte/image/style) à tous les
  utilisateurs ayant déjà démarré le bot

