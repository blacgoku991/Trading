# STRATEGIES.md — Stratégies testées

Chaque stratégie : règles 100 % codées, 3 à 5 paramètres libres, même code en backtest et en live
(`src/goldbot/strategies/`). Aucune n'est présumée rentable : tout se prouve sur les données Axi, frais compris.

## Méthode commune

- **Données** : barres M1 Axi du 22/07/2019 au 28/09/2026 (`docs/RESEARCH.md` annexe E).
- **Hors échantillon** : du 01/10/2025 au 28/09/2026, mis de côté. Il ne sert qu'une fois, pour la validation
  finale (`run_backtest.py --hors-echantillon`). Tout le développement se fait sur la période d'étude, du 22/07/2019
  au 30/09/2025.
- **Coûts** : spread de chaque barre (le plus petit de la minute), avec un plancher de 15 points ; 5 points de
  glissement défavorable par exécution au marché ou au SL ; swaps réels du compte (−61,6 / +40,5 points par lot et par
  nuit, triple le mercredi) ; pas de commission (compte Standard). Stress prévu : spread × 1,5 et glissement doublé.
- **Exécution** : signal à la clôture d'une barre, exécution à la barre suivante ; achat à l'ask, vente au bid ;
  SL et TP dans la même barre : SL.
- **Risque** : 0,5 % de l'equity par trade, arrondi vers le bas ; limites du CLAUDE.md §3.5 actives (perte
  journalière, positions, trades par jour, pertes d'affilée). En « mode recherche », l'arrêt total à −10 % est noté
  sans être appliqué, pour mesurer la stratégie sur tout l'historique.
- **Journal des essais** : chaque lancement de `run_backtest.py` ajoute une ligne à `reports/essais.csv`
  (paramètres et résultats), pour garder la trace de toutes les variantes testées.
- **Contrainte du petit compte** : avec 5 000 € et 0,5 % de risque, le lot minimal (0,01 lot = 1 once) limite la
  distance du SL à environ 28 $. Les signaux au SL plus large sont ignorés, jamais grossis.

## S1 — Cassure du range asiatique

**Idée** : la nuit européenne (session asiatique) est calme ; à l'ouverture de Londres, la liquidité arrive et le prix
sort souvent du range de la nuit.

**Règles** (heures de Londres) :
- range = plus haut et plus bas de 00:00 à 07:00 ; au moins la moitié des minutes doit avoir une barre ;
- filtre : taille du range entre `min_range_atr` et `max_range_atr` fois l'ATR journalier (14 jours précédents) ;
- à 07:00 : achat stop au plus haut + `buffer_atr` × ATR, vente stop au plus bas − `buffer_atr` × ATR ;
  un trade maximum par côté et par jour ;
- SL de l'autre côté du range ; TP à `tp_r` fois le risque ;
- ordres non déclenchés annulés à 11:00 ; positions fermées à 20:00.

**Paramètres libres** : `buffer_atr` = 0,05 ; `tp_r` = 1,5 ; `min_range_atr` = 0,15 ; `max_range_atr` = 0,8.
Valeurs choisies a priori, sans optimisation.

**Résultat sur la période d'étude** (22/07/2019 → 30/09/2025, mode recherche) :

| Trades | Profit factor | Espérance | Gagnants | Rendement | Drawdown max |
|---|---|---|---|---|---|
| 1 080 | 0,98 | −0,014 R | 42,9 % | −4,3 % | −12,8 % |

- Aucun avantage après frais : résultat proche de zéro sur 6 ans, années alternativement positives et négatives.
- Le compte réel aurait été arrêté le 27/01/2020 (drawdown de 10 %).
- **Verdict : rejetée en l'état.** Ne pas l'optimiser à l'aveugle : chercher d'abord une raison de marché
  (filtre de régime, horaires) sur la période d'étude, puis valider sans toucher au hors échantillon.
