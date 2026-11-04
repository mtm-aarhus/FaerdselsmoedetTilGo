"""robot_framework/process.py - Færdselsmødet: overfør dagsorden eller referat til GetOrganized.

Flow:
    1. Web-appen (Færdselsmødet) lægger to køelementer i OpenOrchestrator, når referenten
       trykker "Afslut referat": ét for dagsordenen og ét for referatet. Fejlede det, kan
       det sendes igen fra mødesiden.
    2. Køelementet er kun en pointer (maks. 2000 tegn), fx:
        {
            "meeting_id": "8f3c...",
            "meeting_number": 1302,              # valgfri - kun til log/reference
            "document_type": "referat",          # eller "dagsorden"
            "go_case_number": "GEO-2024-199848"  # valgfri - ellers GO_SAGSNUMMER_STANDARD
        }
    3. Robotten læser mødet direkte fra Færdselsmødets database, henter bilagene fra
       web-appens fil-API (kun referatet), laver dokumentet selv (faerdselsmoede_pdf.py)
       og lægger det på GO-sagen med titlen
       "Færdselsmøde nr. 1302 - referat", dagens dato og Korrespondance "Udgående":
         - dagsorden: én PDF
         - referat:   én ZIP med referatet (PDF) og bilagene, én mappe pr. dagsordenspunkt.
       Web-appen angiver sagen i køelementet (én sag til dagsordener, én til referater).

Bilag (Credential "FaerdselsmoedetAPI"):
    Bilagene ligger på web-serverens lokale drev og hentes via /api/v1 (se docs/API.md i
    web-appen og robot_framework/faerdselsmoede_api.py).
        Username = API'ets base-URL, fx "http://localhost:5000/api/v1"
        Password = API-nøglen (samme værdi som FAERDSELSMOEDE_API_KEY på web-serveren)
    Mangler et bilag på web-serveren, fejler referatet som BusinessError - så der aldrig
    lægges en ufuldstændig ZIP på sagen. Hver fil tjekkes med SHA-256 efter download.

Database (Constant "FaerdselsmoedetDatabase"):
    MIDLERTIDIGT: sti til en KOPI af web-appens SQLite-fil, fx "faerdselsmoede_dev.db" i
    robottens mappe. Kopien opdateres ikke af sig selv - møder oprettet efter kopieringen
    findes ikke, og fejler som BusinessError.
    SENERE (produktion): en SQLAlchemy-URL til SQL Server, fx
    "mssql+pyodbc://@SERVER/POFCBA?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes".

Fejltyper:
    BusinessError   -> permanent: kan aldrig lykkes (mødet findes ikke, ugyldig dokumenttype,
                       GO afviser med 4xx). Markeres Failed, ingen retry.
    Anden exception -> midlertidig eller opsætningsfejl (databasefil mangler, GO nede).
                       Frameworket prøver igen op til MAX_RETRY_COUNT og sender fejlmail.
"""
import json
import re
import time
from datetime import datetime

import requests
from requests_ntlm import HttpNtlmAuth
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement

from robot_framework.exceptions import BusinessError
from robot_framework.faerdselsmoede_api import FaerdselsmoedeApi
import faerdselsmoede_pdf   # ligger i samme mappe som main.py

# ---------------------------------------------------------------------------
# Navne på Constants/Credentials i OpenOrchestrator (ret her, hvis de hedder andet)
# ---------------------------------------------------------------------------
CONST_GO_API_URL = "GOApiURL"                       # samme constant som i Personalemappe-robotten
CRED_GO_API_USER = "GOAdminUser"                    # GO API-bruger (username/password)
CONST_DATABASE = "FaerdselsmoedetDatabase"          # sti til SQLite-kopi ELLER SQLAlchemy-URL
DATABASE_STANDARD = "faerdselsmoede_dev.db"         # bruges hvis constanten ikke findes
CRED_FAERDSELSMOEDE_API = "FaerdselsmoedetAPI"      # web-appens fil-API: username = base-URL, password = nøgle

