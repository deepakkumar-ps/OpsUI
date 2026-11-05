# OpsUI 1.0 — Motion Applied swarm console

A small, browser-based ops console for the Docker Swarm manager (10.104.10.88), served on port 3000.
It gives you terminals, saved commands, live logs and a file editor in one page. There is no
database, no framework and no build step: one Python file (standard library only) plus one HTML page.

Image: `deepakkumarps08122/opsui:1.0`

---

## 1. What you get

| Tab | What it does |
|---|---|
| **Terminal** | Real interactive shells in the browser, up to 4 tabs. vim, top, `docker exec -it`, Tab completion and Ctrl+C all work. |
| **Commands** | Your own saved commands: add, edit and delete them in the UI. Click one to run it in a terminal. |
| **Live logs** | Streams swarm service logs, container logs or log files live, with error/warning highlighting, filter, pause and download. |
| **Files** | Browse and edit any file on the VM. Saving keeps a `.bak` copy of the previous version. |

The header also shows swarm health ("3/3 nodes ready"), the leader's hostname and a light/dark theme toggle.
Colours follow Motion Applied branding (Orange #FA6914, Grey #1F292E, Teal, Bright Blue).

Footprint: about 25 MB RAM and 0% CPU when idle.

### Terminal types
- **Container shell**: a bash login shell *inside* the OpsUI container. Because the Docker socket is mounted,
  the `docker` command here controls the whole swarm. It starts in `/opt/docker`.
- **VM (SSH)**: runs `ssh user@host` from the container, so you get a real login on the VM
  (default `ocsautotest@10.104.10.88`; you can change the target in the UI). It asks for the password unless
  you mount an SSH key (see the stack file).

### Copy and paste in the terminal
- Select text and press **Ctrl+C** to copy. With nothing selected, Ctrl+C interrupts as usual.
- **Ctrl+V** pastes. Ctrl+Shift+C / Ctrl+Shift+V also work.

---

## 2. Files in this repository

| File | Purpose |
|---|---|
| `app.py` | The whole backend: HTTP server, login, REST API, WebSocket terminals, log streaming, file editing. |
| `static/index.html` | The whole frontend: HTML, CSS and JavaScript in one file. |
| `static/vendor/xterm.mjs`, `xterm.css` | [xterm.js](https://xtermjs.org/), the terminal emulator that draws the terminal in the browser (bundled, so it works offline). |
| `commands.default.json` | Example commands shown the first time, before you have saved your own. |
| `Dockerfile` | Builds the image: Python 3.12 Alpine plus bash, ssh, docker CLI, vim, nano, curl, jq and similar tools. |
| `opsui-stack.yml` | Swarm stack file: ports, mounts, environment, password secret, resource limits. |
| `THIRD_PARTY_NOTICES.txt` | Licence notice for xterm.js. |

---

## 3. How it works

```
  Browser (index.html + xterm.js)
     │  HTTP Basic auth (admin / password from the Docker secret)
     ▼
  app.py  — ThreadingHTTPServer on :3000, inside the opsui container
     ├── /api/*        JSON + streaming endpoints
     ├── /ws/term      WebSocket ⇄ pseudo-terminal (bash or ssh)
     │
     ├── /var/run/docker.sock ──► Docker engine (controls the whole swarm)
     ├── /opt/docker           ──► workspace + saved commands
     ├── /host (= VM's /)      ──► Files tab
     └── /logs (= /var/log/bridge, read-only) ──► log files
```

### 3.1 Start-up
1. `tini` starts as PID 1 (it forwards signals and cleans up finished child processes), then runs `python -I /app/app.py`.
2. `app.py` reads its settings from environment variables (section 6).
3. It reads the password from `OPS_PASS_FILE` (the Docker secret `/run/secrets/opsui_password`) or from `OPS_PASS`.
   **If neither is set it will not start.**
4. It lowers its own CPU priority (`nice 10`) so it always gives way to Bridge, then listens on `0.0.0.0:3000`.

### 3.2 Login and security
- Every page and API call needs **HTTP Basic auth** (`OPS_USER`, default `admin`, plus the password).
  The browser shows its own login box. The password is compared in constant time.
- POST requests (save file, save commands, run) are refused if the `Origin` header is from another site,
  which blocks cross-site request forgery.
- Browsers cannot send Basic auth on WebSockets, so the page first calls `/api/config`, which returns a
  random **token valid for 12 hours**. The terminal WebSocket must present this token and must come from the same origin.
- File paths are checked with `safe_path()`: anything that resolves (after `..` and symlinks) outside the
  allowed root is refused.
- Log and container names must match a strict pattern, and SSH targets must look like `user@host[:port]`,
  so nothing can be injected into those commands.
- The password is removed from the environment of every shell and child process.

> OpsUI is a full admin console: anyone who logs in can run any command on the swarm and edit any file on the VM.
> Keep the password strong and keep port 3000 on the internal network. The traffic is plain HTTP.

### 3.3 Terminal (WebSocket + PTY)
1. In the browser, xterm.js draws the terminal. Clicking *Container* or *VM (SSH)* opens
   `ws://host:3000/ws/term?t=<token>&shell=container|vm&target=...&cols=..&rows=..`.
2. `app.py` does the WebSocket handshake itself (a minimal RFC 6455 implementation, no libraries).
3. It calls `pty.fork()` to create a **pseudo-terminal** and starts `/bin/bash -l` (container) or
   `ssh -tt user@host` (VM) in it. Because it is a real TTY, full-screen programs, colours, Ctrl+C and Ctrl+Z behave normally.
4. Data flow:
   - keys you type → `{"t":"i","d":"..."}` → written to the PTY
   - window resize → `{"t":"r","c":cols,"r":rows}` → `TIOCSWINSZ` ioctl, so vim/top redraw at the right size
   - PTY output → binary WebSocket frames → xterm.js
5. A `select()` loop waits on both the socket and the PTY, so it uses no CPU while idle. Every 30 s it sends a ping;
   if the browser has been silent for 95 s the session is closed.
6. When the tab closes or the connection drops, the whole process group gets SIGHUP/SIGTERM (then SIGKILL),
   so nothing is left running. If the connection drops, press Enter in that tab to reconnect.
7. At most `MAX_TERMS` (default 4) terminals can be open at once; extra ones are refused with HTTP 429.

### 3.4 Commands
- Saved in `/opt/docker/opsui-commands.json` on the VM, so they survive image upgrades and redeploys.
  If that file does not exist yet, the examples from `commands.default.json` are shown.
- Each command has: `name`, `group`, `cmd` (can be several lines), `where` and `confirm`.
  - `where` = `active` (current terminal), `container` or `vm`.
  - `confirm: true` shows an "Are you sure?" box first (used for restart/rollback).
- Clicking a command **types it into a terminal**, as if you typed it yourself. It uses an existing terminal of
  the right type, or opens a new one. For a new SSH terminal it waits until you have logged in (it watches for a
  `$` or `#` prompt) and then types the command.
- Saving writes to a temporary file and then renames it, so the file is never left half-written.
  Up to 500 commands are kept.

### 3.5 Live logs
- `/api/targets` lists swarm services, running containers and `*.log/*.txt/*.out` files under `/logs`
  (two folder levels deep). The result is cached for 10 s.
- Choosing a source opens `/api/logs`, which runs one of:
  - `docker service logs -f --timestamps --tail N <service>`
  - `docker logs -f --timestamps --tail N <container>`
  - `tail -n N -F /logs/<file>`
- The output is sent to the browser as it arrives (an HTTP response that stays open). When nothing has been
  printed for 25 s the server sends a small keep-alive line, which the browser hides.
- The browser colours lines containing ERROR/FATAL/EXCEPTION/CRITICAL/Traceback/panic red and WARN/WARNING
  amber, counts them, and keeps the last 2000 lines. You can filter, show only errors, pause, clear and download.
- If the stream ends (for example the service was redeployed) it reconnects automatically after 3 s.
- At most `MAX_STREAMS` (default 3) streams run at once. Closing the tab or pressing Stop kills the `docker logs`/`tail` process.

### 3.6 Files
- The VM's `/` is mounted at `/host`, and `FILES_ROOT=/host`, so the Files tab can reach the whole VM.
  It opens in `FILES_HOME` (`opt/docker`). Type a path such as `/etc/hosts` in the "go to path" box to jump to it.
- `/api/ls` lists a folder; `/api/file` (GET) opens a text file up to 2 MB. Binary files are refused (use the terminal).
- Saving (POST `/api/file`, or Ctrl+S) first copies the old file to `<name>.bak`, then writes the new content.
  An existing file keeps its owner and permissions. A new file gets the same owner as its folder (not root).
- Without the `/:/host` mount, the Files tab falls back to `/opt/docker` only.

### 3.7 Swarm status
`/api/status` runs `docker node ls` and `docker service ls` (cached for 20 s). The page refreshes it every
60 s, and only while the browser tab is visible.

### 3.8 Why it stays light
- Nothing runs unless a browser is connected, and every child process is killed as soon as its terminal or stream closes.
- All waiting is done with `select()` in the kernel, so idle CPU is 0%.
- Child processes run at the lowest priority (`nice 19`); the docker CLI is limited to one thread (`GOMAXPROCS=1`).
- Static files are gzip-compressed once and kept in memory.
- The stack file limits the service to 0.5 CPU and 192 MB RAM.

---

## 4. Step by step: what travels between browser and server

The examples below use the real messages that `index.html` and `app.py` exchange.
`abc123` stands for the token and `YWRtaW46...` for the encoded login; the real values are random or secret.

### 4.1 Three kinds of connection

OpsUI uses three ways of talking to the server, depending on the job.

**a) Normal request/response** (status, file list, open/save file, save commands):
one question, one answer, then the request is finished.

```
Browser                    Server

  GET /api/status  ---------->
                   <----------  JSON response
  request finished
```

**b) Streaming response** (live logs, `/api/run`): one request, but the server keeps the response
open and sends output **as it happens**, until one side closes it. Data flows one way only (server to browser).

