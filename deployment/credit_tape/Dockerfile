# OA-242 isolated credit-tape service (slice-only collector + GitHub-gated panel).
# Standard library only; separate from deployment/python (the options producers' image).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY market_data/__init__.py ./market_data/__init__.py
COPY market_data/credit_tape/ ./market_data/credit_tape/

# Private archive lives on a mounted volume; CREDIT_TAPE_ARCHIVE points into it.
CMD ["python", "-u", "-m", "market_data.credit_tape.service"]
