FROM node:22-bookworm-slim
WORKDIR /app
COPY backend/prediction-model.js backend/package.json ./backend/
COPY market_data/prediction ./market_data/prediction
USER node
ENTRYPOINT ["node", "market_data/prediction/run.mjs"]