```
Browser                    Server

  GET /api/logs?...  -------->  starts "docker service logs -f ..."
                     <--------  line 1
                     <--------  line 2
                     <--------  ...           (stays open for minutes or hours)
  Stop / close tab   ---X       server kills the docker logs process
```

**c) WebSocket** (terminals): one long-lived connection where **both sides can send at any time**.
Your keystrokes go in, the shell's output comes back, and nobody has to wait for a "response".

```
Browser                         OpsUI server
Terminal (xterm.js)             PTY / bash

"docker ps⏎"  -------------------->
              <-------------------- container list

"docker service ls⏎" ------------->
              <-------------------- service list

Ctrl+C        -------------------->
              <-------------------- ^C  and a new prompt

resize 180x40 -------------------->  (nothing comes back; programs redraw at the new size)
```

### 4.2 Opening the page and logging in

```
Browser                                   OpsUI server (app.py)

GET /                      -------------->  no Authorization header
                           <--------------  401 Unauthorized
                                            WWW-Authenticate: Basic realm="OpsUI"

  (the browser shows its login box; you type admin and the password)

GET /                      -------------->  Authorization: Basic YWRtaW46...
                                            compare with the expected value
                                            (constant-time compare)
                           <--------------  200 OK  index.html  (gzip-compressed)

GET /static/vendor/xterm.mjs ------------>
                           <--------------  200 OK  terminal library (cached 1 day)

GET /api/config            -------------->  makes a new random token, valid 12 h
                           <--------------  {"vmSsh":"ocsautotest@10.104.10.88",
                                             "token":"abc123", "workspace":"/opt/docker",
                                             "maxTerms":4, "user":"admin",
                                             "filesHome":"opt/docker"}

GET /api/status            -------------->  docker node ls + docker service ls
                                            (result cached for 20 s)
                           <--------------  {"nodes":["mgr1\tReady\tActive\tLeader", ...],
                                             "services":["bridge_bridge-service\t1/1\t...", ...]}

GET /api/commands          -------------->  reads /opt/docker/opsui-commands.json
                           <--------------  [{"id":"d1","group":"Swarm","name":"Nodes", ...}, ...]
```

