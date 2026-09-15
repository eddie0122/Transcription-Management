# Frontend image: static UI + reverse proxy targeting backend:8000.
# The UI is plain HTML/CSS/JS (no build step), so nginx serves it directly.
FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY static /usr/share/nginx/html

ENV MAX_BODY_SIZE=2g
EXPOSE 80
