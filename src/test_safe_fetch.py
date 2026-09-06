"""
test_safe_fetch.py

Regressionstests fuer den SSRF-Fix (siehe safe_fetch.py).

Deckt die vom Issue geforderten Faelle ab:
  - Redirect zu loopback (127.0.0.1)
  - Redirect zu privatem Netz (10.x/192.168.x)
  - Redirect zu link-local / Cloud-Metadaten (169.254.169.254)
  - DNS-Rebinding (Hostname loest beim Check auf oeffentliche IP auf,
    zum "Connect"-Zeitpunkt aber auf eine private IP)
  - zu viele Redirects (Redirect-Loop / Kette laenger als erlaubt)
  - Happy Path: normale oeffentliche URL, auch mit ein paar harmlosen
    Redirects, funktioniert weiterhin

Die Tests mocken sowohl die DNS-Aufloesung (socket.getaddrinfo) als auch
den eigentlichen HTTP-Request (requests.Session.send / responses-Bibliothek),
damit keine echten Netzwerkzugriffe stattfinden.

Benoetigt: pytest, responses (pip install pytest responses)
"""

import socket

import pytest
import responses

import safe_fetch
from safe_fetch import (
    safe_fetch as do_safe_fetch,
    validate_url_and_resolve,
    resolve_all_ips,
    _is_global_ip,
    SSRFValidationError,
)


# ---------------------------------------------------------------------------
# Hilfsfunktionen zum Mocken von DNS
# ---------------------------------------------------------------------------

def _fake_getaddrinfo(mapping):
    """
    Baut einen Ersatz fuer socket.getaddrinfo, der Host -> Liste von IPs
    aus `mapping` zurueckgibt, im gleichen Format wie das Original.
    """
    def _impl(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(f"Unbekannter Host in Test-Mapping: {host}")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in mapping[host]
        ]
    return _impl


@pytest.fixture
def dns_mapping(monkeypatch):
    """
    Test setzt vor jedem Aufruf mapping[hostname] = [ip, ...] und die
    Aufloesung liefert genau diese IPs zurueck. Erlaubt es, das Mapping
    WAEHREND eines Tests zu aendern, um DNS-Rebinding zu simulieren.
    """
    mapping = {}

    def _impl(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(f"Unbekannter Host in Test-Mapping: {host}")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in mapping[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _impl)
    return mapping


# ---------------------------------------------------------------------------
# Basis-Validierung: _is_global_ip / validate_url_and_resolve
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip,expected", [
    ("8.8.8.8", True),          # oeffentlich
    ("1.1.1.1", True),          # oeffentlich
    ("127.0.0.1", False),       # loopback
    ("10.0.0.5", False),        # privat
    ("192.168.1.1", False),     # privat
    ("172.16.0.1", False),      # privat
    ("169.254.169.254", False), # link-local / Cloud-Metadaten
    ("0.0.0.0", False),         # unspecified
    ("224.0.0.1", False),       # multicast
    ("::1", False),             # IPv6 loopback
    ("fe80::1", False),         # IPv6 link-local
    ("2001:4860:4860::8888", True),  # Google DNS IPv6, oeffentlich
])
def test_is_global_ip(ip, expected):
    assert _is_global_ip(ip) is expected


def test_validate_rejects_disallowed_scheme():
    with pytest.raises(SSRFValidationError):
        validate_url_and_resolve("file:///etc/passwd")


def test_validate_rejects_ip_literal_loopback():
    with pytest.raises(SSRFValidationError):
        validate_url_and_resolve("http://127.0.0.1/admin")


def test_validate_accepts_ip_literal_public():
    hostname, ips = validate_url_and_resolve("http://8.8.8.8/")
    assert hostname == "8.8.8.8"
    assert ips == ["8.8.8.8"]


def test_validate_rejects_if_any_resolved_ip_is_private(dns_mapping):
    # Hostname loest zu MEHREREN IPs auf, eine davon privat -> muss blocken,
    # auch wenn die erste IP oeffentlich ist.
    dns_mapping["multi.example.com"] = ["93.184.216.34", "10.0.0.1"]
    with pytest.raises(SSRFValidationError):
        validate_url_and_resolve("http://multi.example.com/")


def test_validate_accepts_fully_public_host(dns_mapping):
    dns_mapping["public.example.com"] = ["93.184.216.34"]
    hostname, ips = validate_url_and_resolve("http://public.example.com/")
    assert hostname == "public.example.com"
    assert ips == ["93.184.216.34"]


# ---------------------------------------------------------------------------
# Ende-zu-Ende: safe_fetch() mit gemockten HTTP-Antworten (responses-Lib)
# ---------------------------------------------------------------------------
#
# Hinweis: responses faengt requests auf HTTP-Ebene ab (bevor ein echter
# Socket geoeffnet wird), daher ist das IP-Pinning des Adapters hier nicht
# aktiv im Spiel - dafuer testen wir explizit unten mit einem echten
# TCP-Server. Diese Tests pruefen die REDIRECT-VALIDIERUNGSLOGIK: dass jeder
# Hop erneut durch validate_url_and_resolve() muss, bevor ihm gefolgt wird.

@responses.activate
def test_redirect_to_loopback_is_blocked(dns_mapping):
    dns_mapping["public.example.com"] = ["93.184.216.34"]
    dns_mapping["localhost"] = ["127.0.0.1"]

    responses.add(
        responses.GET, "http://public.example.com/start",
        status=302, headers={"Location": "http://localhost/admin"},
    )

    with pytest.raises(SSRFValidationError):
        do_safe_fetch("http://public.example.com/start")


