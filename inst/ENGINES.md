# `generate_Mosa()` : chaînes Metashape et OpenDroneMap — convergences et différences

canObsR 0.0.0.9012 — ODM 3.6.2 (OpenSfM, OpenMVS, mvs-texturing), API Python de Metashape 2.x.

Ce document décrit, étape par étape, ce que fait chacune des deux chaînes de `generate_Mosa()`
(`engine = "metashape"` et `engine = "odm"`), ce qui est identique, ce qui a été aligné, et ce qui reste
différent parce que les deux logiciels n'utilisent pas les mêmes algorithmes. Les affirmations sur ODM
viennent de la lecture du code d'ODM 3.6.2 et d'OpenSfM, et des journaux d'exécution sur les données de test
(Bouamir, 3 dates, 133 images DJI 5472 × 3648 px). Celles sur Metashape viennent du script
`inst/PYTHON/TimeSIFT_scripts_auto.py`, des journaux de Metashape et de sa documentation. Ce qui n'a pas pu être
vérifié est signalé « à vérifier ».

## 1. Principe commun : Time-SIFT

Les deux chaînes appliquent la méthode Time-SIFT (Feurer & Vinatier 2018,
https://doi.org/10.1016/j.isprsjprs.2018.10.016) :

1. toutes les images de toutes les dates sont alignées ensemble, en un seul bloc ;
2. le bloc est ensuite séparé par date : chaque date garde ses caméras, avec leurs positions et orientations
   issues de l'alignement commun ;
3. pour chaque date, la reconstruction dense, le modèle de surface et l'orthomosaïque sont calculés avec les
   seules images de cette date.

Les mosaïques des différentes dates sont ainsi co-registrées par construction, sans points d'appui.

## 2. Vue d'ensemble

Statut : **identique** (même traitement), **aligné** (réglé en 0.0.0.9012 pour reproduire Metashape),
**différent** (algorithmes propres à chaque logiciel), **à vérifier**.

| Étape | Metashape | ODM | Statut |
|---|---|---|---|
| Sous-dossiers traités | ceux avec une date AAAAMMJJ ou AAAAMM dans le nom | idem (même fonction R) | identique |
| Date d'une image | le sous-dossier, jamais l'EXIF | idem | identique |
| Résolution pour la détection des points | images d'origine (5472 px) | 4480 px au plus (limite fixée dans ODM) | différent |
| Points détectés par image | 100 000 au plus | 10 000 exactement par défaut (100 000 possible) | différent, choisi (voir 4) |
| Points de liaison gardés par image | 10 000 au plus | pas de limite | différent |
| Choix des paires d'images | GPS + similarité des images | GPS (triangulation des positions) | différent |
| Images non alignées | réappariées avec 2 × plus de points, jusqu'à stabilité | pas de seconde passe | différent |
| Modèle de caméra | f, cx, cy, k1, k2, k3, p1, p2 | fx, fy, cx, cy, k1, k2, k3, p1, p2 (Brown) | quasi identique |
| Précision des positions GPS | 10 m | 10 m (avant : 3 m) | aligné |
| Séparation par date | copie du chunk aligné | copie du projet aligné, fichiers filtrés et contrôlés | identique sur le principe |
| Calibration des couleurs | gains par image et par canal + vignettage | idem, sur les mêmes points de liaison | aligné (vignettage Metashape à vérifier) |
| Cartes de profondeur | 1/2 résolution (2736 px), filtrage « Aggressive » | 1/2 résolution (2736 px), filtrage OpenMVS + filtre statistique | différent (algorithme) |
| Surface de projection | MNS à `resol_ref` (5 cm), interpolé | maillage 2.5D tiré d'un MNS à ~5 cm, simplifié à 200 000 sommets | **différent (principal)** |
| Assemblage des images | « mosaic » : basses fréquences mélangées, détails d'une seule image | choix d'une image par facette + égalisation des raccords | différent (algorithme) |
| Résolution de l'orthomosaïque | `resol_ref` | `resol_ref` (avant : plafonnée par ODM à 5,48 cm) | aligné |
| Emprise | toute la zone reconstruite | idem (avant : 3 m retirés sur les bords) | aligné |
| Système de coordonnées | rendu directement dans `crs` | rendu dans la zone UTM du site, gardé tel quel si c'est `crs` | aligné |
| Format de sortie | 3 bandes 8 bits, fond blanc (255), LZW | idem | identique |
| MNS (`out_dir_DEM`) | depuis le nuage dense, interpolé, `resol_ref` | depuis le nuage dense, trous comblés, `resol_ref` | différent (algorithme) |

## 3. Détail des étapes

### 3.1 Images et dates

Les deux chaînes reçoivent la même liste de sous-dossiers, calculée en R par `.select_subfolders()` : les
sous-dossiers de `path_in` dont le nom contient une date AAAAMMJJ ou AAAAMM (tous avec `group_by_flight = TRUE`).
Ceux dont le nom commence par « . » ou « _ », ainsi que `time-sift-block` et `timesift_block`, sont toujours ignorés.
Les images sont cherchées dans toute l'arborescence de chaque sous-dossier (extensions jpg, jpeg, png, tif, tiff).

- **Metashape** : un chunk par sous-dossier ; chaque caméra est renommée `<dossier>_EPOCH_<image>`. Les chunks sont
  fusionnés en un chunk « TimeSIFT ». Après l'alignement, la date d'une caméra est lue dans ce nom.
- **ODM** : les images sont liées (liens physiques, sans copie) dans un projet `timesift_block`, renommées
  `<date>__<image>`. Une erreur est levée si un même fichier apparaît deux fois.
  La date de chaque image reste lisible dans tous les fichiers d'ODM.

Dans les deux cas, toutes les images d'un sous-dossier font une seule mosaïque, même si certaines ont été prises
un autre jour (EXIF) ; `check_flights()` signale ces cas sans bloquer.

### 3.2 Alignement du bloc multi-dates

**Détection des points d'intérêt.**
- Metashape : `matchPhotos(downscale = 1)`, soit les images à leur taille d'origine ; jusqu'à `keypoint_limit`
  = 100 000 points par image.
- ODM : DSP-SIFT (`--feature-type dspsift`) avec `--feature-quality ultra`. ODM réduit toujours les images à
  4480 px au plus pour cette étape (`opendm/osfm.py`, limite fixe) : les images de 5472 px sont réduites d'environ
  18 %. Le nombre de points extraits est exactement `--min-num-features` (OpenSfM le passe comme nombre cible au
  détecteur ; les journaux montrent exactement 10 000 points par image avec la valeur par défaut).
  canObsR passe `keypoint_limit` à `--min-num-features` : 10 000 par défaut pour ODM (100 000 pour Metashape).

**Appariement.**
- Metashape : présélection générique (images réduites) et par les positions GPS (`reference_preselection`), puis
  au plus `tiepoint_limit` = 10 000 points de liaison gardés par image. Les photos non alignées sont réappariées
  avec des limites doublées (200 000 / 20 000) : le script relance `matchPhotos()` puis `alignCameras()` en boucle,
  jusqu'à ce que le nombre de caméras alignées ne change plus.
- ODM / OpenSfM : appariement FLANN entre paires d'images choisies d'après les positions GPS (triangulation des
  positions, `matcher-neighbors 0`), filtrage géométrique robuste. Tous les appariements validés sont gardés.
  Il n'y a pas de seconde passe pour les images non alignées : ODM indique pour chaque date le nombre d'images
  alignées.

Ce point compte pour Time-SIFT : la qualité de la co-registration entre dates dépend des points de liaison
entre images de dates différentes. Avec 10 000 points par image, le journal d'ODM donne une médiane de
200 correspondances validées entre deux images de dates différentes (10 % des paires sous 50), contre 650 entre
deux images d'une même date.

**Ajustement de faisceaux et géoréférencement.**
- Metashape : `alignCameras()`, puis `optimizeCameras()` avec ses réglages par défaut (f, cx, cy, k1, k2, k3, p1,
  p2) ; les positions GPS des images servent avec une précision de 10 m (réglage par défaut de Metashape).
- ODM : reconstruction incrémentale OpenSfM ; modèle de caméra Brown (choisi automatiquement par ODM, vérifié
  dans le journal : « brown »), avec deux focales fx et fy au lieu d'une seule. Les positions GPS entrent
  dans l'ajustement ; leur précision était de 3 m par défaut dans ODM, elle est désormais fixée à 10 m
  (`--gps-accuracy 10`) comme dans Metashape.
- Dans les deux cas, une seule calibration de caméra est estimée pour toutes les dates quand le même drone est
  utilisé (Metashape regroupe les caméras identiques dans un même « sensor », OpenSfM dans une même caméra).
- Metashape limite ensuite la reconstruction à une boîte englobant les points de liaison (`resetRegion()`) ; ODM
  n'a pas de boîte.

### 3.3 Séparation par date

- **Metashape** : le chunk aligné est copié une fois par date (date lue dans le nom des caméras) et les caméras des
  autres dates sont supprimées de la copie. Positions, orientations, calibration et points de liaison sont ceux de
  l'alignement commun ; il n'y a pas de nouvel appariement.
- **ODM** : le projet aligné est copié une fois par date (copie légère, images liées) et tous les fichiers qui
  listent des images sont filtrés : reconstructions OpenSfM (avec et sans distorsion), `tracks.csv`, liste des
  caméras utilisée pour la texture (`reconstruction.nvm`), `images.json`. Ne restent que les points vus dans au
  moins 2 images de la date. ODM est relancé à partir de l'étape `openmvs`. Après chaque date, canObsR vérifie
  qu'aucune image d'une autre date ne figure dans les fichiers qui servent au nuage dense, à la texture et à
  l'orthomosaïque (erreur sinon ; voir aussi `check_odm_epochs()`).

Le résultat est le même : chaque date est reconstruite avec ses seules images, dans la géométrie de
l'alignement commun.

### 3.4 Calibration des couleurs (`calibrate_col`, `calibrate_col_by`)

**Metashape** : `calibrateColors(TiePointsData, white_balance = TRUE)`.
- `white_balance = TRUE` : chaque canal (R, G, B) est corrigé séparément ; sans cette option, un seul
  facteur par image serait appliqué aux trois canaux.
- La correction est relative entre images : elle rend cohérentes les couleurs des mêmes points vus dans
  plusieurs images, sans référence absolue de gris neutre.
- D'après la documentation communautaire de Metashape, `calibrateColors()` estime aussi le vignettage de la
  caméra (polynôme de degré 3 en x, y). La sortie console de canObsR 0.0.0.9012 affiche ce que Metashape a
  estimé pour le capteur (« Colour calibration … vignetting = … ») : **à vérifier** sur le prochain calcul.
- « chunk » : calibration sur le chunk TimeSIFT avant la séparation (toutes les dates ensemble) ;
  « date » : calibration de chaque chunk-date après la séparation.

**ODM** : il n'y a pas d'équivalent dans ODM. canObsR l'implémente dans `timesift_odm.py`, sur les mêmes
données que Metashape, c'est-à-dire les points de liaison du bloc multi-dates :
- Pour chaque observation d'un point de liaison, OpenSfM enregistre la couleur de l'image à l'endroit du point et
  sa position dans l'image (`tracks.csv`).
- Modèle, pour chaque canal : log(couleur observée) = log(gain de l'image) + log(couleur du point)
  + vignettage(x, y), où le vignettage est un polynôme de degré 3 en x, y, propre à la caméra, nul au centre
  de l'image.
- Ajustement par moindres carrés alternés. Sont exclues les observations sombres (< 5) ou saturées (> 250), et
  par deux fois les valeurs aberrantes (> 3,5 écarts-types robustes). Au-delà de 3 millions d'observations, un
  sous-ensemble aléatoire de points est utilisé.
- Normalisation : la moyenne des log-gains est nulle par canal (le niveau de couleur global ne change pas). Les
  gains sont bornés entre 0,5 et 2.
- Application : chaque image non distordue servant au nuage dense et à la texture est multipliée par son gain
  et par la correction de vignettage. Celle-ci est calculée à la position du pixel dans l'image d'origine, à
  partir des modèles de caméra d'OpenSfM.
- « chunk » : un seul ajustement pour toutes les images ; un point doit avoir la même couleur à toutes les dates.
  « date » : mêmes points et mêmes observations, mais un ajustement par date (un point peut changer de couleur
  d'une date à l'autre, le vignettage est estimé par date, la normalisation se fait par date). Les observations
  qui n'apportent aucune contrainte (point vu dans une seule image de la date) ne sont pas utilisées.
- Fichiers produits : `color_gains.csv` (gains par image) et `vignetting.csv` (coefficients et correction aux
  coins de l'image) dans `out_dir_project`.
- `calibrate_vignetting = FALSE` désactive le vignettage (gains seuls).
- Validation sur données simulées : gains retrouvés à 0,6 % près, vignettage à 0,6 % près ; une surface uniforme
  vue avec un vignettage de 30 % aux coins ressort plate à 0,5 % près.

Différences qui restent : l'algorithme interne de Metashape (pondérations, normalisation du vignettage, rejet
des valeurs aberrantes) n'est pas documenté. Les deux corrections visent le même résultat sur les mêmes points,
sans être identiques au pixel près.

### 3.5 Reconstruction dense

- **Metashape** : `buildDepthMaps(downscale = 2, filter_mode = Aggressive)`, soit des cartes de profondeur à la moitié
  de la résolution (2736 px de large), avec le filtrage le plus fort. Puis `buildPointCloud(point_colors = TRUE)`.
- **ODM** : OpenMVS `DensifyPointCloud` avec `--resolution-level 1` (moitié de la résolution, 2736 px,
  `--pc-quality ultra`), fusion à partir de 2 vues, sur GPU (CUDA). Attention : l'installeur Windows d'ODM place un
  faux `nvcuda.dll` (20 Ko) à côté d'OpenMVS (`C:/ODM/SuperBuild/install/bin`) ; Windows le charge à la place du
  pilote NVIDIA, OpenMVS échoue en GPU et ODM le relance sur le processeur sans le signaler. Une fois ce fichier
  renommé (`nvcuda.dll.odm_dummy`), OpenMVS utilise le GPU : 1,3 min contre 4,8 min sur processeur pour une date du
  jeu test (Quadro RTX 6000, pilote 537.42, `inst/validation/odm_gpu_test.R`). canObsR affiche un avertissement
  quand le faux fichier est présent. Puis filtrage d'OpenMVS
  (`--filter-point-cloud -20`) et filtre statistique d'ODM (16 voisins, 5 écarts-types, `--pc-filter 5`).
- `downscale_factor_depth_map` est converti pour donner la même résolution d'image : 1 et 2 → `ultra`, 4 → `high`,
  8 → `medium`, 16 → `low`. ODM n'a pas de niveau à pleine résolution.

Les deux logiciels calculent des cartes de profondeur multi-vues à la même résolution d'image, mais leurs
algorithmes de mise en correspondance et de filtrage diffèrent : les nuages denses ne sont pas identiques,
notamment sur les houppiers.

### 3.6 Surface sur laquelle les images sont projetées

C'est la principale différence de structure entre les deux chaînes.
- **Metashape** (par défaut) : `buildDem(PointCloudData, interpolation = Enabled, resolution = resol_ref)`, soit un
  MNS à 5 cm interpolé dans les trous ; l'orthomosaïque est projetée sur ce MNS.
- **ODM** : un MNS à environ `resol_ref` est calculé (valeur maximale des points, trous comblés, lissé), puis
  transformé en maillage 2.5D simplifié à `--mesh-size` sommets au plus (200 000 par défaut). Sur les 45 ha des
  données de test, cela fait de l'ordre d'un sommet tous les 1,5 m, adaptés au relief (plus serrés là où la
  surface varie). L'orthomosaïque est rendue à partir de ce maillage texturé.

Sur un couvert forestier, une surface plus grossière pourrait déplacer ou étaler les bords des houppiers dans
l'orthomosaïque. Testé le 24/11/2026 (projet à 10 000 points, ODM relancé à partir du maillage) : 1 et 4 millions
de sommets ne rapprochent pas ODM de Metashape (écart local 40,3 et 40,2 cm contre 38,5 cm à 200 000 sommets ;
co-registration entre dates 2,41 et 2,46 cm contre 2,34 cm), pour 68 et 196 min de calcul supplémentaire. La
valeur par défaut d'ODM (200 000 sommets) est donc gardée. Avec `generate_Mosa(odm_args = c("--mesh-size", "2000000"))`, seuls le maillage, la texture et
l'orthomosaïque sont recalculés sur un projet existant.

`from_mesh = TRUE` : Metashape construit un maillage 3D à partir du nuage dense et projette l'orthomosaïque
dessus ; ODM utilise alors son maillage 3D complet (`--use-3dmesh`). Dans les deux cas aucun MNS n'est produit.

### 3.7 Assemblage des images (couleur de chaque pixel)

- **Metashape** : `buildOrthomosaic(blending_mode = MosaicBlending, fill_holes = TRUE, ghosting_filter = FALSE,
  refine_seamlines = FALSE)`. Les basses fréquences sont mélangées entre les images qui se recouvrent, les
  détails viennent d'une seule image ; les trous sont comblés.
- **ODM** : mvs-texturing (`texrecon`). Pour chaque facette du maillage, une image est choisie : le critère
  favorise les vues nettes et proches (`-d gmi`), et le plus possible au nadir (`--nadir_mode`). Les vues
  aberrantes sont écartées (`gauss_clamping`). Les couleurs sont ensuite égalisées le long des raccords,
  globalement puis localement (« seam leveling », actif par défaut). Les petits trous de l'orthomosaïque sont
  comblés (`inpaintThreshold 1`).

Le principe est voisin (une image source par zone, transitions adoucies) mais les algorithmes diffèrent : on
peut attendre de petites différences de netteté et de position des raccords. L'égalisation des raccords d'ODM
agit en plus de la calibration des couleurs : c'est une autre source d'écart de rendu entre les deux chaînes,
indépendante de `calibrate_col`.

### 3.8 Orthomosaïque finale

- **Résolution** : `resol_ref` pour les deux. ODM plafonnait la résolution à son estimation du GSD moins 10 %
  (5,48 cm au lieu des 5 cm demandés sur les données de test), puis canObsR rééchantillonnait à 5 cm. ODM est
  désormais lancé avec `--ignore-gsd` et rend directement l'orthomosaïque à 5 cm. Cette option garde aussi les
  images non distordues à leur taille d'origine pour la texture, comme Metashape qui part des images d'origine.
- **Emprise** : ODM retirait par défaut 3 m sur le pourtour (`--crop 3`). Ce n'est plus le cas (`--crop 0`) : comme
  Metashape, ODM garde toute la zone reconstruite.
- **Système de coordonnées** :
  - Metashape travaille dans le système des positions GPS (WGS84 géographique). Jusqu'à la version 0.0.0.9011,
    le MNS et l'orthomosaïque étaient construits dans ce système puis rééchantillonnés vers `crs` à l'export.
    Depuis la version 0.0.0.9012, ils sont construits directement dans `crs` (`projection` de `buildDem()` et
    `buildOrthomosaic()`) : l'export ne rééchantillonne plus (**à vérifier** au premier calcul, ce réglage n'a pas
    pu être testé sans licence).
  - ODM rend ses produits dans la zone UTM (WGS84) du site. Quand c'est le `crs` demandé (cas des sites canobs :
    EPSG:32622, EPSG:32633…), canObsR garde les pixels tels que rendus par ODM, sans rééchantillonnage (leur
    taille, calculée par ODM, s'écarte de `resol_ref` de quelques 1e-5 : 0,049998 × 0,049999 m pour 0,05 ; de même,
    Metashape a écrit 0,05001625 m) ;
    sinon, il reprojette (bilinéaire) sur une grille calée sur les multiples de `resol_ref`.
