#!/usr/bin/env python3
"""
OpsUI 2.0 - lightweight ops console for a Docker Swarm manager.

  * Terminal    : real interactive PTY terminals over WebSocket
                  - "container" shell (docker CLI -> host engine, workspace mounted)
                  - "vm" shell (ssh to the VM itself, e.g. ocsautotest@10.104.10.88)
  * Commands    : custom commands saved on the VM (WORKSPACE/opsui-commands.json)
  * Live logs   : docker service logs -f / docker logs -f / tail -F <file>
  * Files       : edit files under FILES_ROOT (the whole VM when / is mounted at /host)

Python standard library only. Nothing runs unless a browser is connected;
every child process is killed as soon as its browser stream/terminal closes.
"""
import base64, fcntl, gzip, hashlib, hmac, json, mimetypes, os, pty, re, secrets, select
import shutil, signal, socket, struct, subprocess, sys, termios, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ------------------------------------------------------------------ config
WORKSPACE = os.path.realpath(os.environ.get("WORKSPACE", "/opt/docker"))
LOG_ROOT = os.path.realpath(os.environ.get("LOG_ROOT", "/logs"))
FILES_ROOT = os.path.realpath(os.environ.get("FILES_ROOT", "") or WORKSPACE)   # Files tab root, e.g. /host (= VM's /)
if not os.path.isdir(FILES_ROOT):
    FILES_ROOT = WORKSPACE
FILES_HOME = os.environ.get("FILES_HOME", "").strip("/")  # folder the Files tab opens in, relative to FILES_ROOT
PORT = int(os.environ.get("PORT", "3000"))
OPS_USER = os.environ.get("OPS_USER", "admin")
VM_SSH = os.environ.get("VM_SSH", "")                     # e.g. ocsautotest@10.104.10.88
MAX_STREAMS = int(os.environ.get("MAX_STREAMS", "3"))     # concurrent live log/run streams
MAX_TERMS = int(os.environ.get("MAX_TERMS", "4"))         # concurrent terminals
MAX_FILE_BYTES = 2 * 1024 * 1024
COMMANDS_FILE = os.path.join(WORKSPACE, "opsui-commands.json")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_COMMANDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "commands.default.json")
PING = b"::opsui-ping::\n"
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,200}$")
SSH_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}@[A-Za-z0-9.\-]{1,253}(:\d{1,5})?$")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _load_password():
    pf = os.environ.get("OPS_PASS_FILE")
    if pf and os.path.isfile(pf):
        with open(pf, "r", encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get("OPS_PASS", "")


OPS_PASS = _load_password()
if not OPS_PASS:
    sys.exit("Refusing to start: set OPS_PASS or OPS_PASS_FILE (docker secret).")
_EXPECTED_AUTH = "Basic " + base64.b64encode(f"{OPS_USER}:{OPS_PASS}".encode()).decode()

CHILD_ENV = dict(os.environ, GOMAXPROCS="1", GOGC="50")   # docker CLI (Go): 1 thread, small heap
for _k in ("OPS_PASS", "OPS_PASS_FILE"):
    CHILD_ENV.pop(_k, None)

_stream_slots = threading.BoundedSemaphore(MAX_STREAMS)
_term_slots = threading.BoundedSemaphore(MAX_TERMS)
_cache, _cache_lock = {}, threading.Lock()
_tokens, _tok_lock = {}, threading.Lock()                  # websocket tokens -> expiry
_cmd_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _lower_priority():
    try:
        os.nice(19 - os.nice(0))          # child processes: lowest CPU priority
    except Exception:
        pass


def safe_path(root, rel):
    p = os.path.realpath(os.path.join(root, (rel or "").lstrip("/\\")))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("path outside allowed root")
    return p


def cached(key, ttl, fn):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def run_quick(argv, timeout=10):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=CHILD_ENV, preexec_fn=_lower_priority)
        return [l for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def list_log_files():
    out = []
    if not os.path.isdir(LOG_ROOT):
        return out
    base = LOG_ROOT.count(os.sep)
    for dirpath, dirnames, filenames in os.walk(LOG_ROOT):
        if dirpath.count(os.sep) - base >= 2:
            dirnames[:] = []
        for fn in filenames:
            if fn.endswith((".log", ".txt", ".out")):
                out.append(os.path.relpath(os.path.join(dirpath, fn), LOG_ROOT))
                if len(out) >= 300:
                    return sorted(out)
    return sorted(out)


def new_token():
    tok = secrets.token_urlsafe(24)
    with _tok_lock:
        now = time.time()
        for k in [k for k, v in _tokens.items() if v < now]:
            del _tokens[k]
        _tokens[tok] = now + 12 * 3600
    return tok


def token_ok(tok):
    with _tok_lock:
        exp = _tokens.get(tok or "")
        return bool(exp and exp > time.time())


# ------------------------------------------------------------------ custom commands
def _clean_cmd(c, i):
    if not isinstance(c, dict):
        return None
    name = str(c.get("name", "")).strip()[:80]
    cmd = str(c.get("cmd", "")).rstrip()[:4000]
    if not name or not cmd.strip():
        return None
    where = c.get("where", "active")
    return {
        "id": str(c.get("id") or f"c{int(time.time()*1000)}{i}")[:40],
        "name": name,
        "group": str(c.get("group", "") or "General").strip()[:40],
        "cmd": cmd,
        "where": where if where in ("active", "container", "vm") else "active",
        "confirm": bool(c.get("confirm", False)),
    }


def load_commands():
    try:
        with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in (_clean_cmd(c, i) for i, c in enumerate(data if isinstance(data, list) else [])) if x]
    except FileNotFoundError:
        try:                                   # first run: show the bundled examples
            with open(DEFAULT_COMMANDS, "r", encoding="utf-8") as f:
                return [x for x in (_clean_cmd(c, i) for i, c in enumerate(json.load(f))) if x]
        except Exception:
            return []
    except Exception:
        return []


