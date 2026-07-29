# Pinned to the exact Playwright version in requirements.txt. Railpack's
# `buildCommand` (the previous approach here) ran `playwright install
# --with-deps chromium` as a build step, but Railpack separates build-layer
# contents (including apt-installed libraries and Playwright's browser
# cache under ~/.cache/ms-playwright) from what actually gets copied into
# the final deploy image -- a build command succeeding is not evidence the
# browser survives into the image the bot actually runs in. The official
# Microsoft image bakes browsers matching a specific Playwright release
# directly into the runtime image itself, sidestepping that build/deploy
# layer split entirely. The image tag and requirements.txt's `playwright`
# pin must be bumped together -- a mismatch leaves the pip package
# importable but unable to find a working Chromium, which is exactly what
# scripts/smoke_browser_launch.py below exists to catch at build time
# instead of on a live deploy.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Prove *this exact image* can launch Chromium and load a page before
# copying in application code or shipping it -- not just that `pip install`
# and `import playwright` succeeded, which a version-mismatched browser
# cache would still let happen silently.
COPY scripts/smoke_browser_launch.py scripts/smoke_browser_launch.py
RUN python scripts/smoke_browser_launch.py

COPY . ./

CMD ["python", "-m", "crassus.runner", "--interval", "300"]
