# Bot Gold : expérience démo avec apprentissage statistique

Version du 29 septembre 2026. Le code fonctionne avec le connecteur MT5 du projet.
La nouvelle stratégie **n'a pas de rentabilité démontrée** : aucun export Axi ni terminal
Windows n'était accessible pendant cette implémentation. Les tests sur broker factice
vérifient le logiciel, pas sa capacité à gagner de l'argent.

## Démarrer sur ton PC Windows

MT5 doit être ouvert sur ton **compte démo hedging**, avec Algo Trading activé.
Ton fichier `.env` et tes données restent dans ton dossier local ; ne les publie pas.
Arrête les autres bots pendant la première expérience pour interpréter les résultats.

Depuis PowerShell, dans ton dépôt existant :

```powershell
cd "$HOME\Trading"
git fetch origin codex/gold-demo-learning:refs/remotes/origin/codex/gold-demo-learning
git switch codex/gold-demo-learning
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ml]"
.\.venv\Scripts\python.exe scripts\run_learning_demo.py --minutes 20
```

Si Git signale des modifications locales, conserve-les : ne fais pas de `reset --hard`.
Le dernier appel **envoie des ordres sur la démo** si un signal apparaît. Il affiche
les entrées, les sorties et les résultats nets. Au bout de 20 minutes, il arrête les
entrées, demande la clôture des positions du bot et affiche deux bilans. Si la connexion
ou le marché empêche une clôture, il le dit et renvoie un code d'échec ; les stops
restent côté serveur. Zéro trade reste possible en l'absence de signal.

Autres commandes :

```powershell
# Test mécanique : ouvre au lot minimal puis ferme ; résultat exclu de la stratégie.
.\.venv\Scripts\python.exe scripts\run_learning_demo.py --verification

# Calcul local uniquement ; aucun ordre, même si des positions existent déjà sur MT5.
.\.venv\Scripts\python.exe scripts\run_learning_demo.py --simulation --minutes 20

# Relire les bilans de la démo (MT5 ouvert).
.\.venv\Scripts\python.exe scripts\run_learning_demo.py --bilan

# Continuer la collecte sans minuterie ; Ctrl+C ferme les positions de cette expérience.
.\.venv\Scripts\python.exe scripts\run_learning_demo.py
```

## Ce que fait la stratégie

1. Construit des bougies de 5 secondes au prix médian bid/ask.
2. Identifie une impulsion dans le sens EMA20/EMA50 des bougies M1 clôturées : son
   corps dépasse 2 fois la taille médiane des bougies précédentes, avec un plancher de spread.
3. Attend un repli de 20 à 70 % de l'impulsion, puis une clôture qui dépasse la
   bougie précédente dans le sens initial. Une impulsion ne produit qu'un signal.
4. Prépare un stop derrière la structure récente, un objectif de 1,2 fois la distance
   au stop et une sortie après 120 secondes, y compris en perte.
5. Refuse une entrée si l'objectif est trop petit face au spread, aux commissions
   configurées et au glissement estimé, si le tick est périmé ou si le lot minimum dépasse le budget.

Le profil `config/learning_demo.yaml` autorise **jusqu'à 5 entrées sur 60 secondes
glissantes**, espacées d'au moins 12 secondes, et 5 positions simultanées. Ce sont
des plafonds, pas une obligation d'ouvrir une position à chaque bougie. Il n'y a pas
de restriction à Londres ou New York : les conditions peuvent être évaluées pendant
toutes les heures de cotation configurées.

Le budget prévu est 0,1 % par entrée et 0,5 % cumulé. À −1 % sur la journée,
les nouvelles entrées sont bloquées ; les positions conservent leur stop et leur
sortie temporelle. Un gap peut dépasser le montant prévu au stop. Pas de martingale,
de doublement après perte ni d'attente indéfinie pour afficher seulement des clôtures vertes.

Le profil possède son magic `20260930` et sa base `data/learning_demo.sqlite`.
Un verrou commun empêche le lancement simultané des deux expériences de scalping.
Les positions manuelles ou d'un autre magic sont ignorées. Le bot refuse les comptes
réels, y compris si le compte change pendant la boucle. Les références de journée
et de cadence sont conservées au redémarrage.

Le plafond de 12 secondes est un **réglage expérimental demandé par l'utilisateur**,
pas une règle Axi ni une affirmation que cette fréquence est acceptée. La page Axi
autorise certaines stratégies de court terme mais exclut le HFT sans donner un
seuil universel de requêtes par minute. Ne pas modifier `cadence_verified` pour
prétendre obtenir une confirmation. Les clôtures et reprises techniques ne sont pas
comptées comme de nouvelles entrées ; la cadence de requêtes n'est donc pas celle des entrées.

## L'apprentissage, concrètement

À chaque signal avec un plan exploitable, le bot enregistre 11 variables connues à
l'instant de la décision : sens, coût et spread rapportés au stop, corps de bougie,
amplitude récente, déplacement, régularité du mouvement, position dans le range,
activité des ticks et heure serveur sous forme cyclique.

Le modèle est une régression logistique avec normalisation, puis calibration sur
une période différente. Il estime si **le résultat net du trade** sera positif ;
il ne lit pas l'avenir, ne comprend pas tous les documents et n'appelle aucun LLM
pour envoyer des ordres. La première expérience tourne avec les règles seules.

