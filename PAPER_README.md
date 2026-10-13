# Expérience Paper ponctuelle — préparation séparée

Cette branche ajoute une intégration **Alpaca Paper uniquement**, indépendante
du simulateur. Les commandes historiques `trading demo` et `trading.ai_cli`
restent hors ligne. Aucune boucle, planification, inférence distante ou exécution
continue n'est ajoutée. Aucun ordre distant n'a été envoyé pour valider ce code.

## État et prérequis

La configuration republiée a permis de vérifier les lectures authentifiées
Paper et données de marché. Cette observation ne garantit pas la configuration
d'un autre environnement et ne valide pas les écritures courtier. Vérifier la
politique effective dans chaque environnement avant toute lecture; les
destinations des secrets ne sont pas exposées dans les métadonnées locales :

- Vérifier que `paper-api.alpaca.markets` et `data.alpaca.markets` sont autorisés;
  si une modification est nécessaire, la republier avant utilisation.
- Autoriser **les deux destinations** `paper-api.alpaca.markets` et
  `data.alpaca.markets` pour chacun des secrets Network `ALPACA_PAPER_KEY` et
  `ALPACA_PAPER_SECRET_` (underscore final volontaire).

Le transport transmet seulement les placeholders protégés dans les deux en-têtes
Alpaca via le proxy HTTPS déjà configuré. Aucun secret dans une URL, un fichier
ou un journal. Il refuse le contournement du proxy, les hôtes non autorisés,
les redirections et le mode live. Vérification TLS/nom d'hôte active, aucune
nouvelle tentative automatique, délai socket 10 s, délai total 20 s par requête,
réponse limitée à 1 Mio. Les erreurs publiées sont des codes génériques.

## Commandes sans écriture courtier

Après confirmation réelle de la configuration, la commande suivante effectue
au maximum dix GET pour les trois candidats, sans lire le compte : horloge,
trois actifs, trois historiques bornés et trois cotations. Aucune pagination ni
substitution de flux automatique. Une erreur arrête le paquet.

```sh
python -m trading.paper_cli market-packet --confirm-secret-destinations --feed iex
```

Le paquet contient les dates exactes, jusqu'à 60 barres complètes d'une minute
par symbole, bid/ask et tailles, âge des données, source/flux et métadonnées de
séance. IEX couvre seulement cette place et ne représente pas le NBBO consolidé.
`sip` nécessite les droits correspondants; aucun repli silencieux vers IEX.
Les données crypto proviennent du lieu `us`. Un prix affiché ne garantit pas une
exécution. Les fichiers de test synthétiques ne servent jamais de données
d'exécution réelle.

Pour vérifier un instantané privé local sans réseau :

```sh
python -m trading.paper_cli dry-run --input outputs/private-snapshot.json
python -m unittest discover -s tests -v
python -m compileall -q trading tests
```

L'entrée dry-run comporte `spec` (champs de `OrderSpec`), `snapshot` (forme de
`PaperBroker.snapshot`) et `now`. La date fournie sert uniquement au rejeu local :
ce résultat n'autorise aucune soumission. Ne jamais committer un instantané de
compte. Le chemin `outputs/` et les bases SQLite sont déjà ignorés par Git.

## Garde-fous de l'expérience unique

- Au maximum **un achat limite IOC**, AAPL, MSFT ou BTC/USD, sans vente ni emprunt.
  Quantité × prix limite au plus 1 000 USD fictifs au total pour cette expérience.
  Le montant ne se renouvelle pas à chaque appel. Un remplissage partiel peut
  exister; l'IOC demande l'annulation de la quantité restante.
- Autorisation séparée portant sur le symbole, la quantité, le prix limite, le
  digest de l'analyse et une fenêtre de validité de cinq minutes maximum.
  Aucun modèle ni cette intégration ne choisit un ordre automatiquement.
