FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md paper_strategy_v1.json paper_strategy_v2.json ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 paper

USER 1000:1000
VOLUME ["/runtime"]
EXPOSE 8080
ENTRYPOINT ["sniper-paper"]
CMD ["--database", "/runtime/paper.db", "--dashboard-host", "0.0.0.0", "--dashboard-port", "8080", "--allow-nonloopback-dashboard"]
