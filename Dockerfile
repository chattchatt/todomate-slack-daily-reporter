FROM node:24-bookworm-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY send-todomate-slack-report.py run.sh ./
RUN chmod +x ./run.sh ./send-todomate-slack-report.py

ENV PATH="/app/node_modules/.bin:${PATH}"
CMD ["./run.sh"]
