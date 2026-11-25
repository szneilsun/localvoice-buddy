import argparse
import base64
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


MAX_UPLOAD = 2 * 1024 * 1024
REQUEST_HEADERS = ("Content-Type", "X-Conversation-Id", "X-Voice-TTS", "X-Voice-Speaker", "X-Voice-Chat-Provider")
RESPONSE_HEADERS = (
    "Content-Type", "Cache-Control", "X-Content-Type-Options", "Permissions-Policy",
    "Cross-Origin-Resource-Policy", "Content-Security-Policy",
)
AUTH_REALM = 'Basic realm="LocalVoice"'


def gateway_address(config):
    network = ipaddress.IPv4Network(config["clientNetwork"])
    address = ipaddress.IPv4Address(config["lanIp"])
    port = int(config["port"])
    if not 1024 <= port <= 65535 or address not in network or not any(
        network.subnet_of(ipaddress.IPv4Network(candidate))
        for candidate in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
    ):
        raise ValueError("The gateway needs an IPv4 address and port in a private client network.")
    return str(address), port, network


def load_lan_config(path, backend_port):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if int(config["backendPort"]) == backend_port:
            gateway_address(config)
            return config
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return None


def gateway_ready(config, directory):
    address, port, _ = gateway_address(config)
    try:
        trust = ssl.create_default_context(cafile=str(directory / "localvoice-ca.pem"))
        connection = http.client.HTTPSConnection(address, port, timeout=1, context=trust)
        try:
            connection.request("GET", "/api/status")
            with connection.getresponse() as response:
                return response.status == 401 and response.getheader("WWW-Authenticate") == AUTH_REALM
        finally:
            connection.close()
    except (OSError, http.client.HTTPException):
        return False


class Gateway(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, config, directory):
        listen, port, self.client_network = gateway_address(config)
        self.backend_port = int(config["backendPort"])
        self.host = f"{listen}:{port}"
        self.origin = f"https://{self.host}"
        password = config["password"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{32,}", password):
            raise ValueError("A generated gateway password is required.")
        self.credential = "Basic " + base64.b64encode(f"voice:{password}".encode()).decode("ascii")
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self.tls.load_cert_chain(directory / "server.pem", directory / "server-key.pem")
        super().__init__((listen, port), Handler)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def verify_request(self, request, client_address):
        return ipaddress.IPv4Address(client_address[0]) in self.client_network

    def process_request_thread(self, request, client_address):
        # Handshake in the worker so one slow TLS client cannot stall new connections.
        request.settimeout(8)
        try:
            secure = self.tls.wrap_socket(request, server_side=True)
        except OSError:
            self.shutdown_request(request)
            return
        super().process_request_thread(secure, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def begin(self, status):
        self.send_response(status)
        self.send_header("Connection", "close")
        self.close_connection = True

    def reject(self, status, message, challenge=False):
        body = json.dumps({"error": message}).encode("utf-8")
        self.begin(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if challenge:
            self.send_header("WWW-Authenticate", AUTH_REALM)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        origin = self.headers.get("Origin")
        if (self.headers.get("Host") != self.server.host
                or self.headers.get("Sec-Fetch-Site") == "cross-site"
                or origin not in (None, self.server.origin)
                or (self.command == "POST" and origin != self.server.origin)):
            return self.reject(403, "Use the same-origin HTTPS address for LocalVoice.")
        supplied = self.headers.get("Authorization", "").encode("latin-1")
        if not hmac.compare_digest(supplied, self.server.credential.encode("ascii")):
            return self.reject(401, "LocalVoice login required.", challenge=True)
        path = urlsplit(self.path)
        if not self.path.startswith("/") or self.path.startswith("//") or path.scheme or path.netloc:
            return self.reject(400, "Invalid local path.")
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            return self.reject(400, "Unsupported message framing.")
        headers = {key: self.headers[key] for key in REQUEST_HEADERS if key in self.headers}
        body = None
        if self.command == "POST":
            size = self.headers.get("Content-Length", "0")
            if not re.fullmatch(r"\d{1,8}", size) or int(size) > MAX_UPLOAD:
                return self.reject(413, "Audio upload is too large.")
            body = self.rfile.read(int(size))
            if len(body) != int(size):
                return self.reject(400, "Incomplete audio upload.")
            headers["Origin"] = f"http://127.0.0.1:{self.server.backend_port}"

        conn = http.client.HTTPConnection("127.0.0.1", self.server.backend_port, timeout=185)
        try:
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException):
                return self.reject(502, "The Windows voice service is unavailable.")
            with response:
                self.begin(response.status)
                for key in RESPONSE_HEADERS:
                    value = response.getheader(key)
                    if value is not None:
                        self.send_header(key, value)
                if response.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                elif response.getheader("Content-Length") is not None:
                    self.send_header("Content-Length", response.getheader("Content-Length"))
                self.end_headers()
                self.relay(response)
        except (OSError, http.client.HTTPException):
            pass
        finally:
            conn.close()

    def relay(self, response):
        while chunk := response.read1(32768):
            if response.chunked:
                self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
            else:
                self.wfile.write(chunk)
        if response.chunked:
            self.wfile.write(b"0\r\n\r\n")


def main():
    parser = argparse.ArgumentParser(description="Authenticated HTTPS entry point for LocalVoice.")
    parser.add_argument("config", type=Path)
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    directory = args.config.resolve().parent
    with Gateway(json.loads(args.config.read_text(encoding="utf-8")), directory) as gateway:
        if not args.detach:
            print(f"LocalVoice HTTPS: {gateway.origin}", flush=True)
            gateway.serve_forever()
            return
        origin = gateway.origin
    with (directory / "gateway.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-u", str(Path(__file__).resolve()), str(args.config.resolve())],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    print(f"Gateway PID {process.pid} | {origin}")


if __name__ == "__main__":
    main()