- Compte actif en USD, trading autorisé, cash et pouvoir d'achat sans marge
  suffisants avec réserve supplémentaire de 1 %. Cette réserve n'est pas une
  estimation de frais. Aucun ordre ouvert, aucune position, aucune exposition
  courte préexistante : tout autre état entraîne l'abstention.
  La présence d'exposition longue dans le compte bloque aussi une réponse
  incohérente où la liste des positions serait vide. Le contrôle des ordres
  n'est filtré ni par symbole ni par côté : un seul résultat suffit à interdire
  l'achat. Ainsi aucune position ni aucun achat en attente n'est ajouté aux
  1 000 USD. Les champs de pouvoir d'achat avec marge ne sont jamais utilisés.
- Actif actif et négociable. Actions entières, prix au centime et au moins 1 USD;
  crypto seulement si le compte crypto est actif et les incréments/minimums de
  l'actif sont disponibles et respectés. Ces choix conservateurs peuvent refuser
  des ordres que le courtier accepterait autrement.
- Compte/horloge et cotation datant d'au plus 15 s, aucune date future; actions
  seulement pendant la séance régulière et à plus de 30 s de sa fermeture.
  Au moins 20 barres complètes, dernière barre datant d'au plus 180 s, séquence
  croissante sans trou supérieur à 120 s, OHLC et volumes valides.
- Bid/ask positifs, non croisés, tailles positives, spread maximal 0,5 %, prix
  limite entre bid et ask, écart ask/dernière clôture inférieur ou égal à 1 %.
  Ces seuils sont des filtres techniques prudents pour l'expérience, pas une
  stratégie démontrée. Le passage des contrôles n'oblige pas à acheter.

## Autorisation, tentative incertaine et rapprochement

Aucune commande CLI de soumission ou d'annulation n'existe. Le transport est
en lecture seule par défaut. Un appelant Python de confiance peut utiliser
`OneShotPaper.submit_approved` uniquement après l'autorisation explicite exacte
de l'opérateur; il doit sélectionner `mode="manual_one_shot"`, fournir le digest
attendu et activer les écritures sur le transport. Le mode par défaut est
`read_only`; soumission et annulation y sont interdites. Tout autre mode,
notamment récurrent ou sans surveillance, est refusé. Ces verrous évitent les
accidents; ils ne prouvent pas la présence d'un humain et ne sont pas une authentification
cryptographique contre du code Python malveillant.

Le `client_order_id` est fixé à `trad-paper-20261013-one-shot-01` pour toute
l'expérience, indépendamment du symbole. Le journal SQLite possède une seule
case atomique, persistée avant le POST. Conserver le même journal, par exemple
`outputs/paper-once/attempt.sqlite3`, après redémarrage; ne pas le supprimer pour
réessayer. Un second appel rapproche l'ordre existant; une spécification différente
est refusée. Même une erreur de prévalidation après réservation consomme cette
case : l'expérience s'arrête plutôt que de s'auto-réarmer.

Une **initialisation unique explicite** utilise `AttemptJournal(path, create=True)`;
elle refuse un fichier existant. Seule cette instance fraîchement initialisée
dispose d'une permission de première soumission, consommée une seule fois dans
le processus, sous verrou. Un redémarrage avant même cette première soumission
perd cette permission : la reprise ne réarme jamais l'expérience.

La reprise utilise `AttemptJournal(path)` et refuse un fichier manquant,
corrompu, un schéma incompatible ou une ligne de tentative supprimée alors que
le marqueur de consommation subsiste. Le remplacement de fichier est détecté
pendant la vie d'une instance. **SQLite local ne prouve pas l'absence de rollback
entre deux processus** : une ancienne copie valide peut restaurer à la fois la
tentative et son marqueur. Une détection durable de ce cas exigerait une autorité
indépendante persistante, absente de cette intégration.