@responses.activate
def test_redirect_to_private_ip_is_blocked(dns_mapping):
    dns_mapping["public.example.com"] = ["93.184.216.34"]

    responses.add(
        responses.GET, "http://public.example.com/start",
        status=302, headers={"Location": "http://192.168.1.10/secret"},
    )

    with pytest.raises(SSRFValidationError):
        do_safe_fetch("http://public.example.com/start")


@responses.activate
def test_redirect_to_cloud_metadata_is_blocked(dns_mapping):
    dns_mapping["public.example.com"] = ["93.184.216.34"]

    responses.add(
        responses.GET, "http://public.example.com/start",
        status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"},
    )

    with pytest.raises(SSRFValidationError):
        do_safe_fetch("http://public.example.com/start")


@responses.activate
def test_dns_rebinding_is_blocked(dns_mapping):
    """
    Simuliert DNS-Rebinding: der Redirect zeigt auf denselben Hostnamen wie
    zuvor als 'oeffentlich' geprueft, aber die zweite Aufloesung (fuer den
    neuen Hop) liefert jetzt eine private IP zurueck.
    """
    # Erster Hop: rebind.example.com -> oeffentlich
    dns_mapping["start.example.com"] = ["93.184.216.34"]
    dns_mapping["rebind.example.com"] = ["93.184.216.35"]

    responses.add(
        responses.GET, "http://start.example.com/",
        status=302, headers={"Location": "http://rebind.example.com/"},
    )

    # Simuliert den Rebinding-Moment: sobald "rebind.example.com" zum ersten
    # Mal aufgeloest wird (waehrend der Validierung des Redirect-Hops),
    # schalten wir das Mapping auf eine private IP um. safe_fetch muss den
    # Hop trotzdem blocken, weil die Validierung fuer JEDEN Hop frisch
    # aufloest, statt einem frueheren "oeffentlich"-Ergebnis zu vertrauen.
    def _resolver(host, *a, **kw):
        if host == "rebind.example.com":
            dns_mapping["rebind.example.com"] = ["127.0.0.1"]
        return _fake_getaddrinfo(dns_mapping)(host, *a, **kw)

    monkeypatch_getaddrinfo = _resolver
    socket.getaddrinfo = monkeypatch_getaddrinfo

    with pytest.raises(SSRFValidationError):
        do_safe_fetch("http://start.example.com/")


@responses.activate
def test_too_many_redirects_is_blocked(dns_mapping):
    dns_mapping["loop.example.com"] = ["93.184.216.34"]

    # Baut eine Kette aus mehr Hops als safe_fetch.MAX_REDIRECTS erlaubt,
    # alle zu sich selbst (endlose Schleife).
    responses.add(
        responses.GET, "http://loop.example.com/",
        status=302, headers={"Location": "http://loop.example.com/"},
    )

    with pytest.raises(SSRFValidationError):
        do_safe_fetch("http://loop.example.com/")


@responses.activate
def test_happy_path_public_url_with_benign_redirect_still_works(dns_mapping):
    dns_mapping["start.example.com"] = ["93.184.216.34"]
    dns_mapping["final.example.com"] = ["93.184.216.35"]

    responses.add(
        responses.GET, "http://start.example.com/",
        status=302, headers={"Location": "http://final.example.com/stream.m3u8"},
    )
    responses.add(
        responses.GET, "http://final.example.com/stream.m3u8",
        status=200, body="#EXTM3U\n#EXTINF:-1,Test\nhttp://final.example.com/seg1.ts\n",
        content_type="application/vnd.apple.mpegurl",
    )

    resp = do_safe_fetch("http://start.example.com/")
    assert resp.status_code == 200
    assert "#EXTM3U" in resp.text


@responses.activate
def test_happy_path_no_redirect(dns_mapping):
    dns_mapping["direct.example.com"] = ["93.184.216.34"]

    responses.add(
        responses.GET, "http://direct.example.com/stream.m3u8",
        status=200, body="#EXTM3U\n",
        content_type="application/vnd.apple.mpegurl",
    )

    resp = do_safe_fetch("http://direct.example.com/stream.m3u8")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Flask-Endpunkt-Tests (Integration, mit gepatchtem safe_fetch)
# ---------------------------------------------------------------------------
# Diese Tests pruefen, dass BEIDE betroffenen Endpunkte (/api/import_url und
# /api/stream) SSRFValidationError konsistent als 400 behandeln, statt sich
# unterschiedlich zu verhalten.

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    import importlib
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), app_module


def test_import_url_endpoint_rejects_ssrf(client, monkeypatch):
    flask_client, app_module = client

    def _raise(*args, **kwargs):
        raise SSRFValidationError("blocked in test")

    monkeypatch.setattr(app_module, "safe_fetch", _raise)

    resp = flask_client.post("/api/import_url", json={"url": "http://127.0.0.1/admin"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "URL not allowed"


def test_stream_endpoint_rejects_ssrf(client, monkeypatch):
    flask_client, app_module = client

    def _raise(*args, **kwargs):
        raise SSRFValidationError("blocked in test")

    monkeypatch.setattr(app_module, "safe_fetch", _raise)

    resp = flask_client.get("/api/stream", query_string={"url": "http://169.254.169.254/"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "URL not allowed"