After this the browser repeats its `Authorization` header on every request automatically, so you log in only once.
`YWRtaW46...` is just `admin:password` in base64. It is **encoded, not encrypted**, which is why OpsUI
must stay on the internal network (plain HTTP).

The page asks for `/api/status` again every 60 s, but only while the browser tab is visible.

### 4.3 Opening a terminal

**Step 1: the browser asks to switch the connection to a WebSocket.**
It uses the token from `/api/config`, because browsers cannot send the Basic login on a WebSocket.

```
GET /ws/term?t=abc123&shell=container&target=&cols=120&rows=30 HTTP/1.1
Host: 10.104.10.88:3000
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Origin: http://10.104.10.88:3000
```

For a VM terminal it is `shell=vm&target=ocsautotest@10.104.10.88`.

**Step 2: the server checks the request and answers.**

```
Browser                                   OpsUI server

GET /ws/term?...           -------------->  1. token abc123 known and not expired?  else 403
                                            2. Origin is the same host?              else 403
                                            3. SSH target looks like user@host?      else 400
                                            4. fewer than 4 terminals open?          else 429
                           <--------------  HTTP/1.1 101 Switching Protocols
                                            Upgrade: websocket
                                            Connection: Upgrade
                                            Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=

                                            5. pty.fork()  -> start /bin/bash -l
                                               (or: ssh -tt ocsautotest@10.104.10.88)
                                            6. set the terminal size to 120 x 30
```

