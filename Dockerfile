# docpipe-agent — the product image.
#
# Build notes (do not "modernise" this file without re-testing the build host):
#   * The build host runs Docker 18.09 with NO buildx, so this Dockerfile must
#     stay classic-builder compatible: no `# syntax=` directive, no `RUN --mount`,
#     no heredocs, no COPY --link, no FROM --platform.
#   * Docker Hub is unreachable from the build host. Bases come from
#     public.ecr.aws, which serves Docker *manifest lists* (verified) that 18.09
#     can consume, unlike the OCI index it serves by default to newer clients.
#   * No secret may enter this image: DEEPSEEK_API_KEY is a runtime variable.

# Node 25, not 22, and this is load-bearing rather than taste.
#
# Under Node 22.23.3 on this host's kernel (4.18 / overlayfs), `fs.promises.realpath`
# with a trailing slash returns EPERM for ANY path — `realpathSync` works, and the
# async form works without the slash, but Cordis HMR calls the async form on the
# profile directory with a trailing slash, so every profile boot died on
# "EPERM: operation not permitted, realpath '/opt/dsh-home/profiles/<name>/'".
# The same harness runs unmodified on Node 25, which is what a working install
# here uses. Verify with:
#   node -e "require('fs').promises.realpath('/tmp/').then(()=>console.log('ok'),e=>console.log(e.code))"
FROM public.ecr.aws/docker/library/node:25-bookworm-slim AS nodestage

FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

ARG DSH_VERSION=0.1.1-rc.2

# ── the harness install must be REPRODUCIBLE ────────────────────────────────
# `npm install -g @deepseek-ai/dsh@<v>` does not reproduce a working harness: every
# dependency of dsh is caret-ranged, so a fresh install floats them to whatever is
# current that day. Measured against a working 0.1.1-rc.2 tree (197 packages on
# both sides), exactly seven had drifted, and every one of them is Cordis core:
#
#     cordis                4.0.1  -> 4.0.4
#     cordis-plugin-group   1.0.1  -> 1.0.4
#     cordis-plugin-include 1.0.6  -> 1.0.9
#     cordis-plugin-loader  1.0.2  -> 1.0.5
#     cordis-plugin-timer   1.1.3  -> 1.1.6
#     cosmokit              1.8.2  -> 1.8.5
#     schemastery           3.18.1 -> 3.18.4
#
# The symptom of that drift is not a version mismatch error. The HMR plugin still
# imports, but fails to register its service against the newer Cordis, and
# `runProfile` then aborts every profile — headless and web alike — with "user
# patch-layer watching requires the Cordis HMR service" before any preset mounts.
# This is the same class of failure as dsh-market/dsh-market#684, whose title
# names it: a floating `cordis-plugin-hmr` dependency. Pinning only hmr was not
# enough; the whole Cordis set has to match the dsh release.
#
# Re-derive this set whenever DSH_VERSION changes: install that dsh once in a
# scratch directory, and diff its tree against these versions.
ARG DSH_PINS="cordis@4.0.1 cordis-plugin-group@1.0.1 cordis-plugin-include@1.0.6 cordis-plugin-loader@1.0.2 cordis-plugin-timer@1.1.3 cosmokit@1.8.2 schemastery@3.18.1"
ARG HMR_PIN=1.0.16

ARG DOCPIPE_VERSION=0.1.0
ARG INSTALL_CJK_FONTS=1
ARG INSTALL_FFMPEG=0
ARG INSTALL_GHOSTSCRIPT=0
# Domestic mirrors by default: the build host is in China, where plain HTTP to
# deb.debian.org failed GPG verification (intercepted responses) and where the
# upstream PyPI/npm round trips are slow. All three are overridable.
ARG APT_MIRROR=https://mirrors.huaweicloud.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# A stalled download with no error is the failure mode here, not a slow one.
ARG PIP_TIMEOUT=30
ARG PIP_RETRIES=5
ARG NPM_REGISTRY=https://registry.npmmirror.com

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOCPIPE_HOME=/opt/docpipe \
    DSH_HOME=/opt/dsh-home \
    PATH=/opt/venv/bin:/usr/local/bin:$PATH

