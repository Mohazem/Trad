# Trad : marchés connectés, analyse IA et simulation

**Recherche multi-horizons expérimentale : [contrats, commandes et limites](docs/MULTI_HORIZON.md).**
Les paramètres nouveaux sont désactivés ; aucune exécution Paper multi-horizons
n’est implémentée dans ce checkout. Vue de lecture : `/strategies`.

**Installation sur Mac M5 / Apple Silicon : [instructions pour Codex local](MACOS.md).**
Lancer `bash scripts/start_macos.sh` après installation de Python et Ollama.

Un mode **consultatif connecté** récupère AAPL, MSFT et BTC/USD via Alpaca, puis peut demander trois avis au petit modèle **Qwen 2.5 1.5B exécuté localement avec Ollama**. Hugging Face reste une option explicite. Une interface HTML affiche les graphiques, les dates des observations et les avis. Aucun ordre courtier n’est envoyé.

```sh
bash scripts/start_local.sh --port 8080
```

Voir [LOCAL_MODEL.md](LOCAL_MODEL.md) pour installer et démarrer le modèle local, et [CONNECTED.md](CONNECTED.md) pour les données Alpaca et l’option Hugging Face. Les vérifications historiques des accès marché et du modèle sont décrites dans LOCAL_MODEL.md ; elles ne garantissent pas l’état de la session courante. Le simulateur décrit ci-dessous garde son fonctionnement hors ligne.

## Socle de simulation hors ligne, actions et crypto

Version 0.2.0, Python 3.11+. Simulation et rejeu : bibliothèque standard uniquement. Adaptateur API LLM optionnel : client SDK fourni par un environnement autorisé.

**Cette version associe le simulateur déterministe à une couche consultative IA à trois rôles. Trois réponses réellement produites par des modèles pendant un test ponctuel sont enregistrées et rejouables; leur synthèse s’abstient. Le programme Python n’a pas encore authentifié ni appelé une API LLM distante. Aucun service autonome continu ni connexion courtier n’est déployé. Voir [AI_README.md](AI_README.md) pour l’état exact et les commandes.**

## Ce qui fonctionne

- Couche IA analyste → critique → synthèse, schémas JSON stricts, validation de sources, abstention en cas d’erreur, dimensionnement et risque indépendants du modèle. [Détails et rejeu](AI_README.md).
- Scénario synthétique reproductible sur AAPL, MSFT et BTC/USD, dans un portefeuille commun en USD fictifs.
- Recherche déterministe sur le passé observé, signal de momentum, critique de cohérence/variation extrême, puis contrôle de risque indépendant dans le moteur d'exécution.
- Intentions d'ordre typées, dates UTC explicites, durée de validité, garde contre les prix périmés/futurs et l'écart au prix de référence.
- Frais et glissement fictifs, solde espèces et positions longues uniquement, sans emprunt ni vente à découvert.
- Plafonds cumulés actions + crypto, crypto, par titre, par ordre et réserve de trésorerie.
- Écriture SQLite atomique : portefeuille, ordre, frais, audit et état d'arrêt. Une répétition identique renvoie la décision originale sans nouvel ordre. Réutiliser un identifiant avec un autre contenu provoque un conflit audité.
- Marquage du portefeuille à chaque pas, même sans signal. Le recul depuis le sommet déclenche un arrêt persistant, conservé après redémarrage et après reprise des prix.
- Squelette Alpaca Paper limité à la préparation d'un dictionnaire illustratif. Son appel submit lève toujours une erreur, sans transport réseau. L'URL réelle de trading est interdite.

## Exécuter

Depuis le dossier du projet :

```sh
python -m unittest discover -s tests -v
python -m trading demo --output outputs/mon-essai --config config.example.json
python -m trading status --ledger outputs/mon-essai/ledger.sqlite3
```

Les sorties locales et bases SQLite sont exclues du dépôt. La commande de démonstration les génère dans le répertoire choisi. Choisir un nouveau répertoire pour chaque nouvelle démonstration : aucun registre existant n'est écrasé. La commande `status` lit la base en lecture seule.

Pour la simulation, le mock et le rejeu, aucune installation de paquet, aucune clé et aucun compte ne sont nécessaires. Aucun fichier de secrets ni aucune variable de credentials ne sont lus. Les CLI n’activent pas le réseau; l’adaptateur LLM optionnel nécessite l’injection explicite d’un client authentifié par un environnement autorisé. Le connecteur de trading reste sans transport réseau.

## Résultat de la démonstration validée

- 72 pas horaires entièrement inventés
- 79 intentions issues des règles, plus 4 sondes explicites de sécurité
- 71 exécutions simulées et 12 intentions bloquées
- 83 répétitions vérifiées sans modification du portefeuille ni de l'audit
- Blocages : 8 variations extrêmes rejetées par la critique, 1 intention expirée, 1 ordre excessif, 1 vente à découvert, 1 prix périmé

Le capital final fictif est d'environ 98 905,99 USD sur 100 000 USD fictifs au départ. Ce résultat n'est ni un backtest de marché ni une mesure de performance prédictive : la série est fabriquée, les règles sont arbitraires et aucun bénéfice futur n'est démontré. La démonstration observée est perdante; elle vérifie le comportement du logiciel.

