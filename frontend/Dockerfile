# Образ интерфейса (задача A-7): сборка в одном слое, отдача — в другом.
#
# В готовый образ не попадают ни node_modules, ни исходники: nginx отдаёт
# статику, а собирает её промежуточный слой, который в итоговый образ не идёт.

FROM node:20-alpine AS build

WORKDIR /src

# Зависимости до кода: правка компонента не должна пересобирать npm ci.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html

EXPOSE 80