# ── system tooling ──────────────────────────────────────────────────────────
# poppler-utils gives pdftotext/pdftoppm/pdfinfo — the preferred text and raster
# backends. PDFium (pypdfium2) is the primary rasteriser inside the runtime, so
# ghostscript is optional: it is off by default because its libpaper1 dependency
# fails to configure on this build host (`ucf: do not have write privilege to the
# state data`, the container inheriting umask 0027 with no /var/lib/ucf), and a
# secondary rasteriser is not worth an unreliable build. Enable with
# --build-arg INSTALL_GHOSTSCRIPT=1 where the host behaves.
# tesseract-ocr is here for a deliberate reason rather than convenience: a page that
# is an image has no other local reader, and the alternative is the vision model at
# ~4 100 tokens a page, a per-page cost that never falls with scale. Tesseract reads
# such a page for no tokens at all, and the model is kept for pages OCR cannot read.
#
# What tesseract is NOT, measured on the 1789 volume it was added for: that volume
# turned out to carry an OCR layer of 4 300-5 600 characters a page, and on the same
# page the layer and a fresh tesseract pass are comparable — 732 tokens with inserted
# spaces against 728 tokens with substituted characters, same word shape. Neither
# source is better; they fail differently. The operator is for documents with NO
# layer, not for replacing a layer that exists.
#
# DO NOT put a comment inside the command below. The first attempt at adding
# tesseract did exactly that, and a `#` inside a backslash-continued RUN comments
# out every argument after it — the package was silently not installed, apt
# succeeded, and the build exited 0. The verification below now fails loudly.
#
# libgomp1 serves pyarrow/pyarrow OpenMP kernels; libatomic1 is required by the
# Node 25 runtime copied from the node stage; sqlite3 backs the work queue.
#
# The bootstrap install needs the gpgv shim: see docker/apt-key-gpgv-shim.sh for
# why a slim Debian base cannot verify its own repositories. gnupg is installed
# in the same transaction so apt's real apt-key works afterwards, and the build
# re-runs `apt-get update` WITHOUT the shim to prove it before removing it.
#
# Sources are written in the deb822 format the base already used, NOT as a
# classic sources.list: only deb822 carries `Signed-By`, and without it apt
# invokes the verifier with no keyring at all, which silently falls back to the
# empty legacy /etc/apt/trusted.gpg. That difference is the whole reason the
# shim works here and did not work with a classic sources.list.
COPY docker/apt-key-gpgv-shim.sh /usr/local/bin/apt-key-gpgv-shim
RUN set -eux; \
    chmod 0755 /usr/local/bin/apt-key-gpgv-shim; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list; \
    printf 'Types: deb\nURIs: %s/debian\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: %s/debian-security\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' \
        "$APT_MIRROR" "$APT_MIRROR" > /etc/apt/sources.list.d/debian.sources; \
    cat /etc/apt/sources.list.d/debian.sources; \
    apt-get -o Dir::Bin::apt-key=/usr/local/bin/apt-key-gpgv-shim update; \
    apt-get -o Dir::Bin::apt-key=/usr/local/bin/apt-key-gpgv-shim install -y --no-install-recommends \
        gnupg \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        fontconfig \
        fonts-dejavu-core \
        libgomp1 \
        libatomic1 \
        libglib2.0-0 \
        rsync \
        git \
        curl \
        ca-certificates \
        sqlite3 \
        unzip \
        xz-utils \
        procps; \
    if [ "$INSTALL_CJK_FONTS" = "1" ]; then \
        apt-get install -y --no-install-recommends fonts-noto-cjk; \
    fi; \
    if [ "$INSTALL_FFMPEG" = "1" ]; then \
        apt-get install -y --no-install-recommends ffmpeg; \
    fi; \
    if [ "${INSTALL_GHOSTSCRIPT:-0}" = "1" ]; then \
        apt-get install -y --no-install-recommends ghostscript; \
    fi; \
    fc-cache -f >/dev/null 2>&1 || true; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    rm -f /usr/local/bin/apt-key-gpgv-shim; \
    rm -rf /var/lib/apt/lists/*; \
    pdftotext -v 2>&1 | head -1; \
    pdfinfo -v 2>&1 | head -1

# ── node + the DeepSeek Harness agent ───────────────────────────────────────
COPY --from=nodestage /usr/local/bin/node /usr/local/bin/node
COPY --from=nodestage /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN set -eux; \
    umask 0022; \
    ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm; \
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx; \
    node --version; npm --version; \
    npm install --global --registry="${NPM_REGISTRY}" "@deepseek-ai/dsh@${DSH_VERSION}"; \
    # pnpm is how `dsh plugin --profile <name> add <pkg>` installs extensions, and
    # that is the documented way for a user of this image to extend the AGENT
    # layer. Without it the command fails with "pnpm not found on PATH", so the
    # plugin story would be a documented capability that does not work.
    npm install --global --registry="${NPM_REGISTRY}" pnpm; \
    # Pin the Cordis core set, then hmr, by extracting exact tarballs. This
    # bypasses npm's tree resolution on purpose: the point is to reproduce one
    # known-good tree, not to compute a new one.
    pin_pkg() { \
        pkg="$1"; ver="$2"; \
        dir=/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/"$pkg"; \
        echo "pinning $pkg@$ver"; \
        rm -rf "$dir" /tmp/pin; mkdir -p /tmp/pin; \
        curl -fsSL "${NPM_REGISTRY}/@deepseek-ai/${pkg}/-/${pkg}-${ver}.tgz" | tar -xz -C /tmp/pin; \
        mv /tmp/pin/package "$dir"; rm -rf /tmp/pin; \
        got=$(node -p "require('$dir/package.json').version"); \
        test "$got" = "$ver" || { echo "$pkg pinned to $got, expected $ver" >&2; exit 1; }; \
    }; \
    for entry in ${DSH_PINS}; do pin_pkg "${entry%%@*}" "${entry##*@}"; done; \
    if [ -n "${HMR_PIN}" ]; then pin_pkg cordis-plugin-hmr "${HMR_PIN}"; fi; \
    dsh --version || true; \
    pnpm --version; \
    npm cache clean --force; \
    chmod -R a+rX /usr/local/lib/node_modules

# ── docpipe runtime ─────────────────────────────────────────────────────────
# `umask 0022` is not decoration: this build host runs containers with umask
# 0027, so directories created here would be mode 0750 root:root and the
# unprivileged runtime user could not traverse /opt/venv at all ("Permission
# denied" on /opt/venv/bin, then `docpipe: not found`). The chmod is the
# belt-and-braces for layers copied in with restrictive modes.
#
# Dependencies and project code are TWO layers on purpose. A one-line change to
# docpipe/ used to invalidate the whole install and re-download ~200 MB of wheels
# (daft alone is 56 MB) over a slow link on every rebuild. requirements.txt is
# GENERATED from pyproject.toml by tools/build_image.sh, so there is still exactly
# one place that declares dependencies — and no heredoc, which this builder
# cannot parse.
WORKDIR /opt/docpipe
COPY pyproject.toml README.md requirements.txt /opt/docpipe/
RUN set -eux; \
    umask 0022; \
    python -m venv /opt/venv; \
    /opt/venv/bin/pip install --index-url "${PIP_INDEX_URL}" --timeout "${PIP_TIMEOUT}" --retries "${PIP_RETRIES}" --upgrade pip setuptools wheel; \
    /opt/venv/bin/pip install --index-url "${PIP_INDEX_URL}" --timeout "${PIP_TIMEOUT}" --retries "${PIP_RETRIES}" -r /opt/docpipe/requirements.txt; \
    chmod -R a+rX /opt/venv

COPY docpipe/ ./docpipe/
RUN set -eux; \
    umask 0022; \
    /opt/venv/bin/pip install --no-deps --no-build-isolation .; \
    cd /tmp && /opt/venv/bin/python -c "import docpipe, pypdfium2, pptx, docx, pdfplumber; print('imports ok', docpipe.__version__)"; \
    # Assert the external tools the operators call actually exist. A package list is
    # a claim; a lookup is evidence, and the last claim about tesseract was false.
    for tool in pdftotext pdffonts pdftoppm pdfinfo tesseract; do \
        command -v "$tool" > /dev/null || { echo "MISSING TOOL: $tool"; exit 1; }; \
    done; \
    tesseract --version | head -1; \
    rm -rf /opt/docpipe/build /opt/docpipe/*.egg-info

# ── agent preset, skills, entrypoints ───────────────────────────────────────
COPY presets/docpipe/ /opt/dsh-home/.agent-presets/docpipe/
COPY presets/settings.yaml /opt/dsh-home/settings.yaml
# Pre-initialised so the container does not auto-initialise on first run, and
# so the HMR row the shipped headless bundles omit is present. See the header
# of presets/profiles/headless/cordis.patch.yml.
COPY presets/profiles/ /opt/dsh-home/profiles/
# The profile module fallback is created EMPTY and warmed, because dsh manages it
# itself as symlinks into the installation.
#
# `healProfilesModuleFallback` fills `<DSH_HOME>/profiles/node_modules` at boot.
# The directory must exist for that to happen, and it must contain no real files:
# copying packages in makes the heal refuse to run at all ("exists and is not a
# symlink; remove it so dsh can manage the installation fallback"). Warming it at
# build time ships the image with the same ~197-entry fallback a working install
# has, so the first boot does not have to create it.
#
# This does NOT by itself make `dsh --profile …` boot in this container: with the
# fallback fully populated, the loader still fails to register the `hmr` service
# that `runProfile` auto-loads, and every profile (shipped `headless` and `web`
# alike) aborts before any preset mounts with "user patch-layer watching requires
# the Cordis HMR service". The identical dsh version boots fine on the host, so
# the remaining cause is environmental and unfixed. See docs/STATUS.md.
RUN set -eux; \
    umask 0022; \
    mkdir -p /opt/dsh-home/profiles/node_modules; \
    chmod 0755 /opt/dsh-home/profiles /opt/dsh-home/profiles/node_modules; \
    dsh plugin --profile headless list >/dev/null 2>&1 || true; \
    dsh --profile headless warmup >/dev/null 2>&1 || true; \
    links=$(find /opt/dsh-home/profiles/node_modules/@deepseek-ai -maxdepth 1 | wc -l); \
    echo "profile module fallback: $links entries"; \
    test "$links" -gt 50; \
    test -e /opt/dsh-home/profiles/node_modules/@deepseek-ai/cordis-plugin-hmr; \
    chmod -R a+rX /opt/dsh-home/profiles; \
    echo "warm-up populated the fallback; ownership is fixed by the layer that creates the runtime user"
COPY skills/ /opt/skills/
COPY tools/skills-link.sh /opt/docpipe/tools/skills-link.sh
COPY docker/entrypoint.sh /usr/local/bin/docpipe-entrypoint
COPY docker/healthcheck.sh /usr/local/bin/docpipe-healthcheck
RUN set -eux; \
    umask 0022; \
    mkdir -p /work /out /in /opt/dsh-home/skills; \
    chmod 0755 /usr/local/bin/docpipe-entrypoint /usr/local/bin/docpipe-healthcheck /opt/docpipe/tools/skills-link.sh; \
    chmod 0644 /opt/dsh-home/settings.yaml; \
    chmod -R a+rX /opt/skills /opt/dsh-home; \
    /opt/docpipe/tools/skills-link.sh; \
    groupadd -g 1000 docpipe 2>/dev/null || true; \
    useradd -u 1000 -g 1000 -m -s /bin/bash docpipe 2>/dev/null || true; \
    chown -R 1000:1000 /work /out /opt/dsh-home /opt/docpipe; \
    chmod 0755 /work /out /in; \
    su docpipe -c 'touch /work/.write-probe && rm /work/.write-probe && echo work-writable'; \
    su docpipe -c 'command -v docpipe dsh node pdftotext'

ENV DOCPIPE_WORK=/work \
    DOCPIPE_OUT=/out \
    DOCPIPE_IN=/in

USER 1000:1000
WORKDIR /work

HEALTHCHECK --interval=60s --timeout=20s --start-period=10s --retries=2 \
    CMD ["/usr/local/bin/docpipe-healthcheck"]

# docpipe run … | dsh … | shell | selftest
ENTRYPOINT ["/usr/local/bin/docpipe-entrypoint"]
CMD ["docpipe", "--help"]
