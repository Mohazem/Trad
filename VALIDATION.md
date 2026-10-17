## Correction des séances

97 tests réussis. Contrôle Alpaca réel : AAPL et MSFT fermés, prochaine ouverture le 19 octobre 2026 à 13 h 30 UTC ; BTC/USD récent. Le stockage Qwen reste refusé par le proxy (CONNECT 403). Mise à jour réseau non enregistrée : brouillon non modifiable.

## Reprise du 16 octobre 2026 (Toronto)

- 95 tests unitaires réussis.
- Alpaca : AAPL et MSFT périmés, BTC/USD récent lors du contrôle.
- Ollama 0.6.8 démarré ; registre HTTP 200, poids refusés sur le domaine de stockage précisé dans LOCAL_MODEL.md. Aucune inférence locale validée.
- Ajout du lanceur `scripts/start_local.sh` pour reprendre les services sans téléchargement automatique.

# Validation du simulateur et de la couche IA

## Passage au petit modèle local

- 95 tests réussis : les quatre nouveaux tests vérifient le fournisseur Ollama, le schéma contraint, le plafond de sortie et l’absence de clé LLM requise.
- Ollama 0.6.8 installé après vérification SHA256 de l’archive officielle ; serveur CPU local démarré sur 127.0.0.1:11434.
- Fournisseur par défaut : `ollama`, modèle souhaité : `qwen2.5:1.5b`.
- Le téléchargement des poids a échoué avec `Forbidden` sur le registre Ollama. Les exigences réseau et les instructions d’installation/démarrage sont enregistrées en brouillon. L’inférence locale réelle reste non validée jusqu’au téléchargement.
- Le tableau de bord a été redémarré avec le fournisseur local et signale le modèle absent. Voir [LOCAL_MODEL.md](LOCAL_MODEL.md).

## Extension connectée validée dans la session du 16 octobre 2026

- 91 tests réussis (75 existants + 16 tests de collecte, fournisseur Hugging Face et serveur HTTP).
- Lecture authentifiée Alpaca des trois instruments : AAPL, MSFT, BTC/USD. Les horodatages anciens des actions sont signalés et exclus de l’analyse.
- Interface testée dans Chromium : trois graphiques affichés, aucun appel IA automatique, bouton IA désactivé sans configuration, aucune erreur JavaScript et aucune sortie horizontale à 390 pixels.
- Adaptateur Hugging Face testé avec un double de transport : schéma strict, trois appels maximum, refus des réponses tronquées/outils/refus et abstention sur erreur. Une vraie inférence nécessite encore une nouvelle clé sécurisée, un modèle servi et l’activation du domaine.
- Aucun ordre Paper ou réel envoyé. Voir [CONNECTED.md](CONNECTED.md).

Les vérifications historiques ci-dessous concernent le scénario synthétique initial.

Validation exécutée le 13 octobre 2026 dans l'environnement cloud de développement, Python 3.12. Aucun compte ni réseau de trading utilisé.

## Contrôles exécutés

- `python -m unittest discover -s tests -v` : **75 tests réussis** : 36 sur le socle déterministe et 39 sur la couche IA, aucune erreur.
- `python -m compileall -q trading tests` : réussi.
- `python -m trading demo --output outputs/demo --config config.example.json` : démonstration synthétique terminée.
- `python -m trading status --ledger outputs/demo/ledger.sqlite3` : état lu avec succès en lecture seule.
- Intégration : deux démonstrations indépendantes produisent des exports identiques octet pour octet.
- Intégration : `PRAGMA integrity_check` de SQLite renvoie `ok`.
- Intégration : les sockets sont remplacées par une erreur pendant l'exécution de la démonstration; celle-ci termine sans appel réseau.

## Validation IA ajoutée

