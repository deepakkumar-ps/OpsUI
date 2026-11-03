# OpsUI 2.0 — Motion Applied swarm console

Browser console for the swarm manager (10.104.10.88), port 3000.

- **Terminal**: real interactive shells with tabs (up to 4). vim, top, `docker exec -it`, Tab completion and Ctrl+C all work.
  - *Container shell*: runs inside OpsUI. `docker` controls the whole swarm, and /opt/docker is mounted.
  - *VM (SSH)*: a real login on the VM (default `ocsautotest@10.104.10.88`).
- **Commands**: add, edit and delete your own commands in the UI. They are saved on the VM in
  `/opt/docker/opsui-commands.json`. Click one to run it in a terminal (active, container or VM).
- **Live logs**: swarm services, containers and log files, streamed live with error/warning highlighting.
- **Files**: browse and edit any file on the VM (the VM's `/` is mounted at `/host`; opens in /opt/docker,
  type a path such as `/etc/hosts` in the "go to path" box to jump). Saving keeps a `.bak` copy of the previous version.
  Without the `/:/host` mount it falls back to /opt/docker only.
- **Terminal copy/paste**: select text and press Ctrl+C to copy (with nothing selected Ctrl+C interrupts as usual);
  Ctrl+V pastes. Ctrl+Shift+C / Ctrl+Shift+V still work.
- Motion Applied colours (Orange #FA6914, Grey #1F292E, Teal, Bright Blue), with a light/dark toggle in the top right.

Footprint: about 25 MB RAM and 0% CPU when idle. Uses only the Python standard library.

## Rebuild and deploy (on the VM)
    cd ~/opsui
    sed -i 's/\r$//' Dockerfile app.py opsui-stack.yml
    nice -n 19 docker build -t deepakkumarps08122/opsui:1.0 .
    docker push deepakkumarps08122/opsui:1.0
    docker service update --with-registry-auth --image deepakkumarps08122/opsui:1.0 \
        --env-add VM_SSH=ocsautotest@10.104.10.88 --env-add MAX_TERMS=4 \
        --mount-add type=bind,source=/,target=/host \
        --env-add FILES_ROOT=/host --env-add FILES_HOME=opt/docker \
        --limit-cpu 0.5 opsui_opsui

To apply the whole stack file instead (this also adds the new settings):
    docker stack deploy -c opsui-stack.yml --with-registry-auth opsui