# GO-sagen alle færdselsmødedokumenter lægges på, når køelementet ikke angiver en anden.
GO_SAGSNUMMER_STANDARD = "GEO-2024-199848"
GO_MAKS_BYTES = 10 * 1024 * 1024   # AddToCase-grænse (større filer kræver chunk-upload)

GYLDIGE_DOKUMENTTYPER = ("dagsorden", "referat")

# Retry af kald til GO (samme mønster som nova_client._with_retries)
HTTP_FORSOEG = 3
HTTP_PAUSE_SEK = 20


def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    """Behandler ét køelement: læser mødet, laver dokumentet (PDF / ZIP) og overfører det til GO."""
    log = orchestrator_connection.log_info
    orchestrator_connection.log_trace("Running process.")

    # 1. Læs og valider beskeden fra køen ------------------------------------
    payload = _parse_queue_element(queue_element)
    meeting_id = payload["meeting_id"]
    document_type = payload["document_type"]
    go_case_number = payload.get("go_case_number")   # tom -> GO_SAGSNUMMER_STANDARD

    log(f"Behandler {document_type} for møde {payload.get('meeting_number') or meeting_id}")

    # 2. Constants og credentials (samme måde som Personalemappe-robotten) ----
    go_api_url = orchestrator_connection.get_constant(CONST_GO_API_URL).value
    go_api_login = orchestrator_connection.get_credential(CRED_GO_API_USER)
    database = _hent_database_constant(orchestrator_connection)
    log("Constants og credentials hentet")

    # 3. Læs mødet fra databasen ---------------------------------------------
    engine = faerdselsmoede_pdf.opret_engine(database)
    try:
        resultat = faerdselsmoede_pdf.hent_moede(engine, meeting_id)
    finally:
        engine.dispose()
    if resultat is None:
        raise BusinessError(f"Mødet {meeting_id} findes ikke i databasen ({database}). "
                            "Er databasekopien ældre end mødet? Kopiér en ny og læg elementet i køen igen.")
    moede, indstillinger = resultat
    log(f"Færdselsmøde nr. {moede.meeting_number} hentet med {len(indstillinger)} indstilling(er)")

    # 4. Lav dokumentet: dagsorden = PDF, referat = ZIP med referat + bilag ---
    if document_type == "dagsorden":
        indhold, filtype = faerdselsmoede_pdf.build_agenda_pdf(moede, indstillinger), "pdf"
    else:
        api_login = orchestrator_connection.get_credential(CRED_FAERDSELSMOEDE_API)
        api = FaerdselsmoedeApi(api_login.username, api_login.password)
        api.status()   # forkert nøgle/URL opdages her, før der hentes filer
        bilag = api.hent_moedets_bilag(meeting_id)
        antal = sum(len(filer) for filer in bilag.values())
        log(f"{antal} bilag hentet fra Færdselsmødets API "
            f"({sum(len(i) for filer in bilag.values() for _, i in filer) / 1024:.0f} KB)")
        indhold, ukendte = faerdselsmoede_pdf.build_referat_zip(moede, indstillinger, bilag)
        if ukendte:
            raise BusinessError(f"Web-appen har bilag til {len(ukendte)} dagsordenspunkt(er), som ikke findes i "
                                f"robottens database ({database}). Kopiér en ny database og læg elementet i køen igen.")
        filtype = "zip"

    # Titel som de eksisterende dokumenter i GO: "Færdselsmøde nr. 1302 - referat".
    # En rettelse (manuel overførsel) bliver et NYT dokument, fordi det første er
    # journaliseret i GO og ikke skal overskrives.
    titel = f"Færdselsmøde nr. {moede.meeting_number} - {document_type}"
    if payload.get("rettelse"):
        titel += f" (rettet {datetime.now().strftime('%d-%m-%Y %H.%M')})"
    log(f"{filtype.upper()} lavet: {titel} ({len(indhold) / 1024:.0f} KB)")

    # 5. Overfør til GO og journaliser --------------------------------------
    session = requests.Session()
    session.auth = HttpNtlmAuth(go_api_login.username, go_api_login.password)

    doc_id = overfoer_til_go(
        orchestrator_connection=orchestrator_connection,
        session=session,
        go_api_url=go_api_url,
        go_case_number=go_case_number,
        titel=titel,
        indhold=indhold,
        filtype=filtype,
    )
    log(f"Overført {titel} til GO (DocId {doc_id})")

    journaliser_dokument(go_api_url, doc_id, session)
    log(f"Journaliseret DocId {doc_id}")

    # MIDLERTIDIGT: find GO's interne feltnavn for dokumentansvarlig (fjernes igen)
    log_brugerfelter(orchestrator_connection, go_api_url, doc_id, session,
                     soeg=go_api_login.username.split("\\")[-1])


