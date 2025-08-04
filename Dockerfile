FROM bitnami/pgbouncer:latest

ENV POSTGRESQL_USER="dbmaster_yehk_user"
ENV POSTGRESQL_PASSWORD="FL6nmuzBXyf2EpnPQmYrHMTI0C2tc6Q0"
ENV PGBOUNCER_DISABLE_PING="no"

COPY pgbouncer.ini /opt/bitnami/pgbouncer/conf/
COPY userlist.txt /opt/bitnami/pgbouncer/conf/
