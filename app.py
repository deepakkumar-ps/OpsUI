#!/usr/bin/env python3
"""
OpsUI 1.3 - lightweight ops console for a Docker Swarm manager.

  * Terminal    : real interactive PTY terminals over WebSocket
                  - "container" shell (docker CLI -> host engine, workspace mounted)
                  - "vm" shell (ssh to the VM itself, e.g. ocsautotest@10.104.10.88)
  * Commands    : custom commands saved on the VM (WORKSPACE/opsui-commands.json)
  * Live logs   : docker service logs -f / docker logs -f / tail -F <file>  (running sources only)
  * Files       : browse, edit, upload (drag & drop), delete under FILES_ROOT
                  (the whole VM when / is mounted at /host)
  * Versions    : installed version of every running service (image tag / env / OCI label)

Python standard library only. Nothing runs unless a browser is connected;
every child process is killed as soon as its browser stream/terminal closes.
"""
import base64, fcntl, gzip, hashlib, hmac, json, mimetypes, os, pty, re, secrets, select
import shutil, signal, socket, struct, subprocess, sys, termios, threading, time
import urllib.error, urllib.parse, urllib.request
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
OPS_USER = os.environ.get("OPS_USER", "Secline")
VM_SSH = os.environ.get("VM_SSH", "")                     # e.g. ocsautotest@10.104.10.88
MAX_STREAMS = int(os.environ.get("MAX_STREAMS", "3"))     # concurrent live log/run streams
MAX_TERMS = int(os.environ.get("MAX_TERMS", "4"))         # concurrent terminals
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "512")) * 1024 * 1024   # per file (streamed to disk)
MAX_LS_ITEMS = 5000
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


REPLICAS_RE = re.compile(r"^(\d+)/(\d+)")


def list_services():
    """One `docker service ls` shared by /api/status and /api/targets (cached 10 s)."""
    def load():
        out = []
        for line in run_quick(["docker", "service", "ls", "--format", "{{.Name}}\t{{.Replicas}}\t{{.Image}}"]):
            name, reps, image = (line.split("\t") + ["", ""])[:3]
            m = REPLICAS_RE.match(reps)
            out.append({"name": name, "replicas": reps.split(" ")[0], "image": image,
                        "running": int(m[1]) if m else 0, "desired": int(m[2]) if m else 0})
        return sorted(out, key=lambda s: s["name"])
    return cached("services", 10, load)


def list_containers():
    """Running containers that are NOT swarm tasks (those are covered by their service)."""
    rows = run_quick(["docker", "ps", "--format", '{{.Names}}\t{{.Label "com.docker.swarm.service.name"}}'])
    return sorted(r.split("\t")[0] for r in rows if not r.partition("\t")[2].strip())


# ---- installed versions
_WORD = {"ui": "UI", "api": "API", "db": "DB", "mongodb": "MongoDB", "opsui": "OpsUI", "id": "ID"}
_VER_LABELS = ("org.opencontainers.image.version", "org.label-schema.version", "version", "VERSION")


def pretty_name(svc):
    """bridge_bridge-service -> Bridge Service, kafka_kafka-ui -> Kafka UI."""
    base = svc.split("_", 1)[1] if "_" in svc else svc
    return " ".join(_WORD.get(w.lower(), w.capitalize()) for w in re.split(r"[-_.]+", base) if w)


def _looks_like_version(v):
    return bool(v) and any(c.isdigit() for c in v)


def _image_tag(image):
    ref = image.split("@", 1)[0]                     # drop @sha256:... digest
    repo, _, tag = ref.rpartition(":")
    return tag if repo and "/" not in tag else ""    # "registry:5000/app" has no tag


# *_VERSION variables that describe the runtime/base image, not the application
_RUNTIME_ENV = re.compile(r"^(DOTNET|ASPNET|ASPNETCORE|NUGET|POWERSHELL|PYTHON|PIP|SETUPTOOLS|WHEEL|NODE|NPM|"
                          r"YARN|JAVA|JDK|JRE|GOLANG|GO|RUBY|GEM|BUNDLER|PHP|PERL|GPG|GOSU|TINI|OPENSSL|"
                          r"ALPINE|DEBIAN|UBUNTU|DOCKER|SCALA|SBT|GRADLE|MAVEN)_")
_GENERIC_ENV = ("", "APP", "APPLICATION", "SERVICE", "BUILD", "RELEASE", "IMAGE", "PRODUCT")


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_NUM_VER_RE = re.compile(r"^v?(\d+(?:\.\d+)+)([-+].*)?$")      # 1.2 / 2.1.5.3 / v1.0.0-rc1, not '260f9e8'
_BASE_SUFFIX = re.compile(r"^-(ubuntu|debian|alpine|bookworm|bullseye|buster|jammy|focal|noble|slim|jre|jdk)",
                          re.I)
