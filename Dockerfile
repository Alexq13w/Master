FROM bitnami/pgbouncer:latest

USER root
RUN apt-get update && apt-get install -y dnsutils telnet && apt-get clean
USER 1001

ENV POSTGRESQL_USER="dbmaster_yehk_user"
ENV POSTGRESQL_PASSWORD="FL6nmuzBXyf2EpnPQmYrHMTI0C2tc6Q0"

COPY pgbouncer.ini /opt/bitnami/pgbouncer/conf/
COPY userlist.txt /opt/bitnami/pgbouncer/conf/

RUN nslookup dpg-d23vame3jp1c73ae9650-a.oregon-postgres.render.com
