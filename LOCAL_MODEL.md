# Petit modèle exécuté localement

Trad utilise désormais **Ollama + Qwen 2.5 1.5B** par défaut. Le modèle est quantifié et vise une petite consommation mémoire (téléchargement de l’ordre de 1 Go, mémoire d’exécution de quelques Go). Il tourne sur le CPU de cette machine : pas de facturation d’API LLM, pas de token Hugging Face requis. Le calcul et l’hébergement de la machine restent des ressources consommées. Les données de marché continuent à venir d’Alpaca.

## Reprendre en une commande

```sh
bash scripts/start_local.sh --port 8080
```

Le lanceur réutilise un serveur Ollama existant ou en démarre un, puis ouvre le serveur du tableau de bord. À l’arrêt, il ferme uniquement le serveur Ollama qu’il a lui-même lancé. Il ne télécharge pas automatiquement les poids ; sans modèle, les données de marché restent consultables.

## Installer et lancer

Depuis `/workspace/Trad`, sur Linux x86_64, Python 3.11+ :

```sh
bash scripts/install_local.sh

# Terminal 1 : serveur local du modèle
OLLAMA_MODELS=/workspace/models/ollama \
OLLAMA_HOST=127.0.0.1:11434 \
OLLAMA_NUM_PARALLEL=1 \
OLLAMA_MAX_LOADED_MODELS=1 \
/workspace/tools/ollama/bin/ollama serve

# Terminal 2 : téléchargement initial, puis tableau de bord
/workspace/tools/ollama/bin/ollama pull qwen2.5:1.5b
TRAD_AI_PROVIDER=ollama TRAD_LOCAL_MODEL=qwen2.5:1.5b python -m trading.web --port 8080

# Ou : analyse ponctuelle en ligne de commande
TRAD_AI_PROVIDER=ollama python -m trading.connected --analyze
```

Le script épingle Ollama 0.6.8, vérifie le SHA256 officiel de son archive et exclut les bibliothèques GPU. Un binaire déjà installé est conservé. Ollama vérifie les empreintes de ses fichiers de modèle pendant le téléchargement. Aucun contrôle TLS ou checksum n’est désactivé.

Ollama crée ses fichiers de fonctionnement dans `~/.ollama` ; ce répertoire doit être accessible en écriture. Les poids sont dans `/workspace/models/ollama`. Le dossier `/workspace/tools/ollama` contient le moteur. Ces fichiers persistent sur le disque préparé ; les deux serveurs doivent être relancés après restauration de l’environnement.

Le moteur écoute seulement sur `127.0.0.1:11434`, le tableau de bord sur `127.0.0.1:8080`. Les requêtes de Trad vers Ollama passent directement par l’interface locale, sans proxy réseau. Aucune clé Alpaca ou Hugging Face n’est transmise au modèle. Le téléchargement initial exige l’accès HTTPS au registre `registry.ollama.ai` et à son stockage d’artefacts ; une fois les poids présents, l’inférence ne dépend plus de ces destinations.

## Fonctionnement et limites

`TRAD_AI_PROVIDER` vaut `ollama` par défaut et `TRAD_LOCAL_MODEL` vaut `qwen2.5:1.5b`. Pour revenir explicitement à Hugging Face, utiliser `TRAD_AI_PROVIDER=huggingface` et les paramètres décrits dans CONNECTED.md.

Les trois rôles partagent le même petit modèle, appelé successivement. Le JSON Schema est transmis à Ollama pour contraindre le décodage. Chaque réponse est ensuite revalidée par Python : champs, sources, symboles et cohérence de la chaîne. Il n’y a aucun accès aux outils ni exécution d’ordre. Chaque rôle dispose de 30 secondes et de 512 tokens de sortie, avec un contexte de 4 096 tokens et quatre threads CPU. Un dépassement, une réponse tronquée ou incohérente provoque une abstention. La fraîcheur des données reste vérifiée après les appels.

Ce modèle est moins capable qu’un grand modèle distant : le respect du JSON ne prouve pas la qualité du raisonnement ou des conclusions financières. Une analyse qui s’abstient est un résultat possible et normal. Le premier appel peut être plus lent ; le modèle reste chargé pendant 30 minutes après usage.

Le tableau de bord vérifie que le serveur Ollama répond et que le modèle demandé est installé avant d’activer le bouton d’analyse. Un serveur sans poids n’est pas considéré comme prêt.

## Validation à cette étape

- Ollama installé depuis une archive vérifiée et serveur local démarré.
- 95 tests réussis, dont quatre contrôles du fournisseur local, de ses limites et de sa séparation avec les credentials distants.
- Vérification du 16 octobre 2026 (heure de Toronto) : le registre répond HTTP 200. Le téléchargement des poids redirige vers `dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com`, qui reste refusé par le proxy. Le domaine de stockage déjà configuré est différent. Ajouter cette destination dans la configuration réseau et appliquer le changement avant de relancer `ollama pull`.
- Aucune véritable inférence locale n’a donc encore été validée. Après téléchargement, vérifier une analyse complète sur des données récentes ; un code d’abstention lié au fournisseur n’établit pas le fonctionnement du modèle.
