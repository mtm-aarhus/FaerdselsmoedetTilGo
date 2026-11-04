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
| Constant | `FaerdselsmoedetDatabase` | Sti til SQLite-kopi eller SQLAlchemy-URL. Standard: `faerdselsmoede_dev.db` |
| Credential | `FaerdselsmoedetAPI` | **Username:** web-appens API-URL, fx `http://localhost:5000/api/v1`. **Password:** API-nøglen |

API-nøglen skal være den samme som miljøvariablen `FAERDSELSMOEDE_API_KEY` på web-serveren.

## Bilag

Bilagene ligger på web-serverens lokale drev (`UPLOAD_FOLDER`). Robotten henter dem via
web-appens fil-API (`robot_framework/faerdselsmoede_api.py`, dokumentation i web-appens
`docs/API.md`):

1. `GET /status` – tjekker nøgle og URL.
2. `GET /moeder/{meeting_id}/bilag` – mødets bilag pr. dagsordenspunkt.
3. `GET /bilag/{id}` – hver fil. SHA-256 tjekkes, så en afbrudt overførsel opdages.

Fejl:

- Mangler et bilag på web-serveren → **BusinessError**: der lægges aldrig en ufuldstændig ZIP på GO.
- Forkert nøgle/URL, eller web-appen er nede → almindelig fejl: robotten prøver igen og sender fejlmail.

Dagsordenen har ingen bilag og bruger ikke API'et.