Pour cette raison, toute instance rouverte est limitée au rapprochement. Même
avec une autorisation exacte, un journal récupéré vide réserve un état local et
interroge seulement le `client_order_id` fixe. Un ordre correspondant est
enregistré; une absence, un 404, une erreur ou une réponse ambiguë laisse l'état
inconnu, sans POST. Une copie restaurée n'engendre jamais une nouvelle identité.
Ne pas créer un autre journal avec `create=True` pour contourner cette limite;
le code Python de confiance pourrait le faire, et le stockage local ne peut pas
en certifier l'historique. Aucune migration/reset automatique ni exécution
récurrente/sans surveillance n'est proposée ou autorisée.
Le fichier est créé avec les permissions 0600 et conserve seulement le digest,
l'identifiant d'ordre et le dernier état/volume rempli confirmé, jamais un compte
complet ni des clés. Ces données privées restent sous `outputs/`.

Un verrou système non bloquant couvre toute soumission, tout rapprochement et
toute annulation utilisant ce journal. Un second processus reçoit `experiment_busy`;
les erreurs de verrou SQLite bloquent aussi avant toute soumission. Le verrou
est local au fichier Linux; il ne coordonne pas d'autres machines ni des clients
externes. Conserver un seul journal, ne pas créer de copie pour contourner un
état inconnu. Les tests couvrent interruptions Python et corruptions simulées;
ils ne démontrent pas une garantie contre toutes les pannes de stockage.

Avant tout premier POST manuellement permis : recherche de l'identifiant fixe
chez le courtier, puis nouvelles
lectures du compte, positions, ordres ouverts, actif, horloge et données. Après
préparation du transport, les dates et contrôles sont vérifiés encore une fois
juste avant l'envoi. Après
un timeout, une réponse ambiguë, un doublon ou une erreur : **aucun second POST**;
un GET par `client_order_id` recherche le résultat. Un 404 après tentative n'est
pas la preuve qu'aucun ordre n'existe. L'état reste inconnu et exige un
rapprochement manuel.

`reconcile` réalise un seul GET. Avec autorisation explicite d'annulation,
`cancel_open=True` et le `approved_digest` exact, il annule seulement l'ordre de cette expérience puis fait un
GET de confirmation. Un 204 n'est pas une preuve d'annulation; `pending_cancel`
reste en attente. Les quantités déjà remplies sont conservées. Aucune liquidation,
aucun ordre de remplacement, aucune annulation globale, aucune boucle infinie.
Un résultat accepté/ouvert doit être rapproché avant de conclure l'expérience.
Le GET de confirmation est aussi tenté après une erreur/timeout d'annulation,
car l'ordre a pu se remplir entre les deux appels. Un identifiant différent,
une quantité remplie en baisse ou une régression d'état terminal déclenche un
état inconnu. Le dernier remplissage confirmé reste enregistré et toute la
tentative reste consommée, même après annulation ou remplissage partiel.

Les lectures ne sont pas une transaction atomique avec le courtier : d'autres
clients peuvent modifier le compte entre les contrôles et la soumission. Cette
expérience exige l'usage exclusif du compte Paper pendant l'essai. La persistance
de l'environnement n'est pas un service garanti. Une exploitation continue
nécessiterait un registre durable partagé et un plafond cumulé couvrant toutes
les positions et tous les ordres en attente; elle n'est pas implémentée ici.
Le plafond actuel borne l'achat au prix limite. Une appréciation ultérieure
peut porter la valeur de marché de la position au-delà de 1 000 USD; ce code ne
surveille ni ne liquide les positions pour imposer un plafond permanent.

## Références officielles consultées

- [Cotation actions](https://docs.alpaca.markets/us/reference/stocklatestquotes-1)
  et [barres actions](https://docs.alpaca.markets/us/reference/stockbars)
- [Cotation crypto](https://docs.alpaca.markets/us/reference/cryptolatestquotes-1)
  et [barres crypto](https://docs.alpaca.markets/us/reference/cryptobars-1)
- [Horloge US](https://docs.alpaca.markets/us/reference/legacyclock)
  et [actif par symbole](https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id)
- [Création d'ordre](https://docs.alpaca.markets/us/reference/postorder),
  [ordre par identifiant client](https://docs.alpaca.markets/us/reference/getorderbyclientorderid),
  [annulation ciblée](https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1)
  et [ordres crypto](https://docs.alpaca.markets/us/docs/crypto-orders)
