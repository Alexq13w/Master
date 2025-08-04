FROM bitnami/pgbouncer:latest

# حل مشاكل DNS والاتصال
USER root
RUN apt-get update && apt-get install -y dnsutils iputils-ping
USER 1001

# تعيين متغيرات البيئة الأساسية
ENV POSTGRESQL_USER="dbmaster_yehk_user"
ENV POSTGRESQL_PASSWORD="FL6nmuzBXyf2EpnPQmYrHMTI0C2tc6Q0"
ENV PGBOUNCER_DISABLE_PING="no"

# نسخ ملفات الإعداد
COPY pgbouncer.ini /opt/bitnami/pgbouncer/conf/
COPY userlist.txt /opt/bitnami/pgbouncer/conf/

# اختبار الاتصال أثناء البناء
RUN ping -c 4 dpg-d23vame3jp1c73ae9650-a.oregon-postgres.render.com