_MANIFEST_TYPES = ("application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest."
                   "list.v2+json,application/vnd.docker.distribution.manifest.v2+json,"
                   "application/vnd.oci.image.manifest.v1+json")
_net = {"down_until": 0.0}                          # registry unreachable -> don't retry for 10 min


def _running_image(svc, image):
    """(labels, env, digests) of the image the service is actually running: the image of its task
    container on this node if there is one, else the image named in the service spec."""
    refs = []
    ids = run_quick(["docker", "ps", "-q", "--filter", f"label=com.docker.swarm.service.name={svc}"])
    if ids:
        refs += run_quick(["docker", "container", "inspect", "--format", "{{.Image}}", ids[0]])[:1]
    for ref in dict.fromkeys(refs + [image, image.split("@", 1)[0]]):
        out = run_quick(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}\t{{json .Config}}", ref])
        if out:
            digests, _, cfg = out[0].partition("\t")
            try:
                cfg = json.loads(cfg) or {}
            except ValueError:
                cfg = {}
            return cfg.get("Labels") or {}, cfg.get("Env") or [], set(_DIGEST_RE.findall(digests))
    return {}, [], set()


def _split_ref(image):
    """'atlasplatformdocker/bridge:latest@sha256:..' -> ('docker.io', 'atlasplatformdocker/bridge')."""
    name = image.split("@", 1)[0]
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        reg, parts = parts[0], parts[1:]
    else:
        reg = "docker.io"
    if reg in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        reg = "docker.io"
        if len(parts) == 1:
            parts = ["library"] + parts
    return reg, "/".join(parts)


def _clean_tag(tag):
    """'8.0.4-ubuntu2204' -> '8.0.4', 'v1.2.0' -> '1.2.0', '2.1.5.3-rc1' stays."""
    m = _NUM_VER_RE.match(tag)
    if not m:
        return tag
    return m[1] if not m[2] or _BASE_SUFFIX.match(m[2]) else m[1] + m[2]


def _best_tag(tags):
    """Most specific version among tags of the same image: 2.1.5.3 beats 2.1 and 2."""
    nums = [t for t in tags if _NUM_VER_RE.match(t)]
    if not nums:
        return ""
    return _clean_tag(max(nums, key=lambda t: (_NUM_VER_RE.match(t)[1].count("."), -len(t))))


