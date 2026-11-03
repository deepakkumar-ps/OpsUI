# OpsUI 2.0 - Motion Applied swarm console (terminal, commands, live logs, files)
FROM python:3.12-alpine

# bash + ssh for the web terminal, docker CLI for swarm control, a few everyday tools
RUN apk add --no-cache \
      tini bash bash-completion ncurses openssh-client \
      docker-cli docker-cli-buildx docker-bash-completion \
      nano vim less curl jq procps netcat-openbsd bind-tools \
 && printf '%s\n' \
    'export PS1="\[\e[38;2;250;105;20m\]opsui\[\e[0m\]:\[\e[36m\]\w\[\e[0m\]\$ "' \
    'alias ll="ls -la --color=auto"; alias ls="ls --color=auto"' \
    '[ -f /usr/share/bash-completion/bash_completion ] && . /usr/share/bash-completion/bash_completion' \
    > /etc/profile.d/opsui.sh

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONOPTIMIZE=2 \
    PORT=3000 WORKSPACE=/opt/docker LOG_ROOT=/logs MAX_STREAMS=3 MAX_TERMS=4 \
    HOME=/root SHELL=/bin/bash

WORKDIR /app
COPY app.py commands.default.json /app/
COPY static /app/static

EXPOSE 3000
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python", "-I", "/app/app.py"]
