"""robot_framework/process.py - Færdselsmødet: overfør dagsorden eller referat til GetOrganized.

Flow:
    1. Web-appen (Færdselsmødet) lægger to køelementer i OpenOrchestrator, når referenten
       trykker "Afslut referat": ét for dagsordenen og ét for referatet.
    2. Køelementet er kun en pointer (maks. 2000 tegn), fx:
        {
            "meeting_id": "8f3c...",
            "meeting_number": 1302,              # valgfri - kun til log/reference
            "document_type": "referat",          # eller "dagsorden"
            "go_case_number": "GEO-2024-199848", # valgfri - ellers GO_SAGSNUMMER_STANDARD
            "rettelse": false                    # true = mødet er sendt til GO før
        }
    3. Robotten henter det FÆRDIGE dokument fra sitets API (se faerdselsmoede_api.py):
         - dagsorden: én PDF
         - referat:   én ZIP med referatet (PDF) og bilagene, én mappe pr. dagsordenspunkt
       Sitet bygger dokumenterne selv - de er præcis dem, deltagerne fik. Robotten har
       derfor ingen adgang til sitets database eller bilagsmappe.
    4. Dokumentet lægges på GO-sagen med titlen "Færdselsmøde nr. 1302 - referat",
       dagens dato og Korrespondance "Udgående", og journaliseres.
         - Op til 10 MB: /_goapi/Documents/AddToCase
         - Over 10 MB:   chunk-upload via SharePoint (samme metode som Aktbob-robotten)

Credentials/Constants i OpenOrchestrator:
    FaerdselsmoedetAPI (credential): Username = API'ets base-URL, Password = API-nøglen
    GOApiURL (constant), GOAdminUser (credential)

Fejltyper:
    BusinessError   -> permanent: kan aldrig lykkes (mødet findes ikke, bilag mangler på sitet,
                       ugyldig dokumenttype, GO afviser med 4xx). Markeres Failed, ingen retry.
    Anden exception -> midlertidig eller opsætningsfejl (sitet/GO nede, forkert nøgle).
                       Frameworket prøver igen op til MAX_RETRY_COUNT og sender fejlmail.
"""
import io
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote

import requests
from requests_ntlm import HttpNtlmAuth
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement

from robot_framework.exceptions import BusinessError
from robot_framework.faerdselsmoede_api import FaerdselsmoedeApi

# ---------------------------------------------------------------------------
# Navne på Constants/Credentials i OpenOrchestrator 
# ---------------------------------------------------------------------------
CONST_GO_API_URL = "GOApiURL"                       # samme constant som i Personalemappe-robotten
CRED_GO_API_USER = "GOAdminUser"                    # GO API-bruger (username/password)
CRED_FAERDSELSMOEDE_API = "FaerdselsmoedetAPI"      # web-appens fil-API: username = base-URL, password = nøgle

# GO-sagen alle færdselsmødedokumenter lægges på, når køelementet ikke angiver en anden.
GO_SAGSNUMMER_STANDARD = "GEO-2024-199848"
GO_MAKS_BYTES = 10 * 1024 * 1024   # AddToCase-grænse - større filer chunk-uploades
CHUNK_BYTES = 10 * 1024 * 1024     # størrelse pr. chunk ved stor upload

GYLDIGE_DOKUMENTTYPER = ("dagsorden", "referat")

# Retry af kald til GO (samme mønster som nova_client._with_retries)
HTTP_FORSOEG = 3
HTTP_PAUSE_SEK = 20