# ---------------------------------------------------------------------------
# Kø-beskeden og opsætning
# ---------------------------------------------------------------------------

def _parse_queue_element(queue_element: QueueElement | None) -> dict:
    """Læser køelementets JSON og tjekker de påkrævede felter. Fejl her er permanente."""
    if queue_element is None or not queue_element.data:
        raise BusinessError("Køelementet mangler data.")
    try:
        payload = json.loads(queue_element.data)
    except (TypeError, ValueError) as e:
        raise BusinessError(f"Køelementets data er ikke gyldig JSON: {e}") from e

    mangler = [k for k in ("meeting_id", "document_type") if not payload.get(k)]
    if mangler:
        raise BusinessError(f"Køelementet mangler påkrævede felter {mangler}: {payload}")

    payload["document_type"] = str(payload["document_type"]).strip().lower()
    if payload["document_type"] not in GYLDIGE_DOKUMENTTYPER:
        raise BusinessError(f"Ukendt dokumenttype '{payload['document_type']}' - skal være 'dagsorden' eller 'referat'.")
    return payload


def _hent_database_constant(orchestrator_connection: OrchestratorConnection) -> str:
    """Databasen fra Constant; findes den ikke, bruges SQLite-kopien i robottens mappe."""
    return _hent_constant(orchestrator_connection, CONST_DATABASE, DATABASE_STANDARD)


def _hent_constant(orchestrator_connection: OrchestratorConnection, navn: str, standard: str) -> str:
    """En Constant (uden omkransende anførselstegn); findes den ikke, bruges `standard`."""
    try:
        return orchestrator_connection.get_constant(navn).value.strip().strip('"').strip("'")
    except ValueError:
        orchestrator_connection.log_info(f"Constant '{navn}' findes ikke - bruger {standard}")
        return standard


# ---------------------------------------------------------------------------
# GO
# ---------------------------------------------------------------------------

def overfoer_til_go(orchestrator_connection: OrchestratorConnection, session: requests.Session, go_api_url: str,
                    go_case_number: str | None, titel: str, indhold: bytes, filtype: str = "pdf") -> int:
    """Uploader ét dokument til GO-sagen via /_goapi/Documents/AddToCase
    (samme metode som Personalemappe-robotten).

    Metadata svarer til de eksisterende færdselsmødedokumenter i GO:
        Titel          = "Færdselsmøde nr. 1302 - referat"
        Dato           = overførselsdagen (dd-mm-åååå)
        Korrespondance = "Udgående"
    Parter og dokumentansvarlig sættes ikke (endnu).

    "Overwrite" er True, så en automatisk genkørsel (fx hvis journaliseringen fejlede)
    overskriver det ikke-journaliserede dokument i stedet for at lave en dublet.
    Rettelser får en anden titel og dermed et nyt filnavn (se process()).

    Returnerer GO's DocId.
    """
    sagsnummer = (go_case_number or GO_SAGSNUMMER_STANDARD).strip().upper()
    filnavn = _sikkert_filnavn(f"{titel}.{filtype}")

    if len(indhold) > GO_MAKS_BYTES:
        raise BusinessError(f"{filnavn} er {len(indhold) / 1024 / 1024:.1f} MB - over GO's grænse på 10 MB for normal upload.")

    ows = {
        "Title": titel,
        "CaseID": sagsnummer,
        "Dato": datetime.now().strftime("%d-%m-%Y"),
        "Korrespondance": "Udgående",
    }
    metadata_xml = '<z:row xmlns:z="#RowsetSchema" ' + " ".join(
        f'ows_{k}="{_xml_attr(v)}"' for k, v in ows.items()) + "/>"

    payload = {
        "Bytes": list(indhold),
        "CaseId": sagsnummer,
        "ListName": "Dokumenter",
        "FolderPath": "",
        "FileName": filnavn,
        "Metadata": metadata_xml,
        "Overwrite": True,
    }

    url = f"{go_api_url.rstrip('/')}/_goapi/Documents/AddToCase"

    orchestrator_connection.log_info(f"Uploader {filnavn} til GO-sag {sagsnummer}")
    resp = _go_post_med_retries(session, url, sagsnummer, data=payload)
    svar = resp.json()
    if "DocId" not in svar:
        raise RuntimeError(f"GO svarede uden DocId for {filnavn}: {str(svar)[:300]}")
    return svar["DocId"]


