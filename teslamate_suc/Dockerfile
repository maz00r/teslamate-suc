ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# Python stdlib + psycopg 3 (PostgreSQL driver) + libpq from apk (without it: "no pq wrapper available"). HTTP via urllib.
RUN apk add --no-cache python3 py3-psycopg libpq tzdata

COPY *.py panel.html /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONUNBUFFERED=1 \
    SUC_DATA_DIR=/data
WORKDIR /app
CMD [ "/run.sh" ]