def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    """Behandler ét køelement: henter dokumentet fra sitet og overfører det til GO."""
    log = orchestrator_connection.log_info
    orchestrator_connection.log_trace("Running process.")

    # 1. Læs og valider beskeden fra køen ------------------------------------
    payload = _parse_queue_element(queue_element)
    meeting_id = payload["meeting_id"]
    document_type = payload["document_type"]
    go_case_number = payload.get("go_case_number")   # tom -> GO_SAGSNUMMER_STANDARD

    log(f"Behandler {document_type} for møde {payload.get('meeting_number') or meeting_id}")

    # 2. Constants og credentials ---------------------------------------------
    go_api_url = orchestrator_connection.get_constant(CONST_GO_API_URL).value
    go_api_login = orchestrator_connection.get_credential(CRED_GO_API_USER)
    api_login = orchestrator_connection.get_credential(CRED_FAERDSELSMOEDE_API)
    log("Constants og credentials hentet")

    # 3. Hent mødet og det færdige dokument fra sitet ---------------------------
    api = FaerdselsmoedeApi(api_login.username, api_login.password)
    api.status()   # forkert nøgle/URL opdages her
    moede = api.moede_info(meeting_id)
    indhold, filtype = api.hent_dokument(meeting_id, document_type)
    log(f"{moede['label']}: {document_type} hentet fra sitet ({filtype.upper()}, {len(indhold) / 1024:.0f} KB)")

    # Titel som de eksisterende dokumenter i GO: "Færdselsmøde nr. 1302 - referat".
    # En rettelse bliver et NYT dokument, fordi det første er journaliseret i GO og
    # ikke skal overskrives.
    titel = f"Færdselsmøde nr. {moede['meeting_number']} - {document_type}"
    if payload.get("rettelse"):
        titel += f" (rettet {datetime.now().strftime('%d-%m-%Y %H.%M')})"

    # 4. Overfør til GO og journaliser --------------------------------------
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

    ows = {
        "Title": titel,
        "CaseID": sagsnummer,
        "Dato": datetime.now().strftime("%d-%m-%Y"),
        "Korrespondance": "Udgående",
    }
    metadata_xml = '<z:row xmlns:z="#RowsetSchema" ' + " ".join(
        f'ows_{k}="{_xml_attr(v)}"' for k, v in ows.items()) + "/>"

    if len(indhold) > GO_MAKS_BYTES:
        # Over AddToCase-grænsen: chunk-upload via SharePoint (samme metode som Aktbob-robotten)
        orchestrator_connection.log_info(
            f"{filnavn} er {len(indhold) / 1024 / 1024:.1f} MB - over 10 MB, bruger chunk-upload til GO-sag {sagsnummer}")
        return upload_stort_dokument(orchestrator_connection, session, go_api_url, sagsnummer,
                                     filnavn, indhold, metadata_xml)

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
    resp = _go_post_med_retries(session, url, sagsnummer, json=payload)
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


# ---------------------------------------------------------------------------
# Stor upload (> 10 MB) - 
# ---------------------------------------------------------------------------

def upload_stort_dokument(orchestrator_connection: OrchestratorConnection, session: requests.Session,
                          go_api_url: str, sagsnummer: str, filnavn: str, indhold: bytes,
                          metadata_xml: str) -> int:
    """Chunk-uploader filen til sagens dokumentbibliotek, finder dens DocId og sætter
    metadata (titel, dato, korrespondance). Returnerer DocId.

    Bruger en separat session, fordi chunk-upload sætter SharePoint-headere
    (X-RequestDigest, Accept: odata=verbose), som ikke må følge med til andre GO-kald."""
    log = orchestrator_connection.log_info
    sp = requests.Session()
    sp.auth = session.auth
    api_url = go_api_url.rstrip("/")

    case_url = _hent_case_url(api_url, sp, sagsnummer)
    digest = _request_form_digest(api_url, case_url, sp)
    _chunked_file_upload(api_url, case_url, indhold, filnavn, sp, digest, "", log)

    doc_id = None
    for forsoeg in range(1, 7):          # SharePoint/GO skal lige registrere filen
        time.sleep(5)
        doc_id = _find_docid(filnavn, api_url, case_url, "", sp, log)
        if doc_id is not None:
            break
        log(f"DocId for {filnavn} ikke fundet endnu (forsøg {forsoeg})")
    if doc_id is None:
        raise RuntimeError(f"Stor upload af {filnavn} gennemført, men DocId kunne ikke findes på sagen {sagsnummer}.")

    _opdater_metadata(api_url, doc_id, session, metadata_xml)
    log(f"Stor upload færdig: {filnavn} (DocId {doc_id})")
    return int(doc_id)


def _hent_case_url(api_url: str, session: requests.Session, sagsnummer: str) -> str:
    """Sagens SharePoint-URL (ows_CaseUrl) fra GO's metadata."""
    resp = session.get(f"{api_url}/_goapi/Cases/Metadata/{sagsnummer}/False", timeout=120)
    if 400 <= resp.status_code < 500:
        raise BusinessError(f"GO kender ikke sagen {sagsnummer} (HTTP {resp.status_code}).")
    resp.raise_for_status()
    case_url = ET.fromstring(resp.json()["Metadata"]).attrib.get("ows_CaseUrl")
    if not case_url:
        raise RuntimeError(f"GO-sagen {sagsnummer} har ingen CaseUrl i metadata.")
    return case_url


def _request_form_digest(api_url: str, case_url: str, session: requests.Session) -> str:
    session.headers.update({"Accept": "application/json; odata=verbose"})
    resp = session.post(f"{api_url}/{case_url}/_api/contextinfo", timeout=120)
    resp.raise_for_status()
    return resp.json()["d"]["GetContextWebInformation"]["FormDigestValue"]


