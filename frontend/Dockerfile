# =============================================================================
# Frontend Dockerfile — 多階段建置
# Stage 1: node:20-alpine 編譯 React + Vite -> 靜態檔
# Stage 2: nginx:alpine 服務 /usr/share/nginx/html (SPA)
# =============================================================================

# ---- Stage 1: build ---------------------------------------------------------
FROM node:20-alpine AS build

WORKDIR /app

# 先複製套件描述檔以善用 layer cache
COPY package.json ./
# 若有 lockfile 使用 npm ci (可重現安裝), 否則退回 npm install
COPY package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# 複製原始碼並建置
COPY . .
RUN npm run build

# ---- Stage 2: serve ---------------------------------------------------------
FROM nginx:alpine

# SPA 用 nginx 設定 (try_files -> /index.html)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 從 build stage 取出產物
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
