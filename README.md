# RouteVelo V33

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


## V24 - génération de boucles rétablie

- Le filtre anti-aller-retour n'empêche plus toute la génération.
- Jusqu'à 12 variantes sont testées au lieu de 24 pour accélérer l'affichage.
- Les boucles sont classées par qualité : excellente, très bonne, puis meilleur secours disponible.
- Les faux positifs sur des routes parallèles sont réduits avec une détection plus précise.
- Les longs aller-retours restent fortement pénalisés, mais l'application affiche toujours les meilleures boucles disponibles.

## V24 — distance des boucles mieux respectée

- La géométrie initiale des boucles est mieux calibrée pour le réseau routier réel.
- Après le premier calcul, RouteVelo compare la distance obtenue à la distance cible.
- Si l’écart dépasse environ 3 %, les points de passage sont automatiquement rapprochés ou éloignés puis la boucle est recalculée une fois.
- La sélection finale privilégie les boucles à environ ±4 % de la distance demandée, tout en conservant l’optimisation anti aller-retour.

## V25 - Performance + nouvelle direction artistique

- Palette rose/violet sobre, sans police externe ni effet visuel lourd.
- Tracés simplifiés uniquement pour l'affichage (le GPX et les calculs gardent la géométrie complète).
- Rendu Leaflet Canvas et animations carte allégées.
- Cache client des POI par itinéraire et rendu progressif par type de POI.
- Cache serveur des géocodages et réponses Valhalla identiques.
- Compression gzip des réponses et revalidation ETag des fichiers statiques.
- Génération de boucles avec arrêt anticipé quand trois bonnes propositions sont déjà disponibles.

## V26 - Profil d'elevation

- suppression du bouton visible de retour en arriere sur la carte ;
- ajout d'un profil d'elevation leger sous le bouton GPX ;
- courbe altitude/distance dessinee en SVG sans bibliotheque graphique supplementaire ;
- affichage des altitudes min/max et de la distance du profil ;
- le profil se met a jour automatiquement quand l'itineraire selectionne change ;
- interface mobile adaptee avec un graphique plus compact.

## V27 - Boucles strictes, points manuels et type de voie

- Les boucles contenant une vraie portion en aller-retour sont rejetées, y compris près du départ au-delà d'une très courte marge technique.
- Le générateur teste davantage d'orientations, mais évite une seconde calibration inutile quand une première boucle contient déjà un aller-retour.
- Après avoir sélectionné un itinéraire, un clic sur la carte ajoute un point de passage à l'endroit logique du trajet et recalcule le tracé.
- Un bouton **↶ Annuler le point** est placé sous les filtres pour revenir sur la dernière modification de point.
- Sous le profil d'élévation, un bloc **Type de route** affiche en pourcentage la part de route, piste cyclable, voie de service et autres types de voie à partir du contrôle Valhalla.

## V28 — retour au point de départ

- En mode **Départ → arrivée**, une case **Retour au point de départ** permet de calculer un parcours complet Départ → Arrivée → Départ.
- L'application cherche **3 propositions complètes** et privilégie un retour différent de l'aller.
- La distance, le temps, le D+/D−, le profil d'élévation, les types de route, les POI et le GPX correspondent au parcours total aller + retour.
- Les points ajoutés sur la carte restent modifiables ; le retour au départ reste implicite lors des recalculs.


## V29 — résumé mobile + suppression des petits décrochements

- Sur mobile, le panneau du bas montre d’abord les 3 propositions puis Distance / Temps / D+ / D−. Il se fait défiler pour accéder au GPX, au profil d’élévation et au type de route.
- Tous les itinéraires passent désormais par un nettoyage géométrique qui détecte les petites excroissances revenant presque au même point et les coupe automatiquement.
- Le nettoyage s’applique aux trajets A → B, A → B → A, aux boucles et aux tracés recalculés après ajout d’un point.
- La fermeture normale d’une vraie boucle n’est pas supprimée.

## V30 — export GPX toujours disponible

- Le bouton GPX est activé dès qu'un itinéraire est réellement affiché.
- Le contrôle strict du revêtement reste exécuté pour l'analyse et le type de route.
- Un segment inconnu, un avertissement de surface ou une indisponibilité temporaire du contrôle ne bloque plus l'export.
- Le GPX conserve le tracé actuellement sélectionné, ainsi que les POI activés.


## V31 : profil interactif et points de passage plus lisibles

- Le profil d’élévation est interactif : glisser la ligne verticale affiche la distance, l’altitude et la pente locale en %.
- Un point rouge synchronisé apparaît sur la carte à l’endroit correspondant du parcours.
- Les points de passage grossissent et affichent un halo rose pendant leur déplacement, puis une courte animation confirme le dépôt.


## V32 : page déroulante + plan nutritionnel

- La carte reste en haut et le résumé superposé ne contient plus que les 3 propositions et Distance / Temps / D+ / D−.
- Sous la carte, la page se déroule normalement vers le GPX, le profil d’élévation et le type de route.
- Ajout d’un plan nutritionnel calculé à partir de la durée et de la distance du parcours sélectionné.
- L’utilisateur choisit ses aliments (bonbons, gels, pâtes de fruit, barres, compotes, banane, boisson énergétique ou aliment personnalisé) et peut modifier les glucides par portion.
- Les objectifs de glucides par heure et d’eau par heure sont réglables et sauvegardés localement dans le navigateur.
- Le plan répartit les prises sur la sortie avec l’heure de passage, le kilomètre, le pourcentage du parcours, les glucides et l’eau.


## V33 — plan nutritionnel en fenêtre
- Le plan nutritionnel n'occupe plus la page : un bouton dédié ouvre une fenêtre.
- Fenêtre centrée sur ordinateur et bottom sheet plein largeur sur mobile.
- Fermeture par X, fond assombri ou touche Échap.
- Bouton de génération optimisé pour le tactile et conservé visible pendant le défilement.
- Réglages, aliments personnalisés, sauvegarde locale et génération du plan sont conservés.
