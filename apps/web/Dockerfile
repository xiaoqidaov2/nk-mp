FROM node:24-bookworm-slim

WORKDIR /srv

RUN corepack enable

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# 构建产物在镜像里（`tsc -b && vite build`）：生产**不再**跑开发服务器——
# 开发服务器按需转换 + 依赖预构建会中途作废响应（caddy 报
# "aborting with incomplete response"），iPhone/微信真机拿不到 JS 分片 → 白屏。
RUN npm run build

EXPOSE 5173
CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "5173"]
