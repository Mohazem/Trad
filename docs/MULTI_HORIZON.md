# Développement multi-horizons : audit et livraison

## Phase 0 — baseline réelle

Checkout initial : `3a0689e`. Aucun changement local au début de cette demande.
`python3 -m unittest discover -s tests -v` : 97 tests, OK (sortie conservée dans
`/tmp/trad-phase0-tests.log` dans la session de développement).

Flux existants : `demo` → agents déterministes → Ledger SQLite ; `ai_cli`
mock/replay → AdvisoryPipeline → simulate_snapshot → même Ledger ; `connected`
→ lecture Alpaca → pipeline consultatif → dashboard, sans soumission.
`AlpacaPaperSkeleton.submit` lève toujours NetworkTradingDisabled.

Divergences avec la spécification : aucun `paper.py`, `PaperEngine`, `PaperBroker`,
`crypto_strategy.py` ou `indicators.py` dans ce checkout. Aucun verrou de compte
courtier, aucune réconciliation distante et aucune stratégie EMA/RSI crypto
préexistante. Les plafonds 50 USD/45 USD/une position ne sont pas ceux du
simulateur historique à capital fictif 100 000 USD. Ne pas modifier ses paramètres
pour lui faire représenter un compte Paper. Ces bornes sont réservées à la nouvelle
couche expérimentale. L'exécution distante reste interdite et l'intégration au
moteur Paper absent reste un blocage identifié, pas une fonctionnalité simulée.

Aucun historique réel multi-horizon vérifié, aucun modèle ML entraîné ni modèle
Qwen3 disponible démontré. Pas de téléchargement ou appel externe pendant cette
implémentation. Les tests ajoutés utilisent des fixtures explicitement synthétiques.

Inspection distante complémentaire : `feature/one-shot-paper` au commit
`ff1ccf3` possède `paper_once.py`, `paper_transport.py` et un PaperBroker pour
un essai manuel unique, plafonné à 1 000 USD. Ce n'est pas PaperEngine ni le
moteur récurrent 50/45 USD de la spécification. Cette branche n'est pas fusionnée,
et ses écritures ne sont pas activées par cette livraison. Il faut identifier
la version attendue avant toute intégration d'exécution. Le contexte du ZIP
mentionné n'est pas présent dans cette conversation.

## Phases 1 à 7 — fichiers et contrats

| Phase | Livraison | Vérification / limite |
| --- | --- | --- |
| 1 | `signals.py` : StrategySignal, RiskDecision, StrategyAllocation, SignalStore ; `market_history.py` : MarketObservation, cache SQLite ; `features.py` : FeatureSnapshot, agrégation | 102 tests à cette étape ; UTC, doublons, trous, OHLC, arrivée tardive, bougie incomplète, invariance par préfixe |
| 2 | `backtest.py` : BacktestResult, fenêtres walk-forward purgées, coûts et benchmarks | 106 tests ; fixtures synthétiques uniquement ; aucun historique réel téléchargé |
| 3 | `ml_models.py` : ModelManifest, ModelPredictor, adaptateurs ; `training.py` : labels nets, entraînement offline et seuil validé | 110 tests avant installation ; tests natifs LightGBM/XGBoost réussis après correction du format NumPy LightGBM |
| 4 | `model_routing.py`, extension compatible de AdvisoryPipeline et OllamaProvider, `llm_benchmark.py` | 115 tests à cette étape ; timeouts/routage/JSON testés ; benchmark Qwen non exécuté, Ollama absent |
| 5 | `risk_gate.py`, `strategy_router.py`, `portfolio_strategy.py`, configuration séparée | 120 tests ; caps 50/45 USD/une position, conflits, état incertain ; **liaison Paper bloquée par l'absence du moteur attendu** |
| 6 | `strategy_status.py`, routes web et page `/strategies` avec onglets, CLI `research.py` | Lecture seule, aucune activation de Paper ; états inconnus explicites ; métriques de régression distinctes des rendements |
| 7 | Tests contrats, recettes CLI, inférence native, corruption de modèle, rejet in-sample, coûts, arrêt/absence de compte | Bilan final ci-dessous ; aucune preuve de rentabilité |

Les modules proposés ont été créés car leurs responsabilités n'existaient pas
dans le checkout. Les contrats sont regroupés dans leur module métier plutôt
que dans un second modèle générique. `Config` historique n'a pas de nouveau champ :
`MultiHorizonConfig` est une classe séparée dans `config.py` pour conserver
l'empreinte des anciens registres SQLite et leur compatibilité après redémarrage.
`multi_horizon.example.json` est un nouveau fichier, aucun fichier de paramètres
historique n'est écrasé. Pas de Freqtrade/FreqAI.

