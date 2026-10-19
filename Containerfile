FROM quay.io/rockylinux/rockylinux:10
LABEL name="Liquidator" \
 vendor="Rhoods_Corp" \
 license="GPLv2"
RUN dnf install -y python3 python3-pip procps-ng postgresql && dnf clean all
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY liquidator ./liquidator
COPY migrations ./migrations
COPY tests ./tests
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir ".[test]"
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1
CMD ["liquidator", "web"]
HEALTHCHECK --interval=1m --timeout=5s CMD liquidator healthcheck || exit 1