def journaliser_dokument(go_api_url: str, doc_id: int, session: requests.Session) -> None:
    """Journaliserer dokumentet (markerer det som sagsakt) - samme kald som Aktbob-robotten."""
    url = f"{go_api_url.rstrip('/')}/_goapi/Documents/MarkMultipleAsCaseRecord/ByDocumentId"
    _go_post_med_retries(session, url, f"DocId {doc_id}", json={"DocumentIds": [doc_id]})


def log_brugerfelter(orchestrator_connection: OrchestratorConnection, go_api_url: str, doc_id: int,
                     session: requests.Session, soeg: str) -> None:
    """MIDLERTIDIG fejlsøgning: logger de metadatafelter på dokumentet, hvis værdi
    indeholder `soeg` (fx robotbrugeren), så vi kan se feltnavn og format for
    dokumentansvarlig. Kan aldrig få kørslen til at fejle."""
    try:
        url = f"{go_api_url.rstrip('/')}/_goapi/Documents/MetadataWithSystemFields/{doc_id}"
        resp = session.get(url, timeout=120)
        resp.raise_for_status()
        tekst = resp.text
        metadata = tekst
        try:
            metadata = resp.json().get("Metadata", tekst)
        except ValueError:
            pass
        fund = [m.group(0) for m in re.finditer(r'ows_[A-Za-z0-9_]+="[^"]*"', metadata)
                if soeg.lower() in m.group(0).lower()]
        orchestrator_connection.log_info(
            f"FELTSØGNING DocId {doc_id} ('{soeg}'): " + (" | ".join(fund) if fund else "ingen felter fundet"))
    except Exception as e:  # pylint: disable=broad-exception-caught
        orchestrator_connection.log_info(f"FELTSØGNING fejlede for DocId {doc_id}: {e}")


def _go_post_med_retries(session: requests.Session, url: str, hvad: str, **kwargs) -> requests.Response:
    """POST til GO med retry ved forbindelsesfejl, timeout og 5xx.
    4xx er permanent (fx sagen findes ikke eller brugeren mangler adgang) -> BusinessError."""
    sidste_fejl = None
    for forsoeg in range(1, HTTP_FORSOEG + 1):
        try:
            resp = session.post(url, timeout=600, **kwargs)
            if 400 <= resp.status_code < 500:
                raise BusinessError(f"GO afviste kaldet for {hvad} (HTTP {resp.status_code}): {resp.text[:300]}")
            if resp.status_code < 400:
                return resp
            sidste_fejl = RuntimeError(f"HTTP {resp.status_code} fra GO: {resp.text[:300]}")
        except (requests.ConnectionError, requests.Timeout) as e:
            sidste_fejl = e
        if forsoeg < HTTP_FORSOEG:
            time.sleep(HTTP_PAUSE_SEK)
    raise RuntimeError(f"GO svarede ikke efter {HTTP_FORSOEG} forsøg: {sidste_fejl}")


def _xml_attr(vaerdi: str) -> str:
    """Escaper en værdi, så den kan stå i en XML-attribut i GO-metadata."""
    return (str(vaerdi).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _sikkert_filnavn(navn: str) -> str:
    """Fjerner tegn, som Windows/SharePoint ikke tillader i filnavne."""
    return "".join("_" if c in '<>:"/\\|?*#%' else c for c in navn).strip()
