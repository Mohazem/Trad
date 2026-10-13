# Couche IA consultative et simulation locale

## État réel de cette version

La chaîne **analyste → critique → synthèse** est implémentée, avec un fournisseur interchangeable et un adaptateur pour l'API OpenAI Responses. Les propositions passent ensuite par les contrôles déterministes existants.

Trois réponses réellement produites par des modèles pendant un test ponctuel sont conservées dans `ai_samples/recorded_replay.json`, puis rejouées par le programme. Dans ce test, l'analyse constate une tendance synthétique positive sur AAPL, la critique relève les limites, et la synthèse **s'abstient** faute d'éléments suffisants. Aucune opération n'en résulte.

**Le programme Python n'a pas authentifié ni appelé une API LLM distante pendant cette validation.** L'adaptateur OpenAI est testé avec un double de client. Il reste à configurer et à valider dans un environnement autorisé. Rejouer des réponses enregistrées ne déclenche pas une nouvelle inférence.

Le fournisseur par défaut est un **mock déterministe**, explicitement nommé `offline_mock_no_inference`. Il sert à vérifier une proposition et son passage dans le simulateur; il n'est pas présenté comme une IA.

## Exécuter sans clé, sans compte et sans réseau

Depuis la racine du projet, Python 3.11+ :

```sh
python -m unittest discover -s tests -v
python -m trading.ai_cli contracts
python -m trading.ai_cli mock --output outputs/mon-test-mock
python -m trading.ai_cli replay --input ai_samples/recorded_replay.json --output outputs/mon-test-ia
```

Choisir un nouveau dossier de sortie : un dossier existant n'est jamais écrasé. Aucun ordre distant, Paper ou réel, n'est envoyé. Le temps du test est la date du paquet synthétique, pas une cotation actuelle. Le rejeu d'un ancien paquet dans cette horloge historique ne prouve pas sa fraîcheur dans le monde réel.

Résultats produits :

- `ai_trace.json` : fournisseur, mode, provenance déclarée, réponses validées, décision de la chaîne et résultat local
- `ai_input.json` : paquet numérique entièrement synthétique utilisé
- `ledger.sqlite3`, `status.json`, `orders.jsonl`, `audit.jsonl`, `equity_curve.jsonl` : registre et exports du simulateur

La provenance des enregistrements est une déclaration documentaire; le code ne peut pas certifier l'identité d'un modèle à partir d'un fichier JSON. Les empreintes lient le paquet et l'opportunité, mais ne constituent pas des signatures cryptographiques de provenance.

## Les trois rôles

1. **Analyste** : choisit un titre autorisé, une orientation et des références aux données fournies. Il n'invente pas de source externe.
2. **Critique** : accepte ou rejette l'analyse et explicite les limites. Un rejet interdit une suggestion d'achat ou de vente.
3. **Synthèse** : garde le même titre et choisit achat, vente ou abstention. Elle peut toujours s'abstenir; elle ne peut pas inverser l'orientation analysée.

Les schémas exacts sont dans `trading/ai_contracts.py`, et la commande `contracts` les exporte. Tous les champs sont obligatoires, les champs supplémentaires sont interdits. Les trois rôles n'ont aucun outil de navigation, de code ou de trading.

Le paquet contient uniquement des identifiants, dates, symboles et nombres bornés. Chaque symbole dispose d'une cotation et d'un résumé synthétiques au même instant. Les sorties citent au moins une source pertinente pour leur titre. La validité d'une citation ne prouve pas que chaque phrase du raisonnement est vraie : il ne s'agit pas d'un vérificateur sémantique de toutes les affirmations.

## Limites indépendantes du modèle

