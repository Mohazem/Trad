# Marchés Alpaca et avis Hugging Face

**Mise à jour : le fournisseur par défaut est maintenant Ollama local avec Qwen 2.5 1.5B. Voir [LOCAL_MODEL.md](LOCAL_MODEL.md).** Les commandes ci-dessous utilisent le modèle local sauf si `TRAD_AI_PROVIDER=huggingface` est explicitement configuré. Les indications de coût et les variables HF de ce document concernent cette option distante uniquement. Le test authentifié HF a renvoyé HTTP 402 (crédits/facturation) ; le passage local évite cette dépendance.

Le programme lit des observations de marché réelles pour AAPL, MSFT et BTC/USD. Une demande explicite peut lancer une chaîne analyste → critique → synthèse via Hugging Face Inference Providers. Cette chaîne donne un avis : elle n’appelle aucun registre d’exécution ni courtier.

## Prérequis et configuration

Python 3.11+ ; bibliothèque standard seulement, sans installation de paquets. Depuis `/workspace/Trad`, injecter les variables via les paramètres sécurisés de l’environnement ou un gestionnaire de secrets. Ne pas mettre de clé dans une conversation, un fichier versionné ou une commande enregistrée. Une clé exposée doit être révoquée avant remplacement. Aucun fichier `.env` n’est chargé automatiquement.

| Variable | Utilisation |
| --- | --- |
| `ALPACA_PAPER_KEY` | Identifiant existant pour la lecture de données Alpaca. Alternative : `APCA_API_KEY_ID`. |
| `ALPACA_PAPER_SECRET_` | Secret existant, y compris le `_` final. Alternative : `APCA_API_SECRET_KEY`. |
| `TRAD_HF_TOKEN` | Nouvelle clé Hugging Face autorisée à appeler Inference Providers. Alternative locale : `HF_TOKEN`. |
| `TRAD_HF_MODEL` | Identifiant d’un modèle de chat servi par Inference Providers, compatible JSON Schema strict. Suggestion initiale à vérifier avec le compte : `Qwen/Qwen3-32B`. Un suffixe fournisseur peut être utilisé si le routage HF le permet. |

Le jeton nécessite la permission d’appeler Inference Providers et des crédits ou un quota suffisants. La disponibilité du modèle et le support de JSON Schema dépendent du fournisseur choisi. Le programme ne bascule pas vers un format non validé en cas d’incompatibilité : il s’abstient. Le modèle proposé n’a pas encore été validé avec un compte authentifié dans cette session.

Destinations HTTPS : `data.alpaca.markets` pour les données et `router.huggingface.co` pour l’IA. Les clés ne sont envoyées qu’à leur destination, dans les en-têtes ; elles ne sont ni retournées au navigateur ni inscrites dans les résultats. Vérification TLS conservée, redirections refusées et aucune nouvelle tentative automatique. L’ancien réglage OpenAI créé avant le choix de Hugging Face n’est pas requis par ce mode ; il peut être retiré dans les paramètres de l’environnement.

## Lancer l’interface

```sh
cd /workspace/Trad
python -m trading.web --port 8080
```

Le serveur écoute uniquement sur l’interface locale `127.0.0.1`, au port choisi. Sur une machine où le navigateur est local au serveur, ouvrir son adresse locale sur ce port. Un navigateur sur un autre ordinateur nécessite un mécanisme de transfert de port adapté ; ce programme n’expose pas de service public. Arrêter avec Ctrl+C. Le processus doit être relancé après une nouvelle session.

La page affiche trois graphiques SVG, les valeurs tabulaires, les états de fraîcheur et les réponses des trois rôles. « Actualiser les marchés » ne déclenche aucun appel IA. « Demander une analyse IA » exige une clé et un modèle et peut consommer des crédits. Un chargement de page ne lance pas d’inférence. Le serveur limite les demandes IA à une par minute et refuse les appels simultanés. Ce plafond s’applique au processus web ; les commandes CLI restent sous le contrôle de l’opérateur et ne constituent pas un quota de compte.

Les endpoints internes `/api/health` et `/api/market` servent aux contrôles locaux. La santé HTTP indique que le serveur répond, pas que les credentials distants fonctionnent. Vérifier les états des trois instruments et leurs dates. `/api/analyze` nécessite un POST de même origine avec le jeton de requête de la page. Les vérifications d’hôte et d’origine protègent l’interface locale ; elles ne remplacent pas une authentification pour un futur déploiement public.

## Utiliser la ligne de commande

```sh
# Marchés seulement, sans appel IA
python -m trading.connected

# Demande explicite : au plus trois appels au fournisseur choisi, sans ordre
python -m trading.connected --analyze

# Enregistrer dans un NOUVEAU fichier (le dossier doit déjà exister)
python -m trading.connected --output /tmp/mon-nouveau-rapport.json

# Vérification complète hors réseau avec doubles de fournisseurs
python -m unittest discover -s tests -v
```

