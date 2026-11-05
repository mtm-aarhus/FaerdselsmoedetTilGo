# FaerdselsmoedetTilGo

OpenOrchestrator-robot (kø: `FaerdselsmoedetGoOverfoersel`), der lægger dokumenter fra
Færdselsmødet på GetOrganized. Bygget på ITK's Robot-Framework (queue framework).

Web-appen lægger to køelementer i køen, når referenten trykker **Afslut referat**:

- **dagsorden** – én PDF
- **referat** – én ZIP med referatet (PDF) og bilagene, én mappe pr. dagsordenspunkt

## Køelementet

```json
{
  "meeting_id": "8f3c…",
  "meeting_number": 1302,
  "document_type": "referat",
  "go_case_number": "GEO-2024-199848",
  "rettelse": false
}
```

Kun `meeting_id` og `document_type` er påkrævet. Web-appen angiver GO-sagen
(`GO_CASE_NUMBER_DAGSORDEN` / `GO_CASE_NUMBER_REFERAT`). `rettelse` er `true`, når mødet
er sendt til GO før, og så får dokumentet "(rettet …)" i titlen.

## Opsætning i OpenOrchestrator

| Type | Navn | Indhold |
|---|---|---|
| Constant | `GOApiURL` | GO's API-URL |
| Credential | `GOAdminUser` | GO API-bruger (username/password) |
| Credential | `FaerdselsmoedetAPI` | **Username:** `https://faerdselsmoedet.adm.aarhuskommune.dk/api/v1`. **Password:** API-nøglen |

API-nøglen skal være den samme som miljøvariablen `FAERDSELSMOEDE_API_KEY` på web-serveren.
Robotten bruger ikke længere Færdselsmødets database (constanten `FaerdselsmoedetDatabase` skal ikke bruges).

## Dokumenterne

Sitet bygger selv dagsordenen og referatet med bilag – præcis de dokumenter, deltagerne fik –
og robotten henter dem via sitets API (`robot_framework/faerdselsmoede_api.py`, dokumentation
i web-appens `docs/API.md`):

1. `GET /status` – tjekker nøgle og URL.
2. `GET /moeder/{meeting_id}` – mødenummer til titlen i GO.
3. `GET /moeder/{meeting_id}/dagsorden.pdf` eller `GET /moeder/{meeting_id}/referat.zip` –
   det færdige dokument. SHA-256 tjekkes, så en afbrudt overførsel opdages.

## Upload til GO

- **Op til 10 MB:** `/_goapi/Documents/AddToCase`
- **Over 10 MB:** chunk-upload via SharePoint (`startUpload` / `continueUpload` / `finishUpload`,
  10 MB pr. bid), derefter findes DocId, og metadata (titel, dato, korrespondance) sættes –
  samme metode som Aktbob-robotten.

Bagefter journaliseres dokumentet.

## Fejl

- Mødet findes ikke, eller et bilag mangler på web-serveren → **BusinessError**: der lægges
  aldrig en ufuldstændig ZIP på GO.
- Forkert nøgle/URL, eller sitet/GO er nede → almindelig fejl: robotten prøver igen og sender fejlmail.