`Sec-WebSocket-Accept` is `base64(SHA1(key + fixed GUID))`. It proves to the browser that the server
really speaks WebSocket. From here on, the same TCP connection carries WebSocket frames, not HTTP.

**Step 3: the session.** Every message from the browser is a small JSON object:
`{"t":"i","d":"..."}` for input (keys) and `{"t":"r","c":cols,"r":rows}` for a resize.
Everything the server sends back is the shell's raw output.

```
Browser (xterm.js)              OpsUI server (app.py)              PTY -> bash

                     <--------  "opsui:/opt/docker$ "   <--------  bash prints its prompt

you type "d"
{"t":"i","d":"d"}    -------->  write "d" to the PTY    -------->  bash receives "d"
                     <--------  "d"                     <--------  bash echoes it back
  ... same for "ocker ps" (one message per key; a paste is sent in one message)

you press Enter
{"t":"i","d":"\r"}   -------->  write "\r"              -------->  bash runs "docker ps"
                     <--------  "CONTAINER ID   IMAGE ..." <-----  output, in chunks
                     <--------  "opsui:/opt/docker$ "   <--------  new prompt

you press Ctrl+C
{"t":"i","d":"\u0003"} ------>  write byte 0x03         -------->  the terminal sends SIGINT
                                                                   to the running program
                     <--------  "^C" + new prompt       <--------

you resize the window
{"t":"r","c":180,"r":40} ---->  ioctl TIOCSWINSZ 40x180 -------->  programs get SIGWINCH;
                                                                   vim / top redraw at the new size

nothing happens for 30 s
                     <--------  WebSocket ping
pong                 -------->  (the browser answers automatically)
                                (no sign of the browser for 95 s -> server closes the session)

you close the tab
close frame          -------->  SIGHUP + SIGTERM to bash and everything it started,
                                SIGKILL if still alive after 1 s; terminal slot freed
```

The browser does **not** print the letters you type. It only sends them; what you see is the shell
echoing them back. That is why full-screen programs and Tab completion work exactly as in a normal terminal.

If the connection drops, the tab shows `[disconnected — press Enter to reconnect]`. Pressing Enter repeats step 1.
If the first attempt fails (for example the token expired), the page fetches a new token from `/api/config` and retries once by itself.

### 4.4 Running and saving commands

**Clicking a saved command** doesn't use a separate API. The browser just types the command into a terminal for you:

```
You click "Services"   (cmd: docker service ls,  where: container)

  Is a container terminal open?
     yes -> switch to it, then send   {"t":"i","d":"docker service ls\r"}
     no  -> open a new one (4.3), wait 350 ms for the prompt, then send it

  where: vm  -> open an SSH terminal, show "Log in first ...",
                watch the output until a line ends in "$" or "#" (you are logged in),
                then send the command
  where: active -> use whichever terminal is in front

  confirm: true -> the browser first asks "Run “Restart Bridge”?"
```

**Saving** after add/edit/delete sends the whole list:

```
Browser                                   OpsUI server

POST /api/commands         -------------->  Authorization OK?  Origin same host?  else 403
Content-Type: application/json
{"commands":[
  {"name":"Services","group":"Swarm",
   "cmd":"docker service ls","where":"container"},
  ...]}
                                            clean every entry: trim, limit lengths,
                                            give it an id, drop empty ones (max 500)
                                            write opsui-commands.json.tmp
                                            rename it over opsui-commands.json
                           <--------------  200 OK  [the cleaned list]
```

### 4.5 Live logs

