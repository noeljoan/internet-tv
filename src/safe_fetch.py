"""
safe_fetch.py

SSRF-sichere HTTP-Fetch-Funktion fuer internet-tv.

Loest zwei Probleme:
  1. Validation-to-use gap bei Redirects: requests folgt Redirects automatisch,
     ohne die neue Ziel-Adresse erneut zu pruefen.
  2. DNS-Rebinding: Zwischen Pruefung (DNS-Aufloesung) und tatsaechlicher
     Verbindung kann sich die IP hinter einem Hostnamen aendern.

Loesungsansatz:
  - Fuer jede URL (initiale + jeden Redirect-Hop) wird der Hostname selbst
    aufgeloest, JEDE zurueckgegebene IP validiert (nicht nur die erste), und
    die Verbindung wird explizit an die geprueften IP(s) gebunden -
    per HTTPAdapter/HTTPConnectionPool, der den Socket-Connect auf eine
    fixe IP umleitet, waehrend Host-Header/TLS-SNI korrekt bleiben.
  - allow_redirects=False; Redirects werden manuell verarbeitet und jede
    neue Location erneut komplett durchlaufen (kein "Vertrauen" auf alte
    Pruefung).

Verwendung in app.py:

    from safe_fetch import safe_fetch, SSRFValidationError

    try:
        resp = safe_fetch(user_supplied_url, stream=True, timeout=10)
    except SSRFValidationError as e:
        abort(400, description=str(e))

Ersetzt die bisherigen requests.get()-Aufrufe an beiden Sinks
(app.py:266 und app.py:346). Beide sollten diese eine Funktion nutzen,
damit es keine Inkonsistenzen zwischen den Endpunkten gibt.
"""

import ipaddress
import socket
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

try:
    # urllib3 v1/v2 kompatibel
    from urllib3.util import connection as urllib3_connection
except ImportError:  # pragma: no cover
    urllib3_connection = None


MAX_REDIRECTS = 5
ALLOWED_SCHEMES = {"http", "https"}
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10


class SSRFValidationError(Exception):
    """Wird geworfen, wenn eine URL / IP nicht als sicheres Upstream-Ziel gilt."""


def _is_global_ip(ip_str: str) -> bool:
    """
    Prueft, ob eine IP eine oeffentliche, global routbare Adresse ist.
    Blockiert explizit: loopback, private, link-local (inkl. Cloud-Metadaten
    169.254.169.254), multicast, reserved, unspecified.
    """
    ip = ipaddress.ip_address(ip_str)
    if ip.is_loopback:
        return False
    if ip.is_private:
        return False
    if ip.is_link_local:
        return False
    if ip.is_multicast:
        return False
    if ip.is_reserved:
        return False
    if ip.is_unspecified:
        return False
    # IPv4-mapped IPv6 (::ffff:127.0.0.1 etc.) ebenfalls pruefen
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _is_global_ip(str(ip.ipv4_mapped))
    return ip.is_global


