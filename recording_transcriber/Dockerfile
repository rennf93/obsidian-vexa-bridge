# Recording-only mode: build on the stack's own Vexa transcription worker image, so the
# faster-whisper build and model weights match the live worker, and add a Postgres driver
# plus the speaker diarization stack (sherpa-onnx embeddings, scipy clustering).
# BASE_IMAGE is whatever tag your transcription-worker service runs.
ARG BASE_IMAGE=vexa-transcription:cpu-v012
FROM ${BASE_IMAGE}
RUN uv pip install --python /opt/venv/bin/python --no-cache "psycopg[binary]==3.3.6" "sherpa-onnx==1.13.8" "scipy==1.17.1"
# scipy 1.17.x keeps the base image's numpy 1.26.4 (1.18 needs numpy 2 and would upgrade it under faster-whisper).
# NeMo TitaNet small speaker embeddings (40 MB). The base image has curl but no sha256sum, so python verifies it.
RUN mkdir -p /app/diarization \
    && curl -fsSL -o /app/diarization/titanet_small.onnx \
        https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx \
    && python -c "import hashlib,sys; d=hashlib.sha256(open('/app/diarization/titanet_small.onnx','rb').read()).hexdigest(); sys.exit(0 if d=='ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e' else 'titanet_small.onnx sha256 mismatch: '+d)"
ENV PYTHONPATH=/app
COPY __init__.py transcribe_recordings.py diarize.py /app/recording_transcriber/
# The base image checks the worker's HTTP /health, which this batch job does not serve.
HEALTHCHECK NONE
ENTRYPOINT ["python", "-m", "recording_transcriber.transcribe_recordings"]
CMD []
