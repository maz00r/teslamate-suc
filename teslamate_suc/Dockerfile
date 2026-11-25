ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# Python stdlib + tzdata only. HTTP via urllib, no extra libraries.
RUN apk add --no-cache python3 tzdata

COPY *.py panel.html /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONUNBUFFERED=1 \
    SUC_DATA_DIR=/data
WORKDIR /app
CMD [ "/run.sh" ]