```
Browser                                   OpsUI server

GET /api/targets           -------------->  docker service ls, docker ps, look for log files in /logs
                                            (cached for 10 s)
                           <--------------  {"services":["bridge_bridge-service","opsui_opsui"],
                                             "containers":["opsui_opsui.1.x7k..."],
                                             "files":["bridge/service.log"]}

you click bridge_bridge-service
GET /api/logs?kind=service&name=bridge_bridge-service&tail=200
                           -------------->  name allowed? (letters, digits, . _ -)
                                            fewer than 3 streams running?  else 429
                                            start: docker service logs -f --timestamps
                                                   --tail 200 bridge_bridge-service
                           <--------------  200 OK   Content-Type: text/plain
                                            (the response stays open)
                           <--------------  2026-11-05T10:00:01Z ... INFO  started
                           <--------------  2026-11-05T10:00:02Z ... ERROR Kafka timeout
                                                                     ^ browser shows it red
                           ...             (no output for 25 s)
                           <--------------  ::opsui-ping::
                                            ^ keeps the connection alive; the browser hides it

the service is redeployed
                           <--------------  (docker logs exits, response ends)
  browser shows "stream ended — reconnecting in 3s…", waits 3 s,
  then sends the same request with tail=0 (so old lines are not repeated)

you click Stop / close the tab
  connection closed  ---X                   server kills the docker logs process, slot freed
```

For containers the server runs `docker logs -f ...`; for files, `tail -n 200 -F /logs/<file>`.

### 4.6 Files

```
Browser                                   OpsUI server   (FILES_ROOT = /host = the VM's /)

GET /api/ls?path=opt/docker  ------------>  /host/opt/docker inside /host?  yes
                           <--------------  {"path":"opt/docker",
                                             "items":[{"name":"bridge","dir":true},
                                                      {"name":"stack.yml","dir":false}]}

GET /api/file?path=opt/docker/stack.yml -->  inside /host?  under 2 MB?  not binary?
                           <--------------  {"content":"version: \"3.8\"\n..."}

you edit and press Ctrl+S
POST /api/file             -------------->  copy stack.yml -> stack.yml.bak
{"path":"opt/docker/stack.yml",            write the new content
 "content":"version: \"3.8\"\n..."}        (owner and permissions stay the same)
                           <--------------  {"ok":true}

a path that tries to escape
GET /api/file?path=../../etc/shadow ----->  /host/../../etc/shadow resolves to /etc/shadow,
                                            which is outside /host
                           <--------------  400 {"error":"path outside allowed root"}
```

### 4.7 `/api/run` (for scripts and curl)

The page itself runs commands through terminals, but the server also has a simple "run and stream" endpoint.
It is useful from scripts:

```
Client                                    OpsUI server

POST /api/run              -------------->  /bin/sh -c "docker node ls"   in /opt/docker
{"cmd":"docker node ls","cwd":""}
                           <--------------  ID    HOSTNAME   STATUS ...      (as it is printed)
                           <--------------  ...
                           <--------------  [exit code 0]
```

    curl -u admin:PASSWORD -H 'Content-Type: application/json' \
         -d '{"cmd":"docker node ls"}' http://10.104.10.88:3000/api/run

### 4.8 Error answers you may see

| Code | Meaning |
|---|---|
| `401` | Not logged in, or wrong user/password. |
| `403` | Request from another website, or invalid/expired terminal token. |
| `400` | Bad input: invalid name, path outside the allowed folder, binary file, file over 2 MB. |
| `404` | Unknown URL or file not found. |
| `429` | Too many terminals (4) or live streams (3) already open. |
| `500` | Unexpected server error; the message says what failed. |

---

## 5. API reference

All endpoints need Basic auth, except `/ws/term`, which uses the token.

| Method | Path | Description |
|---|---|---|
| GET | `/` | The UI (`index.html`). |
| GET | `/static/...` | xterm.js files. |
| GET | `/api/config` | Default SSH target, WebSocket token, workspace, max terminals, user, Files home folder. |
| GET | `/api/status` | Swarm nodes and services. |
| GET | `/api/targets` | Log sources: services, containers, log files. |
| GET | `/api/logs?kind=service\|container\|file&name=...&tail=200` | Live log stream (plain text). |
| GET | `/api/ls?path=...` | List a folder under `FILES_ROOT`. |
| GET | `/api/file?path=...` | Read a text file. |
| POST | `/api/file` | `{"path","content"}`: save a file (keeps `.bak`). |
| GET | `/api/commands` | List saved commands. |
| POST | `/api/commands` | `{"commands":[...]}`: replace the saved command list. |
| POST | `/api/run` | `{"cmd","cwd"}`: run a command in the workspace and stream its output and exit code. |
| GET (WebSocket) | `/ws/term?t=&shell=&target=&cols=&rows=` | Interactive terminal. |

