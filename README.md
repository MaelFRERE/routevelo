# RouteVelo V22

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
- Points d’eau confirmés : Huwise/OpenDataSoft (données OSM).
- Points d’eau potentiels et cimetières : IGN BD TOPO via le géocodeur Géoplateforme, avec secours OpenStreetMap/Overpass.
- Boulangeries : fusion Huwise/OpenStreetMap + Annuaire des Entreprises / SIRENE.

Les POI sont filtrés à **400 m maximum du tracé réel**. Une fontaine, un lavoir ou un cimetière provenant de la BD TOPO est indiqué comme **eau potentielle à vérifier** : l’application ne prétend pas que cette eau est potable.


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


## V17 - fiabilite des points de ravitaillement

- Boulangeries : fusion Annuaire des Entreprises / SIRENE + OpenStreetMap.
- Eau potable : fusion base nationale Huwise/OpenDataSoft + OpenStreetMap.
- Si une source externe echoue, l autre peut toujours fournir des resultats.
- Le filtre final reste limite a 400 m maximum du trace reel.


## V18 - correction Render des POI

- Remplacement de l'ancienne API Huwise/OpenDataSoft v1 par l'Explore API v2.1 actuelle.
- Eau potable : requêtes géographiques v2.1 le long du tracé, sans clé API.
- Boulangeries : dataset national Huwise `osm-france-shop-craft-office` filtré sur le type `bakery`, avec secours SIRENE/OSM.
- Cimetières : requête Overpass allégée avec `nwr` et ajout d'une troisième instance publique de secours.
- Nouvelles clés de cache pour éviter de réutiliser d'anciens résultats vides.
- Le filtrage final reste strict à 400 m du tracé.


## V19 - couverture continue et fusion réelle des sources

- Correction du filtre à 400 m : la distance est désormais calculée jusqu’aux **segments du tracé**, et non seulement jusqu’à quelques sommets de la ligne.
- Boulangeries : Huwise et SIRENE sont toujours fusionnés. Les codes NAF `10.71C` et `47.24Z` sont interrogés séparément pour éviter qu’un filtre multi-valeurs soit mal interprété.
- Eau : fusion des points potables OSM/Huwise avec les fontaines, points d’eau, sources captées et lavoirs de la BD TOPO.
- Cimetières : IGN BD TOPO devient la source principale ; Overpass n’est plus que le secours.
- Les recherches IGN utilisent des cercles chevauchants tout le long du parcours afin de ne plus laisser de trous entre deux zones de recherche.
- Cache POI versionné à nouveau pour ne pas réutiliser les résultats incomplets des versions précédentes.


## V20 - 3 boucles cliquables avant les POI

- Le mode Boucle génère jusqu’à 3 boucles distinctes autour de la distance cible.
- Les 3 tracés sont affichés simultanément sur la carte avec des repères 1, 2 et 3.
- Cliquer sur un tracé, son repère ou sa carte de choix sélectionne la boucle.
- Eau, cimetières, boulangeries, contrôle du revêtement et dénivelé ne sont chargés qu’après cette sélection.
- Les 3 propositions Départ → arrivée restent inchangées.


## V21 - boucles sans longs aller-retours

- Le générateur de boucles produit davantage de candidats et mesure les portions de route empruntées plusieurs fois.
- Les reprises proches du point de départ/arrivée restent autorisées sur une courte distance.
- Les longs allers-retours au milieu du parcours sont fortement pénalisés et écartés lorsqu’une meilleure boucle existe.
- Les formes de boucle varient entre quadrilatère, pentagone et hexagone afin de favoriser des branches aller/retour différentes.
- Les 3 boucles sont toujours affichées avant le chargement des points d’eau et boulangeries.


## V22 - boucles sans aller-retour au milieu

- Filtre dur des reprises du meme axe hors de la zone depart/arrivee.
- Jusqu a 24 orientations/formes testees pour trouver des boucles propres.
- Les boucles avec plus de 120 m consecutifs ou 200 m cumules de route reprise hors du depart sont rejetees.
- Si moins de 3 boucles propres existent, RouteVelo en affiche moins plutot que de proposer une mauvaise boucle.
- Polygones de 5 a 8 sommets avec formes irregulieres pour mieux separer les branches.
