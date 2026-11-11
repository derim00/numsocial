# NumSocial : les réseaux sociaux sur NumWorks

**NumSocial met YouTube et Snapchat sur une calculatrice NumWorks.**
La calculatrice reste branchée en USB à un ordinateur (Mac ou Windows), qui fait
passerelle avec Internet. Tout se passe sur la calculatrice : on cherche une
vidéo et on la regarde, on lit ses messages Snapchat et on y répond depuis le
clavier.

| Appli | Ce qu'elle fait | Dépôt |
| --- | --- | --- |
| **NumTube** | YouTube : recherche, lecture 320×180 à ~15 images/s, sous-titres | [derim00/numtube](https://github.com/derim00/numtube) |
| **NumChat** | Snapchat : conversations, réponses avec accents, emojis, snaps photo et vidéo | [derim00/numchat](https://github.com/derim00/numchat) |
| **NumSocial** | l'appli principale : installe NumTube et/ou NumChat en un clic, et fait passerelle pour les deux | ce dépôt |

## Ce qui marche aujourd'hui

- ✅ **YouTube sur la calculatrice** avec NumTube : la vidéo est décodée sur
  l'ordinateur, compressée en blocs de 4×4 pixels et envoyée image par image ;
  les sous-titres remplacent le son, que la NumWorks n'a pas.
- ✅ **Snapchat sur la calculatrice** avec NumChat : liste des conversations,
  messages en direct, saisie avec majuscules et accents, emojis en couleur,
  snaps photo et vidéo affichés sur demande.
- ✅ **Une seule appli pour tout installer** : elle lit la version de la
  calculatrice, laisse choisir les applis et les installe sans toucher au
  firmware officiel.
- ✅ **Mac et Windows** : l'appli se compile pour les deux, dans ta copie privée
  de ce dépôt.

## Feuille de route

| | Fonction | Idée |
| --- | --- | --- |
| 🔜 | **Discord** | Lire et écrire dans tes messages privés et tes salons, sur le modèle de NumChat |
| 🔜 | **Partage d'écran** | Afficher sur la calculatrice l'écran de l'ordinateur auquel elle est branchée, en reprenant le codec vidéo de NumTube |
| 🔜 | **WhatsApp** | Tes discussions WhatsApp sur la calculatrice, à travers WhatsApp Web |

Chaque nouvelle appli rejoindra le même firmware et la même appli NumSocial : à
l'installation, tu coches celles que tu veux.

## Installation (l'appli principale)

### 1. Obtenir l'appli NumSocial

Depuis juillet 2021, le code d'Epsilon (le logiciel de la NumWorks) n'est plus
sous licence libre. NumSocial ne publie donc **aucun code ni aucun firmware de
NumWorks** : seulement un **patch** (`patches/numsocial-epsilon.patch`) et son
propre code. Le firmware se compile chez toi, à partir des sources officielles
de NumWorks.

**Sans rien compiler (Windows ou Mac)**

1. En haut de cette page : **Use this template** > **Create a new repository**,
   coche **Private** (obligatoire : la compilation refuse de tourner dans un
   dépôt public, pour que le firmware ne soit pas téléchargeable par tous).
2. Dans ta copie : onglet **Actions** > **Build NumSocial** > **Run workflow**.
3. Environ 15 minutes plus tard, en bas de la page de la compilation, télécharge
   **NumSocial-windows** ou **NumSocial-mac**, et décompresse.
4. Windows : lance `NumSocial\NumSocial.exe` (SmartScreen : « Informations
   complémentaires » > « Exécuter quand même »). Mac : clic droit sur
   `NumSocial.app` > **Ouvrir**.

**En compilant sur ton Mac**

```bash
git clone https://github.com/derim00/numsocial.git && cd numsocial
./scripts/build-firmware.sh          # télécharge Epsilon, applique le patch, compile
.venv/bin/python3 app/build.py       # fabrique l'appli
open app/dist/NumSocial.app
```

### 2. Installer sur la calculatrice

1. Branche la calculatrice : elle affiche « Calculatrice connectée » (sinon :
   reset au dos, puis rebranche).
2. Sur la page de NumSocial (`http://127.0.0.1:8737`), coche **NumTube**,
   **NumChat** ou les deux.
3. Clique **Installer sur la calculatrice** (environ 1 minute).

Le firmware officiel reste en place dans l'autre emplacement de la mémoire : un
reset au dos le ramène.

### 3. Utiliser

Laisse NumSocial ouvert sur l'ordinateur, puis ouvre l'appli voulue sur la
calculatrice. Les modes d'emploi détaillés sont dans chaque dépôt :

- **[Tutoriel NumTube](https://github.com/derim00/numtube#utilisation)**
- **[Tutoriel NumChat](https://github.com/derim00/numchat#tutoriel--première-connexion-à-snapchat)**
  (première connexion à Snapchat, touches, snaps)

## Ce qu'il faut

- Une NumWorks **N0120** ou **N0115** en version officielle **26.4.0** (testé)
- Un câble USB qui transfère les données
- Un **Mac** (testé avec une calculatrice) ou un **PC Windows 10/11** (compilé
  et vérifié par GitHub, pas encore essayé avec une calculatrice)
- Pour NumChat : **Google Chrome** (ou Microsoft Edge sous Windows) et un compte
  Snapchat

## Comment c'est construit

```
            ┌──────────────────────── calculatrice NumWorks ────────────────────────┐
            │  Epsilon officiel + patch NumSocial : NumTube, NumChat, lien USB      │
            └───────────────────────────────┬───────────────────────────────────────┘
                                            │ USB (libusb, trames TMIA + CRC-16)
            ┌───────────────────────────────┴───────────────────────────────────────┐
            │  appli NumSocial (ordinateur) : installeur + passerelle                │
            │   ├─ NumTube : yt-dlp, ffmpeg, codec 4×4, sous-titres                  │
            │   └─ NumChat : Playwright → Chrome (profil dédié) → Snapchat Web       │
            └────────────────────────────────────────────────────────────────────────┘
```

- **Un seul firmware** pour toutes les applis. L'installeur y inscrit la
  version officielle de la calculatrice (sinon le noyau refuse de démarrer) et
  un masque qui cache les applis que tu n'as pas cochées.
- **Lien USB** repris de [Termia](https://github.com/Matinthereal/termia), avec
  deux corrections (débordement d'un tableau de requêtes, fin d'envoi jamais vue
  pendant une réception continue).
- **Pas de pilote à installer** : interface USB propriétaire avec descripteurs
  Microsoft OS (WinUSB) pour Windows.

Ce dépôt contient le même code que [NumTube](https://github.com/derim00/numtube)
et [NumChat](https://github.com/derim00/numchat) : patch complet, appli, scripts,
tests.

## Avertissements

- Projet non officiel, sans lien avec NumWorks, YouTube, Snap Inc., Discord ou
  Meta. Les noms cités sont des marques de leurs propriétaires.
- Télécharger des vidéos avec yt-dlp va à l'encontre des conditions
  d'utilisation de YouTube.
- Piloter Snapchat Web automatiquement va à l'encontre des conditions
  d'utilisation de Snapchat et peut faire **verrouiller ton compte** : essaie
  d'abord avec un compte secondaire.
- Un firmware non officiel ne peut pas passer en mode examen.

## Licence

Le code de ce dépôt est sous licence
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.fr),
comme Termia dont il reprend le lien USB. Voir [LICENSE](LICENSE).
