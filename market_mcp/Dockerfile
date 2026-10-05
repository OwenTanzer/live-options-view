FROM node:22-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --chown=node:node market_mcp/auth.js market_mcp/production.js market_mcp/source.js market_mcp/consumer.js market_mcp/server.js market_mcp/http.js ./market_mcp/
COPY --chown=node:node docs/shared.js docs/squeeze-calendar.json ./docs/
USER node
STOPSIGNAL SIGTERM
CMD ["node", "--max-old-space-size=256", "market_mcp/production.js"]
