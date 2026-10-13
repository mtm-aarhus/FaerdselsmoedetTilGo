import json
import time
from datetime import datetime

import requests
from requests_ntlm import HttpNtlmAuth
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement

from robot_framework.exceptions import BusinessError
import faerdselsmoede_pdf

# ---------------------------------------------------------------------------
# Navne på Constants/Credentials i OpenOrchestrator (ret her, hvis de hedder andet)
# ---------------------------------------------------------------------------
CONST_GO_API_URL = "GOApiURL"                       # samme constant som i Personalemappe-robotten
CRED_GO_API_USER = "GOAktApiUser"                   # GO API-bruger (username/password)
CONST_DATABASE = "FaerdselsmoedetDatabase"          # sti til SQLite-kopi ELLER SQLAlchemy-URL
DATABASE_STANDARD = "faerdselsmoede_dev.db"         # bruges hvis constanten ikke findes

# GO-sagen alle færdselsmødedokumenter lægges på, når køelementet ikke angiver en anden.
GO_SAGSNUMMER_STANDARD = "GEO-2024-199848"
GO_MAKS_BYTES = 10 * 1024 * 1024   # AddToCase-grænse (større filer kræver chunk-upload)

GYLDIGE_DOKUMENTTYPER = ("dagsorden", "referat")

# Retry af kald til GO (samme mønster som nova_client._with_retries)
HTTP_FORSOEG = 3
HTTP_PAUSE_SEK = 20


def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    """Behandler ét køelement: læser mødet, laver PDF'en og overfører den til GO."""
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

    # 4. Lav PDF'en ----------------------------------------------------------
    if document_type == "dagsorden":
        pdf = faerdselsmoede_pdf.build_agenda_pdf(moede, indstillinger)
    else:
        pdf = faerdselsmoede_pdf.build_referat_pdf(moede, indstillinger)

    # Titel som de eksisterende dokumenter i GO: "Færdselsmøde nr. 1302 - referat"
    titel = f"Færdselsmøde nr. {moede.meeting_number} - {document_type}"
    log(f"PDF lavet: {titel} ({len(pdf) / 1024:.0f} KB)")

    # 5. Overfør til GO ------------------------------------------------------
    doc_id = overfoer_til_go(
        orchestrator_connection=orchestrator_connection,
        go_api_url=go_api_url,
        go_username=go_api_login.username,
        go_password=go_api_login.password,
        go_case_number=go_case_number,
        titel=titel,
        indhold=pdf,
    )
    log(f"Overført {titel} til GO (DocId {doc_id})")


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
    try:
        return orchestrator_connection.get_constant(CONST_DATABASE).value
    except ValueError:
        orchestrator_connection.log_info(f"Constant '{CONST_DATABASE}' findes ikke - bruger {DATABASE_STANDARD}")
        return DATABASE_STANDARD


# ---------------------------------------------------------------------------
# GO
# ---------------------------------------------------------------------------

def overfoer_til_go(orchestrator_connection: OrchestratorConnection, go_api_url: str, go_username: str,
                    go_password: str, go_case_number: str | None, titel: str, indhold: bytes) -> int:
    """Uploader ét dokument til GO-sagen via /_goapi/Documents/AddToCase
    (samme metode som Personalemappe-robotten).

    Metadata svarer til de eksisterende færdselsmødedokumenter i GO:
        Titel          = "Færdselsmøde nr. 1302 - referat"
        Dato           = overførselsdagen (dd-mm-åååå)
        Korrespondance = "Udgående"
    Parter og dokumentansvarlig sættes ikke (endnu).

    Idempotens: filnavnet er fast pr. møde og dokumenttype, og "Overwrite" er True,
    så en genkørsel (fx manuel overførsel efter en rettelse) overskriver dokumentet
    i stedet for at lave en dublet.

    Returnerer GO's DocId.
    """
    sagsnummer = (go_case_number or GO_SAGSNUMMER_STANDARD).strip().upper()
    filnavn = _sikkert_filnavn(f"{titel}.pdf")

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

    session = requests.Session()
    session.auth = HttpNtlmAuth(go_username, go_password)
    url = f"{go_api_url.rstrip('/')}/_goapi/Documents/AddToCase"

    orchestrator_connection.log_info(f"Uploader {filnavn} til GO-sag {sagsnummer}")
    resp = _go_post_med_retries(session, url, payload, sagsnummer)
    svar = resp.json()
    if "DocId" not in svar:
        raise RuntimeError(f"GO svarede uden DocId for {filnavn}: {str(svar)[:300]}")
    return svar["DocId"]


def _go_post_med_retries(session: requests.Session, url: str, payload: dict, sagsnummer: str) -> requests.Response:
    """POST til GO med retry ved forbindelsesfejl, timeout og 5xx.
    4xx er permanent (fx sagen findes ikke eller brugeren mangler adgang) -> BusinessError."""
    sidste_fejl = None
    for forsoeg in range(1, HTTP_FORSOEG + 1):
        try:
            resp = session.post(url, data=payload, timeout=600)
            if 400 <= resp.status_code < 500:
                raise BusinessError(f"GO afviste uploadet til sag {sagsnummer} (HTTP {resp.status_code}): {resp.text[:300]}")
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
