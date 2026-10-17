# Installer Trad sur macOS (Apple Silicon, M5, 24 Go)

Instructions destinées à l’utilisateur et à Codex exécuté localement sur le Mac.
Le code Python utilise uniquement la bibliothèque standard ; Python 3.11+ et
Ollama sont nécessaires. Le modèle par défaut est Qwen 2.5 1.5B, adapté à cette
mémoire. Les trois rôles consultatifs utilisent ce même modèle. Aucun ordre
courtier n’est envoyé.

## Installation par Codex local

1. Utiliser ce dépôt dans une session Codex locale sur le Mac. Vérifier
   `sw_vers`, `uname -m`, `python3 --version`, `command -v ollama` et
   `command -v brew`. Ne pas utiliser les chemins `/workspace` sur macOS.
2. Si Python 3.11+ ou Ollama manque et Homebrew est déjà installé, exécuter
   `brew install python ollama` (uniquement les composants manquants).
   Sinon installer Python depuis https://www.python.org/downloads/macos/ et
   Ollama depuis https://ollama.com/download/mac. Une installation système
   peut nécessiter une action locale de l’utilisateur. Ne pas exécuter le
   script Linux `scripts/install_local.sh` sur macOS.
3. Depuis le dépôt, exécuter `bash scripts/start_macos.sh`. Ce script vérifie
   les prérequis, lance Ollama si nécessaire, refuse un modèle absent,
   vérifie sa disponibilité, exécute les tests puis lance le tableau de bord.
   Télécharger explicitement le modèle avec `ollama pull qwen2.5:1.5b`
   après avoir lancé Ollama, puis relancer le script. Aucun téléchargement
   automatique. Le téléchargement initial est d’environ 1 Go.
4. Vérifier `http://127.0.0.1:8080/api/health` : `ai_ready` doit être vrai.
   Ouvrir `http://127.0.0.1:8080` dans le navigateur. Pour Codex, garder le
   processus dans une session persistante. Ctrl+C ferme les processus que le
   lanceur a créés ; un Ollama existant reste actif. Les poids sont conservés
   à l’emplacement standard d’Ollama sur le Mac.

```bash
git clone https://github.com/Mohazem/Trad.git
cd Trad
bash scripts/start_macos.sh
```

Port différent : `bash scripts/start_macos.sh --port 8081`.
Les logs locaux sont dans `outputs/ollama-macos.log` (ignoré par Git).

## Accès aux marchés

Les credentials du cloud ne sont pas transférés dans Git. Pour Alpaca, fournir
localement `ALPACA_PAPER_KEY` et `ALPACA_PAPER_SECRET_` au processus, via les
paramètres sécurisés du lanceur ou une saisie masquée dans un terminal bash :

```bash
bash
read -r -s -p 'Clé Alpaca : ' ALPACA_PAPER_KEY; echo
read -r -s -p 'Secret Alpaca : ' ALPACA_PAPER_SECRET_; echo
export ALPACA_PAPER_KEY ALPACA_PAPER_SECRET_
bash scripts/start_macos.sh
```

Ne jamais coller de credentials dans le chat, dans un fichier versionné ou dans
une commande qui les inscrit dans l’historique. Le programme ne charge pas
les fichiers `.env` automatiquement. Aucun token Hugging Face n’est nécessaire.
Sans clés Alpaca, les tests et Ollama peuvent être vérifiés ; les cours réels
restent indisponibles. Les actions affichent « Marché fermé » quand l’horloge
Alpaca le confirme ; les prix anciens restent exclus de l’analyse. BTC/USD
peut être analysé hors séance si ses observations sont récentes.

## Vérifications et dépannage

- Tests : `python3 -m unittest discover -s tests -v`.
- Modèle : `ollama list` ; au besoin `ollama pull qwen2.5:1.5b`.
- Une fois les marchés configurés : cliquer sur « Analyser » et vérifier les
  traces des rôles. Une abstention peut être une réponse valide ; une erreur
  fournisseur ne valide pas une inférence complète.
- Le premier appel peut être lent. Ne pas supprimer les contrôles de fraîcheur,
  les schémas JSON ni les limites de risque pour faire passer une analyse.

La branche macOS du lanceur doit être validée sur le Mac : les tests exécutés
sur le serveur Linux ne démontrent pas une installation macOS réussie.

## Recherche multi-horizons optionnelle

Voir [docs/MULTI_HORIZON.md](docs/MULTI_HORIZON.md). Les dépendances ML sont
optionnelles et doivent être installées explicitement dans un environnement
Python 3.11–3.13 distinct. Sur Apple Silicon, LightGBM peut nécessiter
`brew install libomp`. Les tests natifs ont été exécutés sur Linux, pas sur ce Mac.
Aucune installation ML ou de modèle ne se déclenche à l’ouverture du dashboard.
