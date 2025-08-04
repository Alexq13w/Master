FROM postgres:14

RUN apt-get update && apt-get install -y pgbouncer

COPY pgbouncer.ini /etc/pgbouncer/
COPY userlist.txt /etc/pgbouncer/

CMD ["sh", "-c", "pgbouncer -d /etc/pgbouncer/pgbouncer.ini"]