def _http(url, token=None, method="GET"):
    h = {"User-Agent": "OpsUI", "Accept": _MANIFEST_TYPES + ",application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    return urllib.request.urlopen(urllib.request.Request(url, headers=h, method=method), timeout=6)


def _hub_version(repo, digests):
    """Docker Hub: find the version tag(s) whose digest is the one we run (newest tags first)."""
    url = f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&ordering=last_updated"
    for _ in range(3):
        with _http(url) as r:
            page = json.load(r)
        hits = [t["name"] for t in page.get("results") or []
                if ({t.get("digest")} | {i.get("digest") for i in t.get("images") or []}) & digests]
        if _best_tag(hits):
            return _best_tag(hits)
        url = page.get("next")
        if not url:
            break
    return ""


def _v2_version(registry, repo, digests):
    """Any OCI registry (ghcr.io, ...): list tags, compare manifest digests of the newest versions."""
    base, token = f"https://{registry}/v2/{repo}", None
    try:
        r = _http(base + "/tags/list?n=1000")
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return ""
        chal = dict(re.findall(r'(\w+)="([^"]*)"', e.headers.get("WWW-Authenticate", "")))
        if not chal.get("realm"):
            return ""
        q = {"service": chal.get("service", registry), "scope": chal.get("scope", f"repository:{repo}:pull")}
        with _http(chal["realm"] + "?" + urllib.parse.urlencode(q)) as tr:
            tok = json.load(tr)
        token = tok.get("token") or tok.get("access_token")
        r = _http(base + "/tags/list?n=1000", token)
    with r:
        tags = json.load(r).get("tags") or []
    vkey = lambda t: tuple(int(x) for x in _NUM_VER_RE.match(t)[1].split("."))
    for t in sorted((t for t in tags if _NUM_VER_RE.match(t)), key=vkey, reverse=True)[:30]:
        with _http(f"{base}/manifests/{t}", token, "HEAD") as m:
            if m.headers.get("Docker-Content-Digest") in digests:
                return _clean_tag(t)
    return ""


def registry_version(image, digests):
    """Version tag in the image's registry that points at exactly the image being run."""
    if not digests or time.monotonic() < _net["down_until"]:
        return ""
    reg, repo = _split_ref(image)
    try:
        return _hub_version(repo, digests) if reg == "docker.io" else _v2_version(reg, repo, digests)
    except urllib.error.HTTPError:
        return ""                                  # private / unknown repo: just skip it
    except Exception:
        _net["down_until"] = time.monotonic() + 600
        return ""


def _pick_version(svc, labels, env):
    words = {w for w in re.split(r"[-_.]+", svc.lower()) if len(w) > 2}
    named, generic = [], []
    for e in env:
        k, _, v = e.partition("=")
        k, v = k.upper(), v.strip()
        if not k.endswith("VERSION") or not _looks_like_version(v) or _RUNTIME_ENV.match(k):
            continue
        prefix = k[:-len("VERSION")].strip("_")
        p = prefix.lower()
        if p and any(w.startswith(p) or p.startswith(w) for w in words):   # BRIDGE_VERSION, MONGO_VERSION
            named.append(v)
        elif prefix in _GENERIC_ENV:                                          # VERSION, APP_VERSION
            generic.append(v)
    # an OCI version label inherited from the base OS (ubuntu 22.04, ...) is not the app's version
    if str(labels.get("org.opencontainers.image.ref.name", "")).lower() not in ("ubuntu", "debian", "alpine"):
        for k in _VER_LABELS:
            if _looks_like_version(str(labels.get(k, ""))):
                named.append(str(labels[k]))
    return (named + generic or [""])[0]


def service_version(svc, image, spec_env):
    """Version a service is running. `image` is the spec image incl. @sha256 digest, so the cache
    entry changes as soon as a new ':latest' is deployed.
      1. image tag, if it is a version (e.g. :2.1.5.3)
      2. registry: the version tag that points at the very same digest we run (:latest == :2.1.5.3)
      3. <SERVICE>_VERSION env var / image version label / VERSION env var
      4. the tag itself (e.g. 'latest')"""
    def load():
        tag = _image_tag(image)
        if _looks_like_version(tag):
            return tag
        labels, env, digests = _running_image(svc, image)
        return registry_version(image, digests | set(_DIGEST_RE.findall(image))) \
            or _pick_version(svc, labels, spec_env + env) or tag or "—"
    return cached(("ver", svc, image), 6 * 3600, load)


def running_versions():
    running = [s for s in list_services() if s["running"] > 0]
    if not running:
        return []
    # one docker call for the exact spec image (with digest) and env of every running service
    fmt = "{{.Spec.Name}}\t{{.Spec.TaskTemplate.ContainerSpec.Image}}\t{{json .Spec.TaskTemplate.ContainerSpec.Env}}"
    spec = {}
    for line in run_quick(["docker", "service", "inspect", "--format", fmt] + [s["name"] for s in running]):
        name, img, env = (line.split("\t") + ["", ""])[:3]
        try:
            spec[name] = (img, json.loads(env) or [])
        except ValueError:
            spec[name] = (img, [])
    out = []
    for s in running:
        img, env = spec.get(s["name"], (s["image"], []))
        out.append({"service": s["name"], "label": pretty_name(s["name"]), "image": img.split("@", 1)[0],
                    "version": service_version(s["name"], img or s["image"], env)})
    return out


# ---- file operations
def _own_like_parent(p):
    """New files/folders get the owner of their parent folder instead of root."""
    try:
        st = os.stat(os.path.dirname(p))
        os.chown(p, st.st_uid, st.st_gid)
    except Exception:
        pass


def make_dirs(d):
    """mkdir -p that keeps ownership consistent with the existing parent folder."""
    missing = []
    while not os.path.isdir(d):
        if os.path.lexists(d):
            raise ValueError(f"{os.path.relpath(d, FILES_ROOT)} exists and is not a folder")
        missing.append(d)
        d = os.path.dirname(d)
    for m in reversed(missing):
        try:
            os.mkdir(m)
        except FileExistsError:
            continue
        _own_like_parent(m)


def entry_path(rel):
    """Path of an entry itself (a symlink is NOT followed), checked to be inside FILES_ROOT."""
    rel = (rel or "").replace("\\", "/").strip("/")
    name = os.path.basename(rel)
    if not rel or name in ("", ".", ".."):
        raise ValueError("invalid path")
    return os.path.join(safe_path(FILES_ROOT, os.path.dirname(rel)), name)


def delete_entry(rel):
    p = entry_path(rel)
    home = os.path.realpath(os.path.join(FILES_ROOT, FILES_HOME))
    if (FILES_ROOT != WORKSPACE and os.path.dirname(p) == FILES_ROOT) or p == home \
            or home.startswith(p + os.sep):
        raise ValueError("refusing to delete a top-level or protected folder")
    if not os.path.lexists(p):
        raise FileNotFoundError(p)
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    else:
        os.unlink(p)


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
            if masked:                         # unmask with one big-int XOR (~100x faster than per byte)
                data = b[i + 4:need]
                if ln:
                    key = (b[i:i + 4] * (ln // 4 + 1))[:ln]
                    data = (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(ln, "big")
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

    # ---- file upload: raw body streamed to a temp file, then atomically renamed into place
    def _upload(self, q):
        p = safe_path(FILES_ROOT, q.get("path", ""))
        n = int(self.headers.get("Content-Length", "-1") or -1)
        if p == FILES_ROOT or os.path.isdir(p):
            return self._err("a folder with that name already exists", 409)
        if n < 0:
            return self._err("Content-Length required", 411)
        if n > MAX_UPLOAD_BYTES:
            return self._err(f"file too large (max {MAX_UPLOAD_BYTES // 1048576} MB)", 413)
        existed = os.path.isfile(p)
        if existed and q.get("overwrite") != "1":
            return self._err("file already exists", 409)
        make_dirs(os.path.dirname(p))
        tmp = os.path.join(os.path.dirname(p), f".{os.path.basename(p)}.opsui-{secrets.token_hex(4)}")
        try:
            with open(tmp, "wb") as f:
                left = n
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    f.write(chunk)
                    left -= len(chunk)
            if existed:                                   # keep owner/mode of the file we replace
                st = os.stat(p)
                os.chmod(tmp, st.st_mode & 0o7777)
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except Exception:
                    pass
            else:
                _own_like_parent(tmp)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._json({"ok": True})

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
                            "maxTerms": MAX_TERMS, "user": OPS_USER, "filesHome": FILES_HOME,
                            "maxUpload": MAX_UPLOAD_BYTES})
            elif u.path == "/api/targets":                # only sources that are actually running
                self._json(cached("targets", 10, lambda: {
                    "services": [{k: s[k] for k in ("name", "replicas", "running", "desired")}
                                 for s in list_services() if s["running"] > 0],
                    "containers": list_containers(),
                    "files": list_log_files(),
                }))
            elif u.path == "/api/status":
                self._json(cached("status", 20, lambda: {
                    "nodes": run_quick(["docker", "node", "ls", "--format",
                                        "{{.Hostname}}\t{{.Status}}\t{{.Availability}}\t{{.ManagerStatus}}"]),
                    "versions": running_versions(),
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
                with os.scandir(d) as it:
                    items = [{"name": e.name, "dir": e.is_dir()} for e in it]
                items.sort(key=lambda e: (not e["dir"], e["name"].lower()))
                rel = os.path.relpath(d, FILES_ROOT)
                self._json({"path": "" if rel == "." else rel.replace(os.sep, "/"),
                            "items": items[:MAX_LS_ITEMS], "truncated": len(items) > MAX_LS_ITEMS})
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
            if u.path == "/api/upload":                # raw body, not JSON
                return self._upload({k: v[0] for k, v in parse_qs(u.query).items()})
            data = self._body()
            if u.path == "/api/exists":
                paths = data.get("paths")
                if not isinstance(paths, list):
                    return self._err("paths must be a list")
                self._json({"exists": [r for r in paths[:20000] if isinstance(r, str)
                                       and os.path.lexists(safe_path(FILES_ROOT, r))]})
            elif u.path == "/api/mkdir":
                make_dirs(safe_path(FILES_ROOT, data.get("path", "")))
                self._json({"ok": True})
            elif u.path == "/api/delete":
                delete_entry(data.get("path", ""))
                self._json({"ok": True})
            elif u.path == "/api/file":
                p = safe_path(FILES_ROOT, data.get("path", ""))
                if p == FILES_ROOT or os.path.isdir(p):
                    return self._err("path is a directory")
                make_dirs(os.path.dirname(p))
                existed = os.path.isfile(p)
                if existed:
                    shutil.copy2(p, p + ".bak")
                with open(p, "w", encoding="utf-8", newline="") as f:      # keeps owner/mode of existing files
                    f.write(data.get("content", ""))
                if not existed:                       # new file: same owner as its folder, not root
                    _own_like_parent(p)
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
        except FileNotFoundError:
            self._err("not found", 404)
        except PermissionError as e:
            self._err(f"permission denied: {e.filename or ''}", 403)
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
    print(f"OpsUI 1.3 on :{PORT}  workspace={WORKSPACE}  files={FILES_ROOT}  logs={LOG_ROOT}  "
          f"vm_ssh={VM_SSH or '-'}", flush=True)
    srv.serve_forever(poll_interval=5)


if __name__ == "__main__":
    main()