- **Grille** : chaque logiciel fixe l'origine de sa grille ; les grilles de deux dates ou des deux chaînes ne sont
  pas forcément calées entre elles. `benchmark_engines.R` écrit les origines dans `grids_engines.csv`
  (**à vérifier**). `align_Mosa()` rééchantillonne de toute façon chaque date sur la grille de référence.
- **Format** : 3 bandes 8 bits (rouge, vert, bleu), fond blanc (255) hors de la zone reconstruite, compression
  LZW, nom `<site>_<date><suffix>.tif` dans les deux cas. Pour ODM, la bande alpha est retirée et sert à placer
  le fond blanc.

### 3.9 Modèle numérique de surface (`out_dir_DEM`)

- **Metashape** : le MNS de l'étape 3.6, exporté à `resol_ref`.
- **ODM** : `--dsm` à `resol_ref`, calculé à partir du nuage dense filtré (`--dem-gapfill-steps 3` pour combler
  les trous), puis mis au même format (1 bande, flottant, NA hors zone).

Dans les deux cas, les altitudes sont celles des positions GPS des images, sans changement de référence
verticale par canObsR.

### 3.10 Options propres à une seule chaîne

- `data_type = "MS"` et `sun_sensor` : Metashape seulement (ODM : erreur explicite).
- `tiepoint_limit` : Metashape seulement (pas d'équivalent dans ODM).
- `calibrate_vignetting`, `odm_path`, `odm_runner`, `odm_args`, `overwrite`, `check_dates`, `tempdir_custom` :
  ODM seulement.
- `group_by_flight` : les deux.

### 3.11 Reprise, fichiers temporaires, contrôles (ODM)

- Un calcul interrompu reprend là où il s'est arrêté (même appel, même `out_dir_project`).
- Si les images ou les options de l'alignement changent : erreur, nouveau calcul avec `overwrite = TRUE`.
- Si seules des options postérieures à l'alignement changent : chaque date est relancée à partir de la première
  étape concernée :
  - `--pc-quality` : à partir de `openmvs` ;
  - filtrage du nuage : à partir de `odm_filterpoints` ;
  - `--mesh-size`, `--use-3dmesh` : à partir de `odm_meshing` ;
  - couleurs, emprise, MNS : à partir de `mvs_texturing`.
- Fichiers temporaires (terra, GDAL, ODM) dans `tempdir_custom`.
- Fichiers de suivi dans `out_dir_project` :
  - `timings.csv` : temps par étape d'ODM ;
  - `epochs_check.json` : contrôle des dates ;
  - `color_gains.csv` et `vignetting.csv` : calibration des couleurs.

## 4. Ce qui a changé en 0.0.0.9012 pour rapprocher les deux chaînes

| Réglage | Avant (≤ 0.0.0.9011) | Maintenant | Effet attendu |
|---|---|---|---|
| Points détectés par image (ODM) | 10 000 | 10 000 par défaut, réglable (`keypoint_limit`) | voir le tableau des tests ci-dessous |
| Précision GPS (ODM) | 3 m | 10 m (comme Metashape) | poids des GPS dans l'ajustement identique |
| Résolution ODM | plafonnée par le GSD estimé (5,48 cm) puis rééchantillonnée à 5 cm | 5 cm directement (`--ignore-gsd`) | un rééchantillonnage de moins |
| Emprise ODM | 3 m retirés sur les bords | rien de retiré (`--crop 0`) | même emprise que Metashape |
| Mise au format ODM | toujours reprojetée (bilinéaire) | pixels gardés quand `crs` = zone UTM du site | pas de lissage supplémentaire |
| Rendu Metashape | MNS et orthomosaïque en WGS84, rééchantillonnés à l'export | construits directement dans `crs` | pas de lissage supplémentaire |
| Calibration des couleurs ODM | gains par image et par canal | + vignettage par caméra et par canal | comme `calibrateColors()` |
| `from_mesh` (ODM) | ignoré | maillage 3D (`--use-3dmesh`), pas de MNS | comme Metashape |
| Limites de points (Metashape) | fixées dans le script | paramètres `keypoint_limit`, `tiepoint_limit` (mêmes valeurs) | aucun changement de traitement |

Tests du nombre de points (jeu test, 3 dates, 24/11/2026) : la co-registration entre dates d'ODM est la même à
10 000, 20 000 et 100 000 points (2,34, 2,28 et 2,23 cm ; Metashape 2,5 à 2,6 cm). L'accord local avec Metashape
n'est atteint qu'à 100 000 points (10,1 cm, contre 37,2 et 38,5 cm), pour une durée ODM de 1 156 min contre 300 et 156
min. Choix retenu : 10 000 points par défaut pour ODM ; les écarts locaux avec Metashape sont corrigés par
l'alignement de la série (`align_Mosa()`).

Conséquence pratique : un projet ODM d'une version antérieure ne peut pas être repris (options d'alignement
différentes) ; il faut un nouveau `out_dir_project` ou `overwrite = TRUE`.

