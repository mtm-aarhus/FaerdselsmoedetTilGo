"""faerdselsmoede_api.py - henter bilag fra Færdselsmødets web-app via dens fil-API.

Bilagene ligger på web-serverens lokale drev. Robotten kører på en anden server og
henter dem derfor over HTTP (dokumentation: docs/API.md i web-appen).

Credential i OpenOrchestrator (CRED_FAERDSELSMOEDE_API i process.py):
    Username = API'ets base-URL, fx "https://faerdselsmoede.aarhus.dk/api/v1"
    Password = API-nøglen (samme værdi som FAERDSELSMOEDE_API_KEY på web-serveren)

Fejl:
    BusinessError  -> mødet/bilaget findes ikke, eller en fil mangler på web-serveren.
                      Kan ikke lykkes ved at prøve igen.
    RuntimeError   -> forkert nøgle/URL, API'et slået fra, serveren nede eller en fil
                      kom ikke helt frem. Frameworket prøver igen og sender fejlmail.
"""
import hashlib
import time

import requests

from robot_framework.exceptions import BusinessError

TIMEOUT_LISTE = (10, 60)    # (forbind, svar) i sekunder
TIMEOUT_FIL = (10, 300)
FORSOEG = 3
PAUSE_SEK = 10

_BUSINESS_FEJL = {"moede_findes_ikke", "bilag_findes_ikke", "fil_mangler"}


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
        self.session.headers["Accept"] = "application/json"

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
            raise BusinessError(f"{hvad}: {besked}")
        if resp.status_code == 401:
            raise RuntimeError("Færdselsmødets API afviste nøglen (401). Tjek at credential'ens Password er "
                               "den samme som FAERDSELSMOEDE_API_KEY på web-serveren.")
        raise RuntimeError(f"Færdselsmødets API svarede {resp.status_code} ved {hvad}: {besked}")

    # -- Endpoints ----------------------------------------------------------

    def status(self) -> dict:
        """Tjekker forbindelse og nøgle. Kaster RuntimeError, hvis noget er galt."""
        data = self._get(f"{self.base_url}/status", TIMEOUT_LISTE, "statuskald").json()
        if not data.get("bilagsmappe_tilgaengelig", True):
            raise RuntimeError("Web-serverens bilagsmappe (UPLOAD_FOLDER) findes ikke.")
        return data

    def bilagsliste(self, meeting_id: str) -> dict:
        """Mødets bilag pr. dagsordenspunkt (se docs/API.md)."""
        return self._get(f"{self.base_url}/moeder/{meeting_id}/bilag", TIMEOUT_LISTE,
                         f"bilagslisten for møde {meeting_id}").json()

    def hent_fil(self, bilag: dict) -> bytes:
        """Henter én fil og tjekker SHA-256, så en afbrudt overførsel ikke ender i ZIP'en."""
        hvad = f"bilag {bilag['id']} ({bilag.get('original_filename')})"
        for forsoeg in range(1, FORSOEG + 1):
            resp = self._get(bilag["download_url"], TIMEOUT_FIL, hvad)
            forventet = resp.headers.get("X-Content-SHA256", "").lower()
            if not forventet or hashlib.sha256(resp.content).hexdigest() == forventet:
                return resp.content
            if forsoeg < FORSOEG:
                time.sleep(PAUSE_SEK * forsoeg)
        raise RuntimeError(f"{hvad} kom ikke helt frem (SHA-256 passer ikke) efter {FORSOEG} forsøg.")

    def hent_moedets_bilag(self, meeting_id: str) -> dict[str, list[tuple[str, bytes]]]:
        """Alle mødets bilag som {proposal_id: [(original_filename, indhold), ...]}.

        Kaster BusinessError, hvis web-serveren mangler en eller flere filer - så lægges
        der aldrig en ufuldstændig ZIP på GO."""
        liste = self.bilagsliste(meeting_id)
        mangler = [f"{p['nummer']}. {p['titel']}/{b['original_filename']}"
                   for p in liste["dagsordenspunkter"] for b in p["bilag"] if not b["findes"]]
        if mangler:
            raise BusinessError(f"{len(mangler)} bilag mangler på web-serveren: "
                                + ", ".join(mangler[:5]) + (" …" if len(mangler) > 5 else "")
                                + ". Upload dem igen i web-appen og læg elementet i køen igen.")
        resultat = {}
        for punkt in liste["dagsordenspunkter"]:
            resultat[punkt["proposal_id"]] = [(b["original_filename"], self.hent_fil(b)) for b in punkt["bilag"]]
        return resultat