---

## 6. Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `3000` | Listening port. |
| `OPS_USER` | `admin` | Login user name. |
| `OPS_PASS_FILE` | — | File holding the password (the Docker secret). |
| `OPS_PASS` | — | Password as plain text (used only if there is no `OPS_PASS_FILE`). |
| `WORKSPACE` | `/opt/docker` | Folder where terminals start; saved commands live here. |
| `LOG_ROOT` | `/logs` | Folder searched for log files. |
| `FILES_ROOT` | = `WORKSPACE` | Root of the Files tab (`/host` = the whole VM). |
| `FILES_HOME` | — | Folder the Files tab opens in, relative to `FILES_ROOT` (e.g. `opt/docker`). |
| `VM_SSH` | — | Default target for VM (SSH) terminals, e.g. `ocsautotest@10.104.10.88`. |
| `MAX_TERMS` | `4` | Most terminals open at once. |
| `MAX_STREAMS` | `3` | Most live log/run streams at once. |
| `TZ` | — | Time zone (stack file uses `Europe/London`). |

### Mounts used by the stack file
| Host | Container | Why |
|---|---|---|
| `/var/run/docker.sock` | same | Lets the `docker` command control the swarm. |
| `/opt/docker` | `/opt/docker` | Workspace and `opsui-commands.json`. |
| `/var/log/bridge` | `/logs` (read-only) | Bridge log files for Live logs. Remove the line if there are none. |
| `/` | `/host` | Files tab: the whole VM filesystem. |
| `~/.ssh/opsui_key` (optional) | `/root/.ssh/id_ed25519` | Passwordless VM (SSH) terminals. |

---

## 7. First-time setup (on the VM)

Create the password secret once (the stack expects it):

    printf 'choose-a-strong-password' | docker secret create opsui_password -

Optional, for passwordless VM (SSH) terminals:

    ssh-keygen -t ed25519 -N '' -f ~/.ssh/opsui_key
    cat ~/.ssh/opsui_key.pub >> ~/.ssh/authorized_keys

Then uncomment the key line in `opsui-stack.yml`.

## 8. Build and deploy (on the VM)

    cd ~/opsui
    sed -i 's/\r$//' Dockerfile app.py opsui-stack.yml      # remove Windows line endings
    nice -n 19 docker build -t deepakkumarps08122/opsui:1.0 .
    docker push deepakkumarps08122/opsui:1.0

Deploy or update with the whole stack file (recommended, applies every setting):

    docker stack deploy -c opsui-stack.yml --with-registry-auth opsui

Or update only the running service:

    docker service update --with-registry-auth --image deepakkumarps08122/opsui:1.0 \
        --env-add VM_SSH=ocsautotest@10.104.10.88 --env-add MAX_TERMS=4 \
        --mount-add type=bind,source=/,target=/host \
        --env-add FILES_ROOT=/host --env-add FILES_HOME=opt/docker \
        --limit-cpu 0.5 opsui_opsui

Then open `http://10.104.10.88:3000` and log in as `admin`.

> If you rebuild with the same `1.0` tag, add `--force` to `docker service update` so swarm pulls the new image.

## 9. Run locally for testing

Needs Linux or WSL, since it uses `pty`, `fcntl` and `termios`:

    OPS_PASS=test WORKSPACE=$PWD python3 app.py        # then open http://localhost:3000

Or with Docker:

    docker build -t opsui:1.0 .
    docker run --rm -p 3000:3000 -e OPS_PASS=test -v /var/run/docker.sock:/var/run/docker.sock opsui:1.0

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Container exits with "Refusing to start" | No password: create the `opsui_password` secret or set `OPS_PASS`. |
| "too many terminals open" / "too many live streams" | Close some terminal tabs or log streams, or raise `MAX_TERMS` / `MAX_STREAMS`. |
| Live logs says "Nothing found" | `/var/run/docker.sock` is not mounted. |
| Files tab only shows `/opt/docker` | The `/:/host` mount or `FILES_ROOT=/host` is missing. |
| VM (SSH) asks for a password | Expected unless the SSH key is mounted (section 7). |
| Terminal shows "[disconnected]" | Press Enter in that tab to reconnect. |
| `bash\r: not found` or similar after build | Files had Windows line endings: run the `sed` line in section 8. |

## Licence notes
xterm.js is MIT-licensed; see `THIRD_PARTY_NOTICES.txt`.
