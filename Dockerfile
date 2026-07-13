FROM python:3.12-slim
WORKDIR /app

# Set at build time from the git tag being built (see
# .github/workflows/docker-publish.yml) so the running app always knows
# its own version without anyone having to hand-maintain a version string.
# Defaults to "dev" for a plain local `docker build` with no --build-arg.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
# Note: intentionally left running as root. It shares a Docker named
# volume with ddns-updater, and pinning this to a non-root UID risks
# permission mismatches against whatever UID that image writes as.
# Acceptable trade-off for a small internal-only homelab tool.
EXPOSE 5000
CMD ["python", "app.py"]