## Hypothèses de risque à confirmer

Le seuil arbitraire de 20 % du fichier d'exemple mesure le recul du portefeuille total depuis son plus haut valorisé, positions ouvertes comprises : `1 - equity / peak_equity`. Ce paramètre sert uniquement à la démonstration technique et ne constitue pas une recommandation. Toute utilisation différente nécessite une définition et des paramètres explicitement validés.

Tous les autres chiffres de configuration (capital fictif, plafonds, frais, glissement, fréquence, momentum) sont des **valeurs arbitraires de démonstration technique**. Ils ne sont ni calibrés ni proposés comme réglages financiers à déployer.

L'arrêt bloque **toutes** les nouvelles soumissions, y compris les ventes. Il ne liquide pas les positions, ne place pas de stop chez un courtier, ne protège pas les sorties et ne garantit pas une perte maximale. Les prix peuvent franchir le seuil entre deux observations. Des frais peuvent déclencher l'arrêt juste après une exécution simulée. Il n'existe pas de bouton de réarmement dans cette version.

## Fichiers produits

- `ledger.sqlite3` : registre autoritaire et état persistant
- `status.json` : état et compteurs de la démonstration
- `orders.jsonl` : décisions, empreintes d'intentions, prix/frais simulés
- `audit.jsonl` : décisions bloquées/exécutées, conflits et arrêts
- `equity_curve.jsonl` : valorisations synthétiques
- `agent_decisions.jsonl` : résultats des rôles déterministes
- `VALIDATION.md` : vérifications exécutées et limites

Les montants sont enregistrés en chaînes décimales, avec un contexte de calcul fixe à 28 chiffres. Les devises sont simplifiées à USD. Une modification de configuration nécessite un autre registre pour éviter de changer silencieusement les règles d'un compte existant. Les exports sont une copie consultable; la base reste la source de vérité.

## Limites importantes

- Les noms ResearchAgent, SignalAgent, CriticAgent et RiskAgent du simulateur de base décrivent des règles déterministes. La couche IA distincte dispose de trois rôles consultatifs et d’un adaptateur OpenAI, testé avec un double de client. Le rejeu inclus contient de vraies réponses de modèles enregistrées, mais ne réalise pas de nouvelle inférence. Aucune connexion API LLM authentifiée n’a été validée par le programme Python.
- **Dans le simulateur hors ligne : aucune connexion Alpaca**, aucune authentification, aucune lecture de compte, aucun ordre Paper distant, aucun ordre réel. Le parcours connecté séparé consulte les marchés et l’horloge Paper. `prepare_payload` ne prouve pas qu'un courtier accepterait un ordre : pas de vérification des pas de cotation, fractions disponibles ou règles de titre.
- Le simulateur emploie des prix avec glissement; le dictionnaire Alpaca illustratif décrit un ordre limite au prix de référence. Ce sont deux mécanismes distincts sans garantie d'équivalence.
- Les étiquettes intraday/swing correspondent uniquement à deux fenêtres de calcul (3 et 8 pas). Pas de clôture forcée de séance, jours fériés, horaires réels ou gestion robuste de positions sur plusieurs jours. Les cotations horaires inventées existent même hors séance.
- Pas de forex, actualités, données historiques réelles, dividendes, splits, taxes, borrow fees, spread variable, carnet d'ordres, liquidité, latence, remplissage partiel, ordre en attente ou réconciliation courtier.
- La critique est une interface interne de confiance; l'API Python n'est pas une frontière d'authentification. Toute intégration future doit garantir la provenance des avis de critique.
- Pas de supervision continue, disponibilité 24 h/24, reprise orchestrée, service cloud durable ni promesse de conservation indéfinie des fichiers de cet environnement.
- L'audit local n'est pas un registre inviolable. Les tests de rollback simulent des erreurs Python, pas toutes les pannes disque ou toutes les coupures électriques.

## Suite avant tout branchement Paper

1. Confirmer le périmètre, la définition et le seuil du drawdown, les instruments, le budget fictif et les objectifs de test.
2. Ajouter de vraies données et un protocole de validation hors échantillon, sans fuite d'information future.
3. Configurer et valider le fournisseur LLM de la couche IA avec une authentification sécurisée, puis mesurer la qualité des propositions. Les règles de risque et l’exécution restent hors du contrôle direct des modèles.
4. Établir un chemin sécurisé d'authentification et un déploiement persistant. Ne jamais transmettre de clés dans une conversation. Toute clé déjà exposée doit être révoquée et remplacée chez son fournisseur.
5. Développer séparément le connecteur Paper, vérifier les règles du courtier et les horaires, puis gérer ordres en attente, exécutions partielles et réconciliation. Un timeout de soumission doit devenir un état « résultat inconnu » : aucun nouvel envoi avant rapprochement de l'identifiant client. Cette logique distante n'est pas implémentée ici.
6. Tester en Paper de bout en bout, observer sur une période significative et revoir les risques avant toute discussion d'une utilisation réelle.