def resolve_all_ips(hostname: str) -> list[str]:
    """
    Loest einen Hostnamen zu ALLEN zurueckgegebenen IPs auf (v4 und v6).
    Wichtig: nicht nur die erste IP pruefen - ein Angreifer koennte
    mehrere A-Records setzen, von denen nur einer oeffentlich ist.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFValidationError(f"DNS-Aufloesung fehlgeschlagen fuer {hostname}: {exc}")

    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise SSRFValidationError(f"Keine IP-Adressen fuer {hostname} gefunden")
    return ips


def validate_url_and_resolve(url: str) -> tuple[str, list[str]]:
    """
    Validiert Schema und Host einer URL und loest sie zu geprueften,
    ausschliesslich globalen IPs auf. Wirft SSRFValidationError, sobald
    IRGENDEINE aufgeloeste IP nicht global ist (fail-closed, nicht nur
    die erste pruefen).

    Gibt (hostname, liste_geprueften_ips) zurueck.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFValidationError(f"Nicht erlaubtes Schema: {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFValidationError("URL enthaelt keinen Hostnamen")

    # Falls der Hostname selbst schon eine IP-Literal ist (z.B. http://127.0.0.1/),
    # direkt pruefen statt DNS aufzuloesen.
    try:
        literal_ip = ipaddress.ip_address(hostname.strip("[]"))
        if not _is_global_ip(str(literal_ip)):
            raise SSRFValidationError(f"IP-Literal ist nicht global routbar: {literal_ip}")
        return hostname, [str(literal_ip)]
    except ValueError:
        pass  # kein IP-Literal, normal per DNS aufloesen

    ips = resolve_all_ips(hostname)
    for ip in ips:
        if not _is_global_ip(ip):
            raise SSRFValidationError(
                f"Aufgeloeste IP {ip} fuer Host {hostname!r} ist nicht global routbar"
            )
    return hostname, ips


class _PinnedIPAdapter(HTTPAdapter):
    """
    HTTPAdapter, der den TCP-Connect zwingt, eine der vorab geprueften IPs
    zu verwenden - unabhaengig davon, was ein erneuter DNS-Lookup zur
    Connect-Zeit liefern wuerde. Das schliesst das DNS-Rebinding-Fenster:
    die IP, die geprueft wurde, ist garantiert die IP, zu der verbunden wird.

    Host-Header und TLS-SNI bleiben unveraendert (Hostname), nur der
    Socket-Connect wird umgeleitet.
    """

    def __init__(self, hostname: str, pinned_ips: list[str], *args, **kwargs):
        self._hostname = hostname
        self._pinned_ips = pinned_ips
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        # Wir patchen urllib3's DNS-Aufloesung fuer die Dauer dieses einen
        # Requests, sodass jeder Connect-Versuch fuer self._hostname
        # ausschliesslich die vorab geprueften, garantiert globalen IPs
        # erhaelt - egal was ein neuer DNS-Lookup jetzt liefern wuerde.
        original_resolver = None
        if urllib3_connection is not None:
            original_resolver = urllib3_connection.create_connection

            def pinned_create_connection(address, *a, **kw):
                host, port = address
                if host == self._hostname:
                    last_exc = None
                    for ip in self._pinned_ips:
                        try:
                            return original_resolver((ip, port), *a, **kw)
                        except OSError as exc:
                            last_exc = exc
                            continue
                    raise last_exc or OSError(
                        f"Konnte keine der gepinnten IPs fuer {host} erreichen"
                    )
                return original_resolver(address, *a, **kw)

            urllib3_connection.create_connection = pinned_create_connection

        try:
            return super().send(request, **kwargs)
        finally:
            if original_resolver is not None:
                urllib3_connection.create_connection = original_resolver


def safe_fetch(url: str, method: str = "GET", **kwargs) -> requests.Response:
    """
    SSRF-sicherer Ersatz fuer requests.get()/requests.request().

    - Validiert jede URL (initial + jeder Redirect-Hop) gegen private/
      loopback/link-local/reserved Ziele.
    - Pinnt die Verbindung an die geprueften IPs (schliesst DNS-Rebinding).
    - Verarbeitet Redirects manuell (allow_redirects=False), max.
      MAX_REDIRECTS Hops, jeder Hop wird komplett neu validiert.

    Wirft SSRFValidationError, wenn irgendein Hop nicht global routbar ist,
    zu viele Redirects auftreten, oder das Schema nicht erlaubt ist.
    """
    current_url = url
    kwargs.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
    kwargs["allow_redirects"] = False  # wir uebernehmen Redirects manuell

    for _ in range(MAX_REDIRECTS + 1):
        hostname, pinned_ips = validate_url_and_resolve(current_url)

        session = requests.Session()
        adapter = _PinnedIPAdapter(hostname, pinned_ips)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        response = session.request(method, current_url, **kwargs)

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            if not location:
                raise SSRFValidationError("Redirect ohne Location-Header erhalten")
            # Relative Redirects gegen die aktuelle URL aufloesen
            current_url = requests.compat.urljoin(current_url, location)
            response.close()
            continue

        return response

    raise SSRFValidationError(f"Zu viele Redirects (> {MAX_REDIRECTS})")