## 5. Différences qui restent

Elles tiennent aux algorithmes des deux logiciels et ne peuvent pas être réglées par des options.

1. **Surface de projection** (3.6) : MNS à 5 cm contre maillage 2.5D simplifié. C'est la différence la plus
   susceptible d'affecter la position des bords de houppiers ; elle se teste (`odm_mesh_size_test.R`).
2. **Détecteur et appariement** (3.2) :
   - détecteurs différents, et ODM limité à 4480 px ;
   - pas de limite du nombre de points de liaison gardés par image côté ODM ;
   - pas de seconde passe pour les images non alignées.
3. **Cartes de profondeur et filtrage** (3.5) : algorithmes et filtres différents à résolution égale.
4. **Assemblage et égalisation des raccords** (3.7).
5. **Calibration des couleurs** (3.4) : même modèle et mêmes points, mais l'algorithme de Metashape n'est pas
   documenté ; ODM y ajoute l'égalisation des raccords de la texture.
6. **Grille des rasters** (3.8) : origines propres à chaque logiciel.

## 6. À vérifier au prochain calcul (`benchmark_engines.R`, dossier `benchmark3`)

1. **Vignettage estimé par Metashape** : lignes « Colour calibration » de la console, recopiées dans
   `compare/colour_calibration_metashape.txt`. Si Metashape n'estime pas de vignettage, mettre
   `calibrate_vignetting = FALSE` par défaut côté ODM.