- `python -m trading.ai_cli mock --output outputs/ai-mock-demo` : trois réponses simulées par un double déterministe, une exécution locale après les contrôles indépendants. **Aucune inférence dans ce mode.**
- `python -m trading.ai_cli replay --input ai_samples/recorded_replay.json --output outputs/ai-recorded-demo` : trois réponses réellement produites par des modèles pendant un test ponctuel, toutes validées, synthèse `abstain`, **zéro opération**. Le rejeu ne réalise pas de nouvelle inférence.
- Schémas stricts, champs manquants/supplémentaires, clés JSON dupliquées, valeurs non finies, scores hors plage, symboles/actions inconnus et sorties trop longues : rejets vérifiés.
- Sources absentes, inventées, dupliquées ou sans rapport avec le titre; données périmées, futures ou dates incohérentes : rejets vérifiés.
- Appels successifs limités à trois, échec sans retry, timeout puis désactivation de l'instance, abstention volontaire et score insuffisant : vérifiés.
- Rejet critique, changement de symbole/direction et tentative de choisir des quantités ou des plafonds : blocages vérifiés.
- Texte d'injection demandant d'ignorer les plafonds : aucun contournement des limites déterministes.
- Veto déterministe après acceptation des rôles IA, vente sans position, coupe-circuit et plafond par ordre : vérifiés.
- Même opportunité rejouée à une seconde d'intervalle ou avec d'autres mots/scores : une seule opération. Changement de direction : conflit sans deuxième opération.
- Mock exécuté avec sockets et accès `os.getenv` remplacés par des erreurs : aucune utilisation détectée.
- Adaptateur OpenAI testé avec un faux client : JSON Schema strict, absence d'outils, `store=False`, plafond de sortie, `max_retries=0`, timeout, refus, réponse incomplète/vide et endpoint non autorisé.
- **Aucune authentification ni requête réelle à une API LLM par le programme Python n'a été testée.** La provenance d'un fichier d'enregistrement n'est pas certifiée cryptographiquement.

Les commandes et limites sont détaillées dans `AI_README.md`. Les traces des démonstrations et les bases SQLite ne sont pas publiées; elles sont régénérables avec les commandes indiquées.

## Couverture des tests

- Rejet des quantités négatives/nulles, nombres non finis, floats de comptabilité, précision de quantité excessive, mauvaise classe d'actif, symbole inconnu et dates invalides.
- Normalisation explicite des décimaux de configuration et refus des champs ou modes non reconnus.
- Prix manquants, trop anciens, futurs ou éloignés du prix de référence.
- Obligation d'une critique explicite et trace des veto.
- Valorisation fraîche de tous les actifs déjà détenus, y compris lors d'une demande portant sur un autre marché.
- Exposition brute partagée, plafond crypto, plafond par symbole et réserve de trésorerie.
- Interdiction de la vente à découvert et contrôle du montant d'un ordre.
- Réduction d'une position après dérive de son exposition; aucune exception au gel lorsque l'arrêt est actif.
- Frais/glissement et états comptables exacts avec Decimal; invariance face à la précision Decimal modifiée par un appelant.
- Répétition d'ordres exécutés et bloqués, reprise après redémarrage, conflit de contenu pour un même identifiant.
- Deux connexions concurrentes soumettant le même ordre : une seule exécution, une répétition reconnue.
- Erreur injectée pendant l'écriture de l'audit après changements comptables provisoires : rollback complet, puis relance réussie.
- Horloge du portefeuille non décroissante; répétition d'un ordre historique toujours reconnue.
- Arrêt manuel et arrêt sur drawdown conservés après redémarrage/reprise des prix; déclenchement sans nouvel ordre et après frais d'exécution.
- Recherche ignorant les observations futures et critique rejetant les variations extrêmes ou une preuve non correspondante.
- Allowlist exacte de l'origine Paper : refus de l'origine live, chemins, ports, userinfo, fragments, query strings, variantes de schéma et hôtes trompeurs.
- `AlpacaPaperSkeleton.submit()` lève systématiquement `NetworkTradingDisabled`, même avec des options qui prétendent activer le réseau.

## Résultat de l'échantillon validé

- 72 observations horaires synthétiques sur trois instruments
- 79 propositions issues des règles et 4 sondes explicites
- 71 exécutions locales, 12 blocages, 83 décisions persistées
- 83 répétitions reconnues sans changement d'état ou d'audit
- Aucune position ouverte à la fin du scénario
- 98 905,9909178710661320256850 USD fictifs en caisse

Ces valeurs vérifient la reproductibilité d'un scénario fabriqué. Elles ne démontrent aucune performance en marché.

## Non testé et non implémenté

Connexion/authentification Alpaca, acceptation effective des ordres par un courtier, données réelles, authentification et appel réseau de l’adaptateur LLM, règles de séances, fills partiels, traitement des timeouts distants, reprise après panne machine, performance financière, surveillance continue et service 24 h/24.

Les erreurs injectées testent le rollback applicatif. Elles ne remplacent pas une qualification du stockage après panne électrique. Aucune action distante ambiguë n'existe ici; la réconciliation avant tout renvoi devra être développée avant d'ajouter un transport Paper.
