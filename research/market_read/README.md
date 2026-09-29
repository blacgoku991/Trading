# Lecture du marché : recherche du 29/09/2026 (nuit)

Question : une lecture des bougies M1 (réaction aux niveaux, structure, élan, volume, retour à la moyenne) donne-t-elle
un sens achat / vente qui bat les coûts à quelques minutes ? Cinq familles de modèles conçues et vérifiées
(causalité, mémoire finie, symétrie) AVANT toute mesure, puis évaluées en une fois (`evaluate.py`, 48 essais sur
2019-07 → 2023), les 3 premiers validés une seule fois (2024 → 09/2025). Résultat : aucun ne bat les coûts
(`docs/STRATEGIES.md`, « Lecture du marché »). `idea_user.py` : l'idée « deux bougies » de l'utilisateur.

Lancer depuis la racine du dépôt (données exportées dans `data/export`) :

    python research/market_read/evaluate.py               # période de développement
    python research/market_read/evaluate.py --validation  # modèles de retenus.txt, une seule fois