def _odata_string_literal(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _alias_url(base_url: str, aliases: dict) -> str:
    query = "&".join(f"{name}={quote(_odata_string_literal(value), safe=chr(39) + '/-_.~')}"
                     for name, value in aliases.items())
    return f"{base_url}?{query}"


def _document_folder_url(case_url: str, folder_path: str | None) -> str:
    root_folder = f"/{str(case_url).strip('/')}/Dokumenter"
    normalized = (folder_path or "").replace("\\", "/").strip("/")
    return f"{root_folder}/{normalized}" if normalized else root_folder


def _chunked_file_upload(api_url, case_url, binary: bytes, file_name, session: requests.Session,
                         request_digest, folder_path, log) -> None:
    """Opretter filen og uploader den i bidder (startUpload / continueUpload / finishUpload)."""
    session.headers.update({"X-FORMS_BASED_AUTH_ACCEPTED": "f", "X-RequestDigest": request_digest})
    web_url = f"{api_url}/{str(case_url).strip('/')}"
    target_folder_url = _document_folder_url(case_url, folder_path)

    create_url = _alias_url(
        f"{web_url}/_api/web/GetFolderByServerRelativePath(DecodedUrl=@p)/Files/add(url=@f,overwrite=true)",
        {"@p": target_folder_url, "@f": file_name})
    session.post(create_url, timeout=300).raise_for_status()

    target_url = f"{target_folder_url}/{file_name}"
    upload_id = str(uuid.uuid4())
    total = len(binary)
    offset = 0

    def kald(metode: str, data: bytes):
        url = _alias_url(f"{web_url}/_api/web/GetFileByServerRelativePath(DecodedUrl=@u)/{metode}",
                         {"@u": target_url})
        session.post(url, data=data, timeout=900).raise_for_status()

    with io.BytesIO(binary) as stream:
        first = True
        while True:
            buffer = stream.read(CHUNK_BYTES)
            if not buffer:
                break
            if first and len(buffer) == total:          # hele filen i ét stykke
                kald(f"startUpload(uploadId=guid'{upload_id}')", buffer)
                kald(f"finishUpload(uploadId=guid'{upload_id}',fileOffset={offset})", buffer)
                break
            if first:
                kald(f"startUpload(uploadId=guid'{upload_id}')", buffer)
                first = False
            elif stream.tell() == total:
                kald(f"finishUpload(uploadId=guid'{upload_id}',fileOffset={offset})", buffer)
            else:
                kald(f"continueUpload(uploadId=guid'{upload_id}',fileOffset={offset})", buffer)
            offset += len(buffer)
            log(f"Uploadet {offset / 1024 / 1024:.0f} af {total / 1024 / 1024:.0f} MB")


def _find_docid(file_name, api_url, case_url, folder_path, session: requests.Session, log):
    """Finder GO's DocId for filen i sagens dokumentbibliotek (som get_docid i Aktbob)."""
    resp = session.get(f"{api_url}/{case_url}/_goapi/Administration/GetLeftMenuCounter", timeout=120)
    resp.raise_for_status()
    view_id = next((i.get("ViewId") for i in resp.json()
                    if i.get("ViewName") == "AllItems.aspx" and i.get("ListName") == "Dokumenter"), None)
    if view_id is None:
        raise RuntimeError("ViewId for AllItems.aspx blev ikke fundet på sagen.")

    list_url = f"'/{case_url}/Dokumenter'"
    root_folder = _document_folder_url(case_url, folder_path)
    url = (f"{api_url}/{case_url}/_api/web/GetList(@listUrl)/RenderListDataAsStream"
           f"?@listUrl={list_url}&View={view_id}&RootFolder={root_folder}")
    payload = json.dumps({"parameters": {
        "__metadata": {"type": "SP.RenderListDataParameters"},
        "ViewXml": "<View><Query><Where><Eq><FieldRef Name=\"UniqueId\" />"
                   f"<Value Type=\"Guid\">{uuid.uuid4()}</Value></Eq></Where></Query>"
                   "<RowLimit Paged=\"TRUE\">100</RowLimit></View>"}})
    while True:
        resp = session.post(url, headers={"content-type": "application/json;odata=verbose"},
                            data=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        for row in data.get("Row", []):
            if str(row.get("FileLeafRef")).lower() == str(file_name).lower():
                return row.get("DocID")
        next_href = data.get("NextHref")
        if not next_href:
            return None
        url = (f"{api_url}/{case_url}/_api/web/GetList(@listUrl)/RenderListDataAsStream"
               f"?@listUrl={list_url}{next_href.replace('?', '&', 1)}")
        log("Henter næste side af dokumentlisten")


def _opdater_metadata(api_url: str, doc_id, session: requests.Session, metadata_xml: str) -> None:
    """Sætter metadata på et chunk-uploadet dokument. Endpointet vil have datoen som
    mm-dd-åååå, så ows_Dato vendes (som update_metadata i Aktbob-robotten)."""
    m = re.search(r'ows_Dato="(\d{2})-(\d{2})-(\d{4})"', metadata_xml)
    if m:
        metadata_xml = metadata_xml.replace(m.group(0), f'ows_Dato="{m.group(2)}-{m.group(1)}-{m.group(3)}"')
    _go_post_med_retries(session, f"{api_url}/_goapi/Documents/Metadata", f"metadata for DocId {doc_id}",
                         data={"DocId": doc_id, "MetadataXml": metadata_xml})