- Maximum trois appels successifs par évaluation, sans boucle de réparation ni nouvelle tentative
- Maximum 10 secondes par rôle par défaut, configurable entre plus de zéro et 30 secondes; sortie JSON limitée à 32 Kio et sortie API à 2 048 tokens
- Timeout, refus, résultat incomplet, erreur, format invalide, champ inattendu, symbole inconnu, action non permise, NaN, dépassement de plage, source inventée ou données périmées : abstention
- Le paquet est vérifié avant tout appel et à nouveau avant l'exécution locale; les sources futures sont refusées
- Les données et le texte des rôles précédents restent dans le contenu utilisateur, jamais dans les instructions de confiance
- Les propositions ne peuvent définir ni quantité, ni seuil de risque, ni clé, ni endpoint, ni ordre brut
- Le dimensionnement de démonstration vaut 1 % du capital fictif initial au prix fourni, indépendamment du score du modèle
- Le seuil de confiance de 0,55 est un filtre **arbitraire de test**, pas une probabilité de réussite ni un réglage financier recommandé
- `CriticAgent.review` est appelé de nouveau, puis `Ledger.submit` réapplique atomiquement tous les plafonds et le coupe-circuit. Un avis favorable du LLM ne remplace jamais l'avis déterministe
- Une opportunité est identifiée par le paquet, le titre et la stratégie. Changer seulement les mots ou la confiance ne peut pas créer une nouvelle opération; un changement incompatible provoque un conflit

Les tests d'injection montrent que des instructions glissées dans une réponse ne peuvent pas modifier les plafonds ni déclencher des outils. Cela ne garantit pas qu'un modèle ne soit jamais influencé dans son choix parmi les actions permises. Les fournisseurs Python et le client SDK restent du code de confiance, pas une sandbox pour du code hostile.

Le timeout de la chaîne abandonne la réponse et désactive cette instance. Un fournisseur qui ne respecte pas l'annulation peut continuer brièvement dans un thread daemon; il n'a aucun accès au registre et sa réponse tardive ne sera pas exécutée. Le SDK reçoit aussi un timeout et `max_retries=0`. Une isolation de processus et des quotas durables sont à prévoir avant tout service continu.

## Intégrer un fournisseur OpenAI dans un environnement autorisé

L'interface attend un client SDK déjà authentifié par un composant de confiance, sans lire de clé ni de variable d'environnement dans cette couche :

```python
from trading.ai_providers import OpenAIResponsesProvider
from trading.ai_pipeline import AdvisoryPipeline

# authenticated_client est fourni par votre infrastructure sécurisée.
# approved_model est un identifiant explicitement choisi par l'opérateur.
provider = OpenAIResponsesProvider(client=authenticated_client, model=approved_model)
pipeline = AdvisoryPipeline(provider, timeout_seconds=10)
# pipeline.evaluate(packet, simulation_time) effectue les appels consultatifs.
```

Aucune commande CLI ne lit une clé ou n'active le réseau. Ne jamais placer une clé dans les fichiers d'exemple, les journaux ou une conversation. L'intégration de l'authentification, le budget API, le modèle autorisé, la politique de journalisation et les permissions sont des prérequis externes à cette version. Le client doit être celui d'une infrastructure autorisée et pointer exactement vers `https://api.openai.com/v1`.

L'adaptateur utilise `client.with_options(timeout=..., max_retries=0).responses.create(...)`, avec `text.format.type="json_schema"`, `strict=True`, `tools=[]`, `tool_choice="none"`, `store=False` et un plafond de tokens. Il n'envoie que le paquet synthétique et les sorties déjà validées. `store=False` ne signifie pas absence de toute rétention fournisseur. Les erreurs sont réduites à des codes génériques pour ne pas publier leurs corps ou en-têtes.

Documentation officielle consultée le 13 octobre 2026 :

- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
- [Responses create](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
- [SDK Python officiel : délais, erreurs et tentatives](https://github.com/openai/openai-python)

## Ce qui reste à construire et valider

- Authentification sécurisée et test réel de l'adaptateur API, avec modèle et budget choisis
- Acquisition de données de marché réelles, contrôle de leur qualité et provenance, puis validation hors échantillon sans fuite d'information future
- Évaluation de la pertinence des décisions, des hallucinations et des désaccords; la validation JSON ne démontre aucun avantage de trading
- Intégration Paper distante avec confirmation des paramètres, gestion des exécutions partielles et rapprochement des ordres
- Supervision, planification durable et exploitation continue

Ce livrable fonctionne sur un instantané **entièrement synthétique**. Il ne constitue ni un backtest réel, ni une stratégie calibrée, ni un service autonome continu. Aucune performance financière et aucune limitation garantie des pertes ne sont démontrées. La définition du seuil arbitraire de démonstration de 20 % et les autres réserves du README restent applicables.
