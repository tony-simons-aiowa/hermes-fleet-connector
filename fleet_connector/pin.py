"""TLS public-key pin of the origin as clients see it (SHA-256 of the DER SubjectPublicKeyInfo).

The connector connects to its own configured HTTPS origin so that a TLS-terminating front end
(for example a tailnet proxy) is pinned, not the loopback listener. Certificate verification is
switched off ONLY to read the presented certificate: nothing is sent over the connection, and the
result is a public value that goes into the pairing link. Trust is established by the phone, which
compares this pin to what it observes (and/or system trust).
"""
from __future__ import annotations

import base64
import hashlib
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization

PIN_CACHE_SECONDS = 600


class PinUnavailable(Exception):
    """The origin's certificate could not be read. Carries no host or certificate detail."""


def spki_pin_from_der_cert(der: bytes) -> str:
    cert = x509.load_der_x509_certificate(der)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.urlsafe_b64encode(hashlib.sha256(spki).digest()).decode().rstrip("=")


def fetch_pin(host: str, port: int, *, timeout: float = 5.0, server_name=None) -> str:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # read the presented certificate only; nothing is sent
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=server_name or host) as tls:
                der = tls.getpeercert(binary_form=True)
        if not der:
            raise PinUnavailable("no certificate presented")
        return spki_pin_from_der_cert(der)
    except PinUnavailable:
        raise
    except Exception:  # network, TLS or parse failure: never echo details
        raise PinUnavailable("origin certificate unavailable") from None


def fetch_origin_pin(origin: str, *, timeout: float = 5.0) -> str:
    parts = urlsplit(origin)
    if parts.scheme != "https" or not parts.hostname:
        raise PinUnavailable("https origin required")
    return fetch_pin(parts.hostname, parts.port or 443, timeout=timeout)


class PinCache:
    """Caches a successful pin for ``PIN_CACHE_SECONDS``; failures are never cached."""

    def __init__(self, fetch=fetch_origin_pin, *, clock=time.monotonic) -> None:
        self._fetch, self._clock = fetch, clock
        self._lock = threading.Lock()
        self._entry = None  # (origin, pin, expires)

    def get(self, origin: str) -> str:
        with self._lock:
            now = self._clock()
            if self._entry and self._entry[0] == origin and self._entry[2] > now:
                return self._entry[1]
        pin = self._fetch(origin)
        with self._lock:
            self._entry = (origin, pin, self._clock() + PIN_CACHE_SECONDS)
        return pin

    def peek(self, origin: str):
        with self._lock:
            if self._entry and self._entry[0] == origin and self._entry[2] > self._clock():
                return self._entry[1]
        return None
