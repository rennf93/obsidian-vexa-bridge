# Recording-only mode: build on the stack's own Vexa transcription worker image, so the
# faster-whisper build and model weights match the live worker, and add a Postgres driver.
# BASE_IMAGE is whatever tag your transcription-worker service runs.
ARG BASE_IMAGE=vexa-transcription:cpu-v012
FROM ${BASE_IMAGE}
RUN uv pip install --python /opt/venv/bin/python --no-cache "psycopg[binary]==3.3.6"
COPY transcribe_recordings.py /app/transcribe_recordings.py
ENTRYPOINT ["python", "/app/transcribe_recordings.py"]
CMD []
