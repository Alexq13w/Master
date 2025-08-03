FROM bitnami/pgbouncer:latest

# نسخ ملفات الإعداد
COPY pgbouncer.ini /opt/bitnami/pgbouncer/conf/
COPY userlist.txt /opt/bitnami/pgbouncer/conf/