2. **Rendu Metashape directement dans `crs`** (non testé sans licence) : le calcul doit aller au bout, avec des
   mosaïques à `resol_ref`.
3. **Temps d'alignement ODM avec 100 000 points par image**, et nombre de correspondances entre dates (journal
   `odm_timesift_block.log`). Si c'est trop long, `keypoint_limit` peut être réduit pour les deux chaînes, ou
   pour ODM seul avec `odm_args = c("--min-num-features", "20000")`.
4. **Origine des grilles** des deux chaînes (`compare/grids_engines.csv`).
5. ~~Effet de `--mesh-size`~~ : testé, aucun gain (200 000 sommets gardés).
6. **Écarts géométriques et colorimétriques** entre les deux chaînes : `compare/summary.txt` (AROSICS) et
   `colour_calibration_test.R`.

## 7. Correspondance des paramètres

| `generate_Mosa()` | Metashape (API Python) | ODM |
|---|---|---|
| `downscale_factor_alignement` = 1 | `matchPhotos(downscale = 1)` | `--feature-quality ultra` (≤ 4480 px) |
| `keypoint_limit` (NULL : 100000 Metashape, 10000 ODM) | `matchPhotos(keypoint_limit)` | `--min-num-features` |
| `tiepoint_limit` = 10000 | `matchPhotos(tiepoint_limit)` | — |
| (fixe) | précision des caméras 10 m (défaut) | `--gps-accuracy 10` |
| `downscale_factor_depth_map` = 2 | `buildDepthMaps(downscale = 2, filter_mode = Aggressive)` | `--pc-quality ultra` |
| `resol_ref` | `buildDem/buildOrthomosaic/exportRaster(resolution)` | `--orthophoto-resolution`, `--dem-resolution` (cm), `--ignore-gsd` |
| `crs` | `projection` de `buildDem`, `buildOrthomosaic`, `exportRaster` | zone UTM d'ODM, reprojection R si différent |
| `calibrate_col`, `calibrate_col_by` | `calibrateColors(TiePointsData, white_balance = TRUE)` sur le bloc ou par date | `compute_color_gains()` de canObsR |
| `calibrate_vignetting` | (compris dans `calibrateColors`, à vérifier) | vignettage de `compute_color_gains()` |
| `from_mesh` | `buildModel` + `buildOrthomosaic(ModelData)` | `--use-3dmesh` |
| `out_dir_DEM` | `buildDem` + `exportRaster(ElevationData)` | `--dsm` |
| `group_by_flight` | un chunk par vol | une date par vol |
| (fixe) | `clip_to_boundary = TRUE`, fond blanc | `--crop 0`, fond blanc ajouté par canObsR |
| `odm_args` | — | options ODM supplémentaires (prioritaires) |

## Sources

- Code d'ODM 3.6.2 : `opendm/osfm.py` (taille des images pour la détection, configuration d'OpenSfM),
  `opendm/gsd.py` (plafonnement par le GSD), `stages/odm_meshing.py` et `opendm/mesh.py` (maillage 2.5D),
  `stages/openmvs.py`.
- Code d'OpenSfM : `features.py` (nombre cible de points DSP-SIFT),
  `src/lib/geometry/camera_distortions_functions.h` (modèle de Brown), `undistort.py`.
- Options d'ODM : https://docs.opendronemap.org/arguments/
- Calibration des couleurs de Metashape (option white balance, vignettage) :
  https://photogrammetrycommunity.github.io/metashape-expert-manual/workflow/texture/color-calibration/
  (source communautaire marquée non vérifiée, d'où le contrôle prévu au point 6.1).
- Précision par défaut des positions des caméras dans Metashape (10 m) :
  https://photogrammetrycommunity.github.io/metashape-expert-manual/reference/agisoft-knowledge-base/
- Journaux d'exécution des deux chaînes sur les données de test (novembre 2026).
