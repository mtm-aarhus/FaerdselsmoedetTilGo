"""faerdselsmoede_api.py - henter de færdige dokumenter fra Færdselsmødets web-app.

Sitet bygger selv dagsorden (PDF) og referat med bilag (ZIP) - præcis de dokumenter,
deltagerne fik - og udleverer dem via sit API (dokumentation: docs/API.md i web-appen).
Robotten skal derfor ikke have adgang til sitets database eller bilagsmappe.

Credential i OpenOrchestrator (CRED_FAERDSELSMOEDE_API i process.py):
    Username = API'ets base-URL, fx "https://faerdselsmoedet.adm.aarhuskommune.dk/api/v1"
    Password = API-nøglen (samme værdi som FAERDSELSMOEDE_API_KEY på web-serveren)

Fejl:
    BusinessError  -> mødet findes ikke, eller bilag mangler på web-serveren (409).
                      Kan ikke lykkes ved at prøve igen, før nogen har rettet det.
    RuntimeError   -> forkert nøgle/URL, API'et slået fra, serveren nede eller en fil
                      kom ikke helt frem. Frameworket prøver igen og sender fejlmail.
"""
import hashlib
import time

import requests

from robot_framework.exceptions import BusinessError

TIMEOUT_KORT = (10, 60)     # (forbind, svar) i sekunder
TIMEOUT_FIL = (10, 900)     # en referat-ZIP med store bilag kan tage tid at bygge og hente
FORSOEG = 3
PAUSE_SEK = 10

_BUSINESS_FEJL = {"moede_findes_ikke", "bilag_mangler", "bilag_findes_ikke", "fil_mangler"}
_DOKUMENTER = {"dagsorden": "dagsorden.pdf", "referat": "referat.zip"}


class FaerdselsmoedeApi:
    """Tynd klient til /api/v1. Én instans pr. køelement."""

    def __init__(self, base_url: str, api_noegle: str):
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url.lower().startswith(("http://", "https://")):
            raise RuntimeError(f"Credential'ens Username skal være API'ets base-URL (fx https://…/api/v1), "
                               f"men er '{base_url}'.")
        if not api_noegle:
            raise RuntimeError("Credential'ens Password (API-nøglen) er tomt.")
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_noegle

    # -- Lavniveau ----------------------------------------------------------

    def _get(self, url: str, timeout, hvad: str) -> requests.Response:
        """GET med retry ved netværksfejl og 5xx. 4xx oversættes til BusinessError/RuntimeError."""
        sidste_fejl = None
        for forsoeg in range(1, FORSOEG + 1):
            try:
                resp = self.session.get(url, timeout=timeout)
            except requests.exceptions.RequestException as e:
                sidste_fejl = f"{e.__class__.__name__}: {e}"
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code < 500:
                    self._kast_klientfejl(resp, hvad)
                sidste_fejl = f"HTTP {resp.status_code}"
            if forsoeg < FORSOEG:
                time.sleep(PAUSE_SEK * forsoeg)
        raise RuntimeError(f"Færdselsmødets API svarede ikke ved {hvad} efter {FORSOEG} forsøg ({sidste_fejl}).")

    @staticmethod
    def _kast_klientfejl(resp: requests.Response, hvad: str):
        try:
            data = resp.json()
        except ValueError:
            data = {}
        kode, besked = data.get("fejl", ""), data.get("besked", resp.text[:200])
        if kode in _BUSINESS_FEJL:
            mangler = data.get("mangler") or []
            if mangler:
                besked += " Mangler: " + ", ".join(mangler[:5]) + (" …" if len(mangler) > 5 else "")
            raise BusinessError(f"{hvad}: {besked}")
        if resp.status_code == 401:
            raise RuntimeError("Færdselsmødets API afviste nøglen (401). Tjek at credential'ens Password er "
                               "den samme som FAERDSELSMOEDE_API_KEY på web-serveren.")
        raise RuntimeError(f"Færdselsmødets API svarede {resp.status_code} ved {hvad}: {besked}")

    # -- Endpoints ----------------------------------------------------------

    def status(self) -> dict:
        """Tjekker forbindelse og nøgle. Kaster RuntimeError, hvis noget er galt."""
        return self._get(f"{self.base_url}/status", TIMEOUT_KORT, "statuskald").json()

    def moede_info(self, meeting_id: str) -> dict:
        """Mødets nummer, label, dato og status (se docs/API.md)."""
        return self._get(f"{self.base_url}/moeder/{meeting_id}", TIMEOUT_KORT,
                         f"mødet {meeting_id}").json()

    def hent_dokument(self, meeting_id: str, document_type: str) -> tuple[bytes, str]:
        """Henter dagsordenen (PDF) eller referatet med bilag (ZIP) fra sitet.
        Returnerer (indhold, filtype). Tjekker SHA-256, så en afbrudt overførsel opdages."""
        sti = _DOKUMENTER[document_type]
        hvad = f"{document_type} for møde {meeting_id}"
        for forsoeg in range(1, FORSOEG + 1):
            resp = self._get(f"{self.base_url}/moeder/{meeting_id}/{sti}", TIMEOUT_FIL, hvad)
            forventet = resp.headers.get("X-Content-SHA256", "").lower()
            if not forventet or hashlib.sha256(resp.content).hexdigest() == forventet:
                return resp.content, sti.rsplit(".", 1)[1]
            if forsoeg < FORSOEG:
                time.sleep(PAUSE_SEK * forsoeg)
        raise RuntimeError(f"{hvad} kom ikke helt frem (SHA-256 passer ikke) efter {FORSOEG} forsøg.")
