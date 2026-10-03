# Every dependency (including the IANA tz database) is installed at build time:
# there is no outbound network at run time.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --system --no-create-home tablekeeper
USER tablekeeper

EXPOSE 8080
CMD ["python", "-m", "app.main"]