def save_commands(items):
    clean = [x for x in (_clean_cmd(c, i) for i, c in enumerate(items)) if x][:500]
    os.makedirs(os.path.dirname(COMMANDS_FILE), exist_ok=True)
    tmp = COMMANDS_FILE + ".tmp"
    with _cmd_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
        os.replace(tmp, COMMANDS_FILE)
    return clean


# ------------------------------------------------------------------ static files (gzip cached in memory)
_static = {}


def static_file(rel):
    rel = rel.lstrip("/") or "index.html"
    if rel in _static:
        return _static[rel]
    p = safe_path(STATIC_DIR, rel)
    with open(p, "rb") as f:
        raw = f.read()
    ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
    if p.endswith(".mjs") or p.endswith(".js"):
        ctype = "text/javascript"
    if ctype.startswith("text/") or ctype.endswith("javascript"):
        ctype += "; charset=utf-8"
    entry = (raw, gzip.compress(raw, 6), ctype)
    _static[rel] = entry
    return entry


# ------------------------------------------------------------------ websocket (RFC 6455, minimal)
def ws_send(sock, payload, opcode=0x2):
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    sock.sendall(hdr + payload)


class WSReader:
    """Incrementally parses client frames (always masked)."""
    def __init__(self):
        self.buf = b""

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            b = self.buf
            if len(b) < 2:
                break
            op, ln = b[0] & 0x0F, b[1] & 0x7F
            masked, i = b[1] & 0x80, 2
            if ln == 126:
                if len(b) < 4:
                    break
                ln, i = struct.unpack("!H", b[2:4])[0], 4
            elif ln == 127:
                if len(b) < 10:
                    break
                ln, i = struct.unpack("!Q", b[2:10])[0], 10
            if ln > 1 << 20:
                raise ValueError("frame too large")
            need = i + (4 if masked else 0) + ln
            if len(b) < need:
                break
            if masked:
                mask = b[i:i + 4]
                data = bytes(c ^ mask[j % 4] for j, c in enumerate(b[i + 4:need]))
            else:
                data = b[i:need]
            self.buf = b[need:]
            out.append((op, data))
        return out


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    server_version = "OpsUI"
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    # ---- plumbing
    def _authed(self):
        got = self.headers.get("Authorization", "")
        if hmac.compare_digest(got.encode(), _EXPECTED_AUTH.encode()):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="OpsUI"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > MAX_FILE_BYTES + 65536:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _same_origin(self):
        o = self.headers.get("Origin")
        if not o:
            return True
        return urlparse(o).netloc == self.headers.get("Host", "")

    def _static(self, rel):
        raw, gz, ctype = static_file(rel)
        use_gz = "gzip" in self.headers.get("Accept-Encoding", "")
        body = gz if use_gz else raw
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-cache" if rel in ("", "index.html") else "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, argv, cwd=None, show_exit=False):
        if not _stream_slots.acquire(blocking=False):
            return self._err(f"too many live streams (max {MAX_STREAMS}); close some tabs", 429)
        p = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True, env=CHILD_ENV,
                                     preexec_fn=_lower_priority)
            except Exception as e:
                self.wfile.write(f"[opsui] failed to start: {e}\n".encode())
                return
            fd = p.stdout.fileno()
            while True:
                ready, _, _ = select.select([fd], [], [], 25)   # sleeps in kernel: 0% CPU
                if ready:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                else:
                    self.wfile.write(PING)
            if show_exit:
                self.wfile.write(f"\n[exit code {p.wait()}]\n".encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if p and p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                    p.wait(timeout=3)
                except Exception:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except Exception:
                        pass
            if p:
                try:
                    p.stdout.close()
                    p.wait(timeout=1)
                except Exception:
                    pass
            _stream_slots.release()

    # ---- terminal over websocket
    def _terminal(self, q):
        if not token_ok(q.get("t")) or not self._same_origin():
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            return self._err("websocket required")
        kind = q.get("shell", "container")
        target = (q.get("target") or VM_SSH).strip()
        if kind == "vm":
            if not SSH_RE.match(target):
                return self._err("invalid ssh target (use user@host or user@host:port)")
            host, _, port = target.partition(":")
            argv = ["ssh", "-tt", "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "ServerAliveInterval=30", "-o", "UserKnownHostsFile=/tmp/opsui_known_hosts"]
            if port:
                argv += ["-p", port]
            argv.append(host)
        else:
            argv = ["/bin/bash", "-l"] if os.path.exists("/bin/bash") else ["/bin/sh", "-l"]
        if not _term_slots.acquire(blocking=False):
            return self._err(f"too many terminals open (max {MAX_TERMS})", 429)

        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        self.wfile.flush()
        sock = self.connection
        pid, fd = None, None
        try:
            cols = max(20, min(int(q.get("cols", "120") or 120), 500))
            rows = max(5, min(int(q.get("rows", "30") or 30), 200))
            pid, fd = pty.fork()
            if pid == 0:                                       # ---- child
                for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE, signal.SIGTSTP,
                            signal.SIGTTIN, signal.SIGTTOU, signal.SIGCHLD, signal.SIGHUP, signal.SIGTERM):
                    try:
                        signal.signal(sig, signal.SIG_DFL)     # real Ctrl+C / Ctrl+Z in the shell
                    except Exception:
                        pass
                try:
                    os.nice(5)
                    os.chdir(WORKSPACE if os.path.isdir(WORKSPACE) else "/")
                except Exception:
                    pass
                env = dict(CHILD_ENV, TERM="xterm-256color", COLORTERM="truecolor",
                           OPSUI_SHELL=kind, HOME=os.environ.get("HOME", "/root"))
                env.pop("GOGC", None)
                try:
                    os.execvpe(argv[0], argv, env)
                except Exception as e:
                    os.write(2, f"\r\n[opsui] cannot start {argv[0]}: {e}\r\n".encode())
                    os._exit(127)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            reader = WSReader()
            last_rx = time.monotonic()
            while True:
                r, _, _ = select.select([sock, fd], [], [], 30)
                if not r:                                      # idle: keepalive / dead client check
                    if time.monotonic() - last_rx > 95:
                        break
                    ws_send(sock, b"", 0x9)
                    continue
                if fd in r:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        ws_send(sock, b"\r\n\x1b[90m[session ended]\x1b[0m\r\n")
                        break
                    ws_send(sock, data)
                if sock in r:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    last_rx = time.monotonic()
                    closing = False
                    for op, data in reader.feed(chunk):
                        if op == 0x8:
                            closing = True
                        elif op == 0x9:
                            ws_send(sock, data, 0xA)
                        elif op in (0x1, 0x2):
                            try:
                                m = json.loads(data.decode("utf-8", "replace"))
                            except Exception:
                                continue
                            if m.get("t") == "i" and isinstance(m.get("d"), str):
                                os.write(fd, m["d"].encode("utf-8"))
                            elif m.get("t") == "r":
                                c = max(20, min(int(m.get("c", cols)), 500))
                                rr = max(5, min(int(m.get("r", rows)), 200))
                                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rr, c, 0, 0))
                    if closing:
                        break
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            try:
                ws_send(sock, b"", 0x8)
            except Exception:
                pass
            if pid:
                for sig in (signal.SIGHUP, signal.SIGTERM):
                    try:
                        os.killpg(pid, sig)
                    except Exception:
                        pass
                for _ in range(20):
                    try:
                        if os.waitpid(pid, os.WNOHANG)[0]:
                            break
                    except ChildProcessError:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    except Exception:
                        pass
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            _term_slots.release()
            self.close_connection = True

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/ws/term":                      # authenticated by short-lived token
            return self._terminal(q)
        if not self._authed():
            return
        try:
            if u.path in ("/", "/index.html") or u.path.startswith("/static/"):
                return self._static("index.html" if u.path in ("/", "/index.html") else u.path[len("/static/"):])
            if u.path == "/api/config":
                self._json({"vmSsh": VM_SSH, "token": new_token(), "workspace": WORKSPACE,
                            "maxTerms": MAX_TERMS, "user": OPS_USER, "filesHome": FILES_HOME})
            elif u.path == "/api/targets":
                self._json(cached("targets", 10, lambda: {
                    "services": run_quick(["docker", "service", "ls", "--format", "{{.Name}}"]),
                    "containers": run_quick(["docker", "ps", "--format", "{{.Names}}"]),
                    "files": list_log_files(),
                }))
            elif u.path == "/api/status":
                self._json(cached("status", 20, lambda: {
                    "nodes": run_quick(["docker", "node", "ls", "--format",
                                        "{{.Hostname}}\t{{.Status}}\t{{.Availability}}\t{{.ManagerStatus}}"]),
                    "services": run_quick(["docker", "service", "ls", "--format",
                                           "{{.Name}}\t{{.Replicas}}\t{{.Image}}"]),
                }))
            elif u.path == "/api/logs":
                kind, name = q.get("kind", ""), q.get("name", "")
                tail = str(max(0, min(int(q.get("tail", "200") or 200), 5000)))
                if kind == "service" and NAME_RE.match(name):
                    argv = ["docker", "service", "logs", "-f", "--timestamps", "--tail", tail, name]
                elif kind == "container" and NAME_RE.match(name):
                    argv = ["docker", "logs", "-f", "--timestamps", "--tail", tail, name]
                elif kind == "file":
                    argv = ["tail", "-n", tail, "-F", safe_path(LOG_ROOT, name)]
                else:
                    return self._err("invalid log target")
                self._stream(argv)
            elif u.path == "/api/ls":
                d = safe_path(FILES_ROOT, q.get("path", ""))
                if not os.path.isdir(d):
                    return self._err("not a folder")
                items = [{"name": e.name, "dir": e.is_dir()}
                         for e in sorted(os.scandir(d), key=lambda e: (not e.is_dir(), e.name.lower()))]
                rel = os.path.relpath(d, FILES_ROOT)
                self._json({"path": "" if rel == "." else rel.replace(os.sep, "/"), "items": items})
            elif u.path == "/api/file":
                p = safe_path(FILES_ROOT, q.get("path", ""))
                if os.path.getsize(p) > MAX_FILE_BYTES:
                    return self._err("file too large for the editor (2 MB)")
                with open(p, "rb") as f:
                    raw = f.read()
                if b"\0" in raw[:8192]:
                    return self._err("binary file - open it in the terminal instead")
                self._json({"content": raw.decode("utf-8", "replace")})
            elif u.path == "/api/commands":
                self._json(load_commands())
            else:
                self._err("not found", 404)
        except ValueError as e:
            self._err(str(e))
        except FileNotFoundError:
            self._err("not found", 404)
        except Exception as e:
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        if not self._authed():
            return
        if not self._same_origin():
            return self._err("cross-origin request blocked", 403)
        u = urlparse(self.path)
        try:
            data = self._body()
            if u.path == "/api/file":
                p = safe_path(FILES_ROOT, data.get("path", ""))
                if p == FILES_ROOT or os.path.isdir(p):
                    return self._err("path is a directory")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                existed = os.path.isfile(p)
                if existed:
                    shutil.copy2(p, p + ".bak")
                with open(p, "w", encoding="utf-8", newline="") as f:      # keeps owner/mode of existing files
                    f.write(data.get("content", ""))
                if not existed:                       # new file: same owner as its folder, not root
                    try:
                        st = os.stat(os.path.dirname(p))
                        os.chown(p, st.st_uid, st.st_gid)
                    except Exception:
                        pass
                self._json({"ok": True})
            elif u.path == "/api/commands":
                items = data.get("commands")
                if not isinstance(items, list):
                    return self._err("commands must be a list")
                self._json(save_commands(items))
            elif u.path == "/api/run":
                cmd = (data.get("cmd") or "").strip()
                if not cmd:
                    return self._err("empty command")
                cwd = safe_path(WORKSPACE, data.get("cwd", ""))
                self._stream(["/bin/sh", "-c", cmd], cwd=cwd, show_exit=True)
            else:
                self._err("not found", 404)
        except ValueError as e:
            self._err(str(e))
        except Exception as e:
            self._err(f"{type(e).__name__}: {e}", 500)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        os.nice(10)                         # the console always yields CPU to Bridge
    except Exception:
        pass
    threading.stack_size(512 * 1024)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"OpsUI 2.0 on :{PORT}  workspace={WORKSPACE}  files={FILES_ROOT}  logs={LOG_ROOT}  "
          f"vm_ssh={VM_SSH or '-'}", flush=True)
    srv.serve_forever(poll_interval=5)


if __name__ == "__main__":
    main()
