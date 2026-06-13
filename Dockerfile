# Coder isolation image — for upgrading isolation.mode from `dir` to `container`.
#
# The orchestrator runs on the HOST and shells to `codex exec`. To confine the
# coder's danger-full-access inside a container, build this image and (a) install
# codex inside it, or (b) point codex's own container/sandbox feature at it. The
# orchestrator confines the coder to runs/<id>/workspace/, which is bind-mounted.
#
#   docker build -t autoscience-coder .
#   # then set in config.yaml:  isolation: { mode: container, docker_image: autoscience-coder }
#
# NOTE: Docker is NOT installed on the current build machine, so the active mode is
# `dir` (fresh git repo + confined directory). This image is shipped ready for the
# day Docker is available.

FROM python:3.12-slim

# LaTeX (compile) + git + repro toolchain. texlive-latex-extra carries geometry,
# the neurips workshop style deps, etc. Trim packages to taste for image size.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        bash \
        curl \
        ca-certificates \
        poppler-utils \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-latex-extra \
        texlive-fonts-recommended \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Python deps the coder is likely to need for synthetic-data papers.
RUN pip install --no-cache-dir numpy scipy matplotlib

# The coder operates here; the orchestrator bind-mounts runs/<id>/workspace/ to it.
# Network stays available for installs/data, but the orchestrator never transmits
# artifacts anywhere except local disk (no auto-submission, ever).
CMD ["/bin/bash"]