`AdvisoryPipeline(provider)` reste valide ; le nouvel argument optionnel
`role_providers` exige exactement analyst/critic/synthesizer. `evaluate` accepte
`signals=()` : seuls des StrategySignal validés sont transmis, avec versions et
dates, dans les données utilisateur. Les schémas de réponse restent inchangés.
Les prédictions sont décrites comme incertaines, jamais comme des observations.
Le résultat ajoute `role_metrics` (durée, modèle, validité JSON et compteurs Ollama
numériques disponibles). Un timeout désactive toute l'instance, sans retry.

## Données et causalité

Univers de recherche : BTC/USD uniquement, seul crypto déjà connu du code.
Cela ne certifie pas sa négociabilité sur un compte Paper particulier. Aucun
univers d'altcoins, historique d'order book ou disponibilité fournisseur inventé.
L'import JSONL exige les OHLCV en chaînes décimales, UTC, source, timeframe,
`received_at_utc`, qualité et `data_kind` (`synthetic` ou `historical`).

L'historique est immuable : un conflit n'écrase pas une observation ; une
correction demande une nouvelle version de source. Les lectures n'utilisent que
les bougies closes et reçues à la date de décision. Un fichier téléchargé
aujourd'hui ne démontre pas la disponibilité historique des observations à leur
clôture : ne pas antidater la réception pour contourner ce contrôle. Il faut un
jeu point-in-time adéquat ou une modélisation explicite de la disponibilité,
non implémentée ici. Le cache ne télécharge rien. `import_jsonl(path, HistoryStore)`
importe une source locale explicitement choisie ; aucun client d'historiques
fournisseur automatique n'est ajouté.

