FROM node:24-bookworm-slim

WORKDIR /srv

RUN corepack enable

COPY package.json ./
RUN npm install

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
