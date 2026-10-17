# Instructions pour Codex

Ce dépôt contient Trad, un simulateur hors ligne et un tableau de bord consultatif.

Pour une installation locale sur macOS Apple Silicon (Mac M5, 24 Go), suivre
`MACOS.md` et utiliser `bash scripts/start_macos.sh`. Effectuer l’installation,
les tests et les vérifications HTTP jusqu’au lancement effectif dans une session
locale. Inspecter les outils disponibles avant toute installation. Ne pas
utiliser les scripts Linux ni les chemins `/workspace` sur le Mac.

Utiliser le checkout existant ; aucun worktree supplémentaire n’est nécessaire.
Préserver les modifications existantes. Ne jamais committer de secrets, poids de
modèle, logs ou bases de données. Les credentials cloud ne sont pas dans ce dépôt.
Ne pas demander de secrets dans le chat. Si les credentials marché manquent,
terminer les vérifications indépendantes et indiquer comment les fournir localement.

Validation : `python3 -m unittest discover -s tests -v`.
La simulation fonctionne sans réseau ; les données réelles exigent Alpaca.
L’IA est consultative : conserver les validations et l’interdiction des ordres
courtiers. Distinguer une abstention valide d’un échec fournisseur et ne pas
annoncer d’inférence réelle sans l’avoir observée.
