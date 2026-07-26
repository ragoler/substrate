# One image, two roles:
#   concierge — CMD below (uvicorn app.concierge:app :8000)
#   guest     — the ActorTemplate overrides the command to run
#               app.agent.server:app on :80 (the atenet-router's fixed port)
# Build context is the REPO ROOT (see deploy_app.sh) so app/, ateapipb/ and
# frontend/ all bake in. Sharing one image is what lets the concierge register
# a digest-pinned ActorTemplate from its own pod's imageID.
FROM python:3.11-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    FRONTEND_DIR=/srv/frontend

COPY app/requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r /srv/app/requirements.txt

COPY app /srv/app
COPY ateapipb /srv/ateapipb
COPY frontend /srv/frontend

EXPOSE 8000
CMD ["uvicorn", "app.concierge:app", "--host", "0.0.0.0", "--port", "8000"]
