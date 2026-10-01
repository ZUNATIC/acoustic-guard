"""Writes the local .env for this machine. Called by setup.sh / setup.ps1; safe to re-run.

    python scripts/configure.py                      # local use on this computer
    python scripts/configure.py --light              # no large second-opinion model
    python scripts/configure.py --lan                # reachable from other computers (HTTPS + token)
    python scripts/configure.py --lan --ip 192.168.1.20 --token my-token
"""
import argparse
import datetime
import ipaddress
import re
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
CERT_DIR = ROOT / "certs"
CERT = CERT_DIR / "acoustic-guard.crt"
KEY = CERT_DIR / "acoustic-guard.key"


def read_env() -> list[str]:
    if not ENV.exists():
        shutil.copyfile(EXAMPLE, ENV)
    return ENV.read_text(encoding="utf-8").splitlines()


def get(lines: list[str], key: str) -> str | None:
    for line in lines:
        m = re.match(rf"\s*ACOUSTIC_{key}\s*=\s*(.*)$", line)
        if m:
            return m.group(1).strip()
    return None


def put(lines: list[str], key: str, value: str) -> None:
    """Set ACOUSTIC_<key>, replacing an existing (or commented-out) line in place."""
    pattern = re.compile(rf"\s*#?\s*ACOUSTIC_{key}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"ACOUSTIC_{key}={value}"
            return
    lines.append(f"ACOUSTIC_{key}={value}")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int) -> int:
    for port in [preferred] + list(range(8011, 8100)):
        if port_free(port):
            return port
    return preferred


def lan_ip() -> str:
    # a UDP "connect" only selects the outgoing interface; nothing is sent
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def easy_token() -> str:
    letters = "abcdefghjkmnpqrstuvwxyz"   # no i, l, o: they are easy to misread
    return f"ag-{secrets.randbelow(9000) + 1000}-{''.join(secrets.choice(letters) for _ in range(4))}"


def make_cert(ip: str) -> None:
    CERT_DIR.mkdir(exist_ok=True)
    host = socket.gethostname()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        openssl = shutil.which("openssl")
        if not openssl:
            sys.exit("need either the 'cryptography' package or openssl to create a certificate")
        san = f"DNS:localhost,DNS:{host},IP:127.0.0.1,IP:{ip}"
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "365", "-nodes",
                        "-keyout", str(KEY), "-out", str(CERT), "-subj", "/CN=ExfilGuard Acoustic Guard",
                        "-addext", f"subjectAltName={san}"], check=True, capture_output=True)
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ExfilGuard Acoustic Guard")])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"), x509.DNSName(host),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.IPAddress(ipaddress.ip_address(ip)),
    ])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=365)).add_extension(san, critical=False)
            .sign(key, hashes.SHA256()))
    KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--light", action="store_true", help="disable the large second-opinion model")
    ap.add_argument("--lan", action="store_true", help="serve on the network over HTTPS with a token")
    ap.add_argument("--ip", help="LAN address other computers will use (detected if omitted)")
    ap.add_argument("--token", help="access token (generated if omitted)")
    ap.add_argument("--port", type=int, help="port (default 8001, or the next free one)")
    args = ap.parse_args()

    lines = read_env()
    current = int(get(lines, "PORT") or 8001)
    port = args.port or (current if port_free(current) else pick_port(current))
    if port != current and not args.port:
        print(f"[configure] port {current} is in use on this computer - using {port} instead")
    put(lines, "PORT", str(port))

    if args.light:
        put(lines, "VERIFIER_MODEL", "")

    summary = {"url": f"http://127.0.0.1:{port}", "token": None}
    if args.lan:
        ip = args.ip or lan_ip()
        token = args.token or get(lines, "API_TOKEN") or easy_token()
        make_cert(ip)
        put(lines, "HOST", "0.0.0.0")
        put(lines, "API_TOKEN", token)
        put(lines, "SSL_CERTFILE", "certs/acoustic-guard.crt")
        put(lines, "SSL_KEYFILE", "certs/acoustic-guard.key")
        summary = {"url": f"https://{ip}:{port}", "token": token}

    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[configure] wrote {ENV.name}")
    print(f"[configure] dashboard: {summary['url']}")
    if summary["token"]:
        print(f"[configure] access token: {summary['token']}")


if __name__ == "__main__":
    main()