Granularités : 1m/5m/15m/4h/1d/1w. Agrégations par seaux UTC entièrement remplis
(les semaines sont ancrées sur l'époque UTC, pas sur un calendrier boursier).
Les features v1 sont rendements 1/8 observations, volatilité et volume relatif
sur 8 observations. Pas de prétention à avoir implémenté les EMA/RSI absents.
La collecte connectée historique conserve son interface et exclut désormais la
bougie minute en cours des résumés fondés sur des clôtures.

## Recette hors ligne reproductible

Python 3.11+ ; depuis le dépôt. Utiliser de nouveaux chemins, les rapports ne
sont jamais écrasés automatiquement.

```sh
mkdir -p outputs/multi-horizon
python3 -m unittest discover -s tests -v
python3 -m trading.research fixture --bars 180 --output outputs/multi-horizon/minute.jsonl
python3 -m trading.research backtest --input outputs/multi-horizon/minute.jsonl --output outputs/multi-horizon/minute-backtest
python3 -m trading.research signal --input outputs/multi-horizon/minute.jsonl --strategy minute --as-of 2026-01-01T03:00:00+00:00 --output outputs/multi-horizon/minute.json --signal-store outputs/multi-horizon/signals.sqlite3
python3 -m trading.web --port 8080
```

La fixture est **synthétique**. `--as-of` est une horloge de recherche historique,
pas un moyen de rendre des cours actuels. Sur `/strategies`, un signal ancien
reste visible avec une alerte de péremption. Sans `--as-of`, les données anciennes
sont rejetées. En cas de données invalides, aucun signal daté artificiellement
n'est fabriqué : le rapport indique `data_or_model_validation_failed`.

Les stratégies sont désactivées par défaut. L'exemple produit donc une abstention.
Le Fast Path ne construit aucun fournisseur LLM et fonctionne sans Ollama.
Les rapports sont des artefacts de recherche ; le journal des signaux n'est
jamais une source d'autorité pour les positions ou la trésorerie courtier.

## Entraînement et comparaison ML optionnels

Installer **explicitement** les paquets dans un environnement séparé ; rien n'est
installé par le dashboard, le simulateur ou l'inférence. Les versions directes
sont épinglées dans `requirements-ml.txt` (sur Linux, distribution XGBoost CPU).
Versions natives réellement testées : Python 3.12, LightGBM 4.6.0, XGBoost CPU
3.0.5, NumPy 2.2.6, SciPy 1.15.3 sur Linux x86_64. Pour le ML sur Mac, utiliser
Python 3.11–3.13 et installer `libomp` via Homebrew si LightGBM le demande.
Le test sur Mac M5 reste à réaliser. Le coeur standard reste indépendant du ML.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-ml.txt
python -m unittest tests.test_ml_native -v
python -m trading.research train --input outputs/multi-horizon/minute.jsonl --family lightgbm --cost-bps 30 --output outputs/multi-horizon/minute-walk-forward
```

Pour XGBoost, utiliser une source 1d ou 4h (`fixture --timeframe 1d` pour un test
logiciel), `--family xgboost` et un `--label-span` exprimé en nombre de bougies.
Les modèles entraînés sur fixtures restent identifiés `synthetic` et refusent
une provenance historique différente. Les artefacts sont des formats natifs
JSON/texte du fournisseur, sans pickle. Le SHA256 protège contre une corruption
locale, pas contre un manifest et un modèle tous deux remplacés par un attaquant.
Les fichiers de modèles doivent rester sous un chemin local de confiance.

Chaque fenêtre entraîne seulement sur train, choisit le seuil sur validation,
conserve test hors de cette sélection, purge les labels avant chaque frontière
et ajoute l'embargo. Aucun scaler global. Seed, hyperparamètres, coût de la cible,
versions, features et digest des données sont enregistrés. Le rapport de modèle
contient la MSE du test : ce n'est pas une performance de portefeuille.
Ne pas choisir la fenêtre ou les hyperparamètres selon les résultats test.

Pour comparer un **modèle gelé choisi à l'avance**, par exemple la première
fenêtre, avec les mêmes bornes temporelles pour tous les benchmarks :

```sh
python -m trading.research backtest --input outputs/multi-horizon/minute.jsonl --model-dir outputs/multi-horizon/minute-walk-forward/window-000 --output outputs/multi-horizon/ml-comparison
```

Cette commande n'utilise que les dates postérieures aux labels de validation.
Les coûts totaux supposés du backtest ne doivent pas dépasser ceux du manifest.
Par défaut, le coût aller-retour est 30 bps : 2×5 frais + 10 spread + 2×5 glissement.
Les labels déduisent une hypothèse globale explicite de coût aller-retour. Les
prédictions supérieures au seuil proposent BUY ; négatives SELL ; sinon HOLD.
Une vente ne constitue jamais une autorisation de vendre à découvert.

Les trois benchmarks sont cash, buy-and-hold, et un momentum causal technique.
Le `baseline_rules` EMA/RSI demandé est **indisponible**, pas remplacé silencieusement
par le momentum. Le simulateur déterministe existant est toujours testé séparément.
Le rejeu utilise une seule position longue, un achat plafonné à 45 USD (frais
inclus), un capital initial <=50 USD, frais/spread/glissement, un proxy de
participation de 1 % du volume de la bougie, remplissages partiels, quantité à
8 décimales et minimum technique de 1 USD. Ces hypothèses arbitraires ne sont
pas les incréments du courtier ni une garantie de liquidité. Pas de carnet,
impact dynamique ou latence réelle. La position finale reste valorisée sans
liquidation forcée. Les métriques comprennent rendement net, recul maximal,
volatilité par pas, annualisation facultative explicite, Sharpe/Sortino si un
nombre de périodes annuel est fourni, turnover, frais, exécution, exposition et
stabilité par moitié de période. Univers fixe, aucun résultat sans biais de
survie ou sélection de stratégie démontré.

## Activation progressive et retour à l'état initial

Copier `multi_horizon.example.json` dans un fichier de configuration locale et
modifier explicitement `enabled` et l'horizon voulu. Utiliser `--config` pour la
CLI, ou `TRAD_MULTI_HORIZON_CONFIG=/chemin/config.json` pour le serveur web.
Les sous-budgets 15/15/20 USD totalisent 50 USD ; les augmenter au-delà est refusé.
L'activation de recherche ne peut armer aucun compte ni envoyer d'ordre.

```sh
python -m trading.research signal --input outputs/multi-horizon/minute.jsonl --strategy minute --as-of 2026-01-01T03:00:00+00:00 --config /chemin/config.json --model-dir outputs/multi-horizon/minute-walk-forward/window-000 --cost-bps 30 --output outputs/multi-horizon/nouveau-signal.json
```

Pour le dashboard, les rapports attendus sont `minute.json`, `swing.json`,
`portfolio.json`, et `<horizon>-backtest/results.json` sous
`outputs/multi-horizon` (ou `TRAD_RESEARCH_DIRECTORY`). Les métadonnées de modèle
et MSE sont lues sous `<horizon>-model/{manifest,evaluation}.json`. Pointer/copier
explicitement un modèle sélectionné à l'avance à cet emplacement pour l'afficher.
Les GET ne téléchargent, n'entraînent et ne démarrent aucun moteur.

La stratégie mensuelle propose une allocation équipondérée bornée avec cash
résiduel, pas une optimisation de rendement. Dans cet univers mono-actif et sans
vérification de tradabilité Paper, son signal s'abstient. L'allocation multi-actifs,
la liquidité, le turnover de rééquilibrage et le suivi de positions mensuelles
restent à implémenter après réception du moteur et d'un univers vérifié.

Pour désactiver : remettre `enabled=false`, tous les `*_enabled=false` et
`routing_enabled=false`, retirer les variables de routage explicites et relancer
le serveur. Les données et modèles peuvent rester sur disque ; leur présence
n'active aucune stratégie. Ne jamais supprimer un journal Paper pour réarmer
une expérience inconnue ; cette livraison ne le lit ni ne le modifie.

## LLM : configuration et mesure, sans installation automatique

Par défaut, le chemin connecté reste Ollama avec Qwen 2.5 1.5B. Aucun poids Qwen3
n'est supposé installé. Le lanceur macOS ne télécharge plus de poids au lancement :
il affiche une commande explicite et s'arrête si le modèle manque.

Activer le routage par `routing_enabled=true` dans le fichier local ou par
`TRAD_LLM_ROUTING_ENABLED=1`. Les variables `TRAD_ANALYST_MODEL`,
`TRAD_CRITIC_MODEL` et `TRAD_SYNTHESIZER_MODEL` permettent de modifier les candidats
8B/14B/8B sans toucher au code. Aucun basculement de fournisseur. Si un candidat
manque, refuser ; seule la variable explicite `TRAD_LLM_FALLBACK_MODEL`, qui doit
nommer un modèle installé, permet le repli de tous les rôles sur ce même modèle.
Le choix effectif est retourné dans `role_models` et les traces.

Les appels routés sont successifs, sérialisés localement et utilisent
`keep_alive=0` pour libérer chaque modèle après l'appel. Cela ne réserve pas la
RAM du Mac contre d'autres applications ou clients Ollama. Avant adoption sur
24 Go, mesurer le comportement réel :

```sh
python3 -m trading.llm_benchmark --single-model qwen2.5:1.5b --repeats 3 --output outputs/llm-benchmark.json
```

Le benchmark teste seulement les modèles déjà installés, signale les configurations
non exécutées et n'installe rien. Il conserve latence, tokens retournés par Ollama,
taux JSON valide, abstentions et pic RSS échantillonné des processus Ollama.
Cette mesure peut manquer des pics et compter deux fois des pages partagées ;
elle ne constitue pas une mesure précise de mémoire GPU/unifiée. Aucune conclusion
mono/multi-modèle ni recommandation Qwen3 ne peut être tirée sans ces résultats.
Le contexte de benchmark est une fixture synthétique, pas une validation de trading.

## Limites bloquantes pour la cible complète

1. Fournir la branche/ZIP avec le PaperEngine/PaperBroker prévu et ses tests,
   réconciliation, verrou de compte et politique 50/45 USD. Ne pas brancher les
   signaux sur OneShotPaper 1 000 USD par approximation.
2. Fournir l'actuelle baseline crypto EMA/RSI et ses contrats ; elle n'est pas
   présente dans ce dépôt. Les modes historiques existants restent préservés.
3. Constituer un historique point-in-time réel, un univers Paper négociable et
   un protocole indépendant hors échantillon, puis observer le Paper explicitement
   armé. Pas de validation financière réalisée ici.
4. Benchmarker les Qwen sur le Mac M5 réel et valider l'installation macOS.

Le risk gate accepte uniquement une vue Paper fournie par un futur adaptateur
fiable et refuse un état absent, non rapproché, ancien, incertain, en attente,
arrêté ou désarmé. Une dataclass avec `source=alpaca_paper_authoritative` n'est
pas une preuve de provenance contre du code Python hostile. L'orchestrateur ne
possède aucun transport ni fonction de soumission. Même une décision de risque
positive reste une proposition de recherche et retourne `execution.disabled`.
Le verrou de l'orchestrateur est uniquement local au processus ; ce n'est pas
le verrou de compte durable demandé. Il ne remplace pas celui du moteur absent.
Aucune liquidation automatique, réarmement, suivi continu ni routage live.

## Résultats finaux vérifiés et suite

- Suite standard : 133 tests, 131 réussis et 2 tests natifs ML ignorés.
- Suite dans le venv ML isolé : 133 tests réussis, aucun ignoré.
- Compilation Python, syntaxe JS/Bash et diff Git : réussis.
- Recettes demo/replay et recherche/entraînement/signal/SQLite/backtest : réussies
  sur fixtures synthétiques. Aucune performance de marché validée.
- Appels Alpaca de contrôle : HTTP 401 pour les trois marchés ; authentification
  réelle à corriger dans la configuration sécurisée, sans coller de clé en chat.
- Benchmark Ollama et validation Mac M5 : non exécutés, prérequis absents ici.

Prochaine phase : retrouver le moteur Paper et la baseline crypto attendus,
inspecter leurs contrats et exécuter leur baseline de tests avant d'ajouter le
pont StrategySignal. La Phase 5 d'exécution n'est pas achevée. La présence des
interfaces de recherche ne vaut ni intégration Paper ni acceptation complète
de la spécification. Les plafonds existants du simulateur n'ont pas été modifiés.
