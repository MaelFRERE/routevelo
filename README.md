# RouteVelo V16

Planificateur d'itinéraires pour vélo de route avec itinéraires alternatifs, boucles, export GPX, eau potable, cimetières et boulangeries.

## Utilisation locale

- Windows : double-cliquer sur `start.bat`.
- macOS/Linux : lancer `./start.sh`.
- En local, aucun mot de passe n'est demandé tant que la variable `APP_PASSWORD` n'est pas définie.

## V11 : mobile-first

Sur téléphone, la carte occupe tout l'écran. Le bouton **☰ Itinéraire** ouvre les réglages dans un panneau latéral. Le résumé, les filtres et le bouton GPX ont été compactés pour laisser davantage de place à la carte.

## Hébergement gratuit sur Render

1. Créer un dépôt GitHub et y envoyer tous les fichiers de ce dossier.
2. Dans Render, créer un service à partir du dépôt et utiliser le fichier `render.yaml` (Blueprint).
3. Quand Render demande la variable `APP_PASSWORD`, saisir le mot de passe souhaité. Ne pas écrire ce mot de passe dans GitHub.
4. `AUTH_SECRET` est généré automatiquement par Render.
5. Déployer. Le site sera accessible avec l'adresse HTTPS fournie par Render.

Le serveur utilise automatiquement la variable `PORT` fournie par Render et écoute sur `0.0.0.0` en hébergement. En local, il conserve le démarrage automatique sur `127.0.0.1:8080` avec recherche d'un port libre si nécessaire.

## Accès privé

Quand `APP_PASSWORD` est défini, toutes les pages et toutes les API RouteVelo sont protégées. Après connexion, une session signée est conservée dans un cookie `HttpOnly` pendant 30 jours. Le mot de passe n'est pas envoyé dans le HTML de l'application.

## Sources de données

- Routage : Valhalla public / OpenStreetMap.
- Géocodage : Nominatim.
- Eau potable : base spécialisée OpenDataSoft/Huwise avec repli OpenStreetMap.
- Cimetières : OpenStreetMap/Overpass, recherche optimisée par petites zones.
- Boulangeries : API Recherche d'entreprises / SIRENE.

Les POI sont filtrés à 400 m maximum du tracé. Les cimetières sont indiqués comme **eau potentielle à vérifier**, et non comme eau potable confirmée.


## V12 — mobile

- Mode visible simplifié : Départ → arrivée uniquement.
- Les trois propositions d’itinéraire sont affichées simultanément sur mobile.
- Filtres déplacés en haut à droite sur mobile.
- Bouton flottant ↶ pour annuler la dernière modification d’un point.


## V13 mobile
- Les 3 propositions restent dans le panneau de résumé et ne chevauchent plus le GPX.
- Les filtres démarrent repliés.
- Le panneau Itinéraire démarre ouvert sur mobile.
- Le bouton ↶ est placé sous les contrôles + / − de la carte.


## V15
- Départ → arrivée : saisir directement les villes/adresses puis cliquer sur « Calculer 3 itinéraires » ; plus de bouton Placer ni de création de point par clic sur la carte.
- Mode Boucle rétabli.
- Boucle : une seule boucle générée pour la distance cible, sans choix Court/Moyen/Long.


## V16
- Le message de contrôle strict à côté des statistiques a été retiré de l’interface.
- Le contrôle de sécurité des surfaces reste actif en arrière-plan et le GPX reste bloqué si nécessaire.