## Données et limites de fraîcheur

Chaque acquisition effectue trois requêtes Alpaca séparées, une par instrument, pour éviter qu’une pagination multi-symboles n’omette des titres. Le tableau affiche les 30 dernières bougies minute disponibles sur les sept derniers jours. AAPL et MSFT utilisent IEX, qui n’est pas une vue consolidée de toutes les places boursières. BTC/USD utilise le flux crypto US d’Alpaca. Les prix sont des clôtures de bougies, pas des cotations exécutables.

Les horodatages de source sont conservés. Une bougie future, un prix invalide ou un horodatage dupliqué fait rejeter la série. Les espaces sur les graphiques respectent le temps écoulé ; neuf observations peuvent donc couvrir plus de neuf minutes en présence de trous. Le momentum utilise les neuf dernières observations, pas une durée garantie.

Pour l’IA, il faut au moins neuf observations et une dernière bougie âgée d’au plus 180 secondes. Une série ancienne reste affichée mais est explicitement exclue du paquet transmis au modèle. Si toutes les séries sont anciennes ou indisponibles, aucun appel n’est effectué. Les actions peuvent être exclues lorsque le marché est fermé ou qu’aucune transaction IEX récente n’est disponible. La fraîcheur est revérifiée après l’inférence : un résultat devenu périmé n’est pas publié.

## Appels Hugging Face

Le fournisseur utilise `POST https://router.huggingface.co/v1/chat/completions` avec un format JSON Schema strict. Chaque rôle dispose de 20 secondes et de 1 200 tokens de sortie au maximum. Une demande complète peut donc comporter trois appels et jusqu’à 3 600 tokens de sortie ; les tokens d’entrée sont facturables également. Ce plafond ne garantit aucun prix précis. Une sortie tronquée, un appel d’outil, un refus, une erreur réseau ou un JSON invalide conduit à l’abstention sans retry.

Seuls les résumés numériques des séries récentes sont envoyés : identifiants, symboles, dates, prix, momentum, plus forte variation et nombre d’observations, ainsi que les réponses précédentes validées. Les barres complètes restent dans l’interface locale. La politique de traitement et de rétention relève de Hugging Face et du fournisseur routé. Le code ne promet pas une absence de rétention externe.

Le mode connecté ne dimensionne ni ne soumet d’ordre. Les propositions ne peuvent pas modifier des plafonds, lire des clés ou déclencher des outils. Le simulateur synthétique reste séparé : les paquets `market` sont refusés à sa frontière d’exécution. Un avis « achat » ou « vente » ne représente aucune performance validée.

## État de validation dans cette session

- 91 tests réussis : 75 existants et 16 ajoutés pour la collecte, la séparation simulation/marché, Hugging Face et l’interface HTTP.
- Lecture authentifiée Alpaca réussie pour AAPL, MSFT et BTC/USD ; 30 observations par titre. Les cours des actions étaient anciens à l’heure du contrôle, BTC/USD récent.
- Chaîne trois rôles Hugging Face et HTTP testés avec des réponses de test, sans inférence réelle.
- Lors du contrôle ultérieur, token et modèle HF ont été injectés mais le fournisseur a renvoyé HTTP 402. Le mode local ne nécessite pas ces paramètres. Voir LOCAL_MODEL.md pour l’état courant.

Après saisie de la nouvelle clé et application des paramètres, relancer `python -m trading.connected --analyze`. Un résultat `provider_error` ne valide pas l’inférence ; vérifier les droits Inference Providers, le quota, le modèle, le support JSON Schema et le réseau. Aucune clé ne doit être ajoutée aux journaux pour diagnostiquer un échec.

Documentation officielle de référence : [Chat completion](https://huggingface.co/docs/inference-providers/tasks/chat-completion), [Inference Providers](https://huggingface.co/docs/inference-providers/index). Le format de requête a été vérifié dans la documentation du dépôt officiel `huggingface/hub-docs` pendant cette session.

## État des séances actions

Le tableau de bord interroge uniquement en lecture l’horloge Alpaca Paper (`GET /v2/clock`). Une réponse récente indiquant une fermeture affiche « Marché fermé » et la prochaine ouverture. Les cours anciens restent exclus de l’analyse. Si l’horloge échoue, les contrôles de fraîcheur continuent sans supposer de fermeture. Aucun endpoint d’ordre n’est autorisé.

## Vue de recherche multi-horizons

`/strategies` et `/api/strategies` lisent uniquement les artefacts locaux décrits
dans [docs/MULTI_HORIZON.md](docs/MULTI_HORIZON.md). Ces routes ne consultent
pas Alpaca et ne démarrent aucun moteur. La collecte connectée ignore les
bougies minute encore en cours. Le routage LLM optionnel conserve Ollama et
exige des modèles installés ou un fallback explicitement configuré.