Deux sources d'apprentissage explicites :

```powershell
# Après au moins 300 trades clôturés de collecte ; les résultats sont simulés avec coût stressé.
.\.venv\Scripts\python.exe scripts\train_scalp.py

# Ou à partir de TES exports Axi déjà présents sur le PC : manifest.json, M1 et ticks.
.\.venv\Scripts\python.exe scripts\train_scalp.py --ticks data\export --output data\models\pullback_ticks_v1.json
```

Le rejeu des ticks utilise un compte hypothétique de 5 000 USD pour le dimensionnement ;
les résultats en R normalisent le gain par la distance initiale au stop. Cela ne constitue
pas une reconstitution exacte d'un compte EUR. La collecte du terminal conserve, elle,
les exécutions démo dans la devise du compte et une simulation distincte.

L'apprentissage utilise exclusivement les résultats simulés stressés d'un même profil,
ou le rejeu des ticks ; les deux sources ne sont pas mélangées silencieusement. Les positions
encore ouvertes ou interrompues ne fournissent pas de label. Une collecte interrompue
peut perdre des résultats : ce biais doit être pris en compte avant d'interpréter le modèle.

Découpage dans l'ordre du temps : 60 % apprentissage, 20 % calibration, 20 % test.
Les trades dont le résultat chevauche la période suivante sont purgés, avec un embargo
égal à la durée maximale d'un trade. Le seuil de sélection est fixé à 0,60 avant le test.
Les indicateurs ne sont jamais normalisés à partir de la période de test.

Le rapport JSON compare le même ensemble d'occasions avec et sans filtre, affiche
le résultat net en R, le profit factor et l'erreur des scores. Un filtre n'est déclaré
admissible **à un essai démo** que s'il conserve au moins 30 trades sur un test couvrant
5 journées, améliore le total net de la base et reste positif. Ce critère exploratoire
ne prouve pas un avantage statistiquement significatif. Plusieurs essais sur la même
période rendent cette période moins probante ; poursuivre ensuite sur des données nouvelles.

Un modèle existant n'est jamais écrasé. Pour un nouvel essai, choisis un autre `--output`.
Il n'y a ni réentraînement automatique pendant les ordres ni activation automatique.

```powershell
# Charger les scores en observation. D'abord terminer / fermer l'expérience précédente.
.\.venv\Scripts\python.exe scripts\run_learning_demo.py --model data\models\pullback.json --nouvelle-experience --minutes 20

# Appliquer le filtre seulement si son rapport l'autorise pour l'expérience démo.
.\.venv\Scripts\python.exe scripts\run_learning_demo.py --model data\models\pullback.json --filter-model --nouvelle-experience --minutes 20
```

Le JSON est chargé sans pickle et vérifié contre les paramètres et coûts de la stratégie.
Changer de modèle ou de réglages archive l'ancienne expérience uniquement avec
`--nouvelle-experience`, et uniquement quand ses positions sont fermées. Ne mélange pas
les résultats avant et après filtrage comme s'il s'agissait d'un seul test indépendant.

## Lire les résultats et les limites

- **Bilan 1** : gains ET pertes exécutés en démo, frais des deals compris, positions
  ouvertes et résultat latent. L'equity affichée est celle du compte entier.
- **Bilan 2** : mêmes signaux acceptés, simulation avec glissement supplémentaire
  à l'entrée, au stop ou à la sortie temporelle. Ce scénario n'émule pas toute la liquidité réelle.
- Une vérification mécanique réussie signifie « les ordres fonctionnent », pas
  « la stratégie est rentable ». Une journée verte n'établit pas la rentabilité durable.
- Pas encore de flux d'actualités, de calendrier macro automatique ni de compréhension
  documentaire. Un modèle de prix ne remplace pas ces éléments. Aucun de ces éléments
  n'est annoncé actif dans cette version.
- Le spread est porté par les prix bid/ask ; il n'est pas soustrait une seconde fois
  au résultat. Vérifie la commission configurée selon ton compte (Standard/Pro).
- Axi indique que la démo ne simule pas le slippage réel : comparer les deux bilans
  est nécessaire, mais ne suffit pas à valider une exécution réelle.

## Sources primaires consultées

- MetaQuotes, interface Python MT5 : https://www.mql5.com/en/docs/python_metatrader5
- MetaQuotes, exécution / retcodes : https://www.mql5.com/en/docs/trading/ordersend
- MetaQuotes, propriétés des deals : https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties
- Scikit-learn, régression logistique : https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html
- Scikit-learn, séparation temporelle et gap : https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html
- Axi, stratégies autorisées : https://help.axi.com/fr-FR/axiv2--axicorp-prod/article/30gUWO-L-quelles-stratgies-de-trading-sont-autorises-
- Axi, limites de la démo : https://help.axi.com/en-GB/axiv2--axicorp-prod/article/-89ecQbt-does-my-demo-account-simulate-real-market-liquidity-and-slippage

Le découpage utilise une purge temporelle adaptée à la durée des trades, plutôt qu'un
simple nombre fixe de lignes `TimeSeriesSplit`, car les signaux ne sont pas régulièrement espacés.
