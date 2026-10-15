FROM python:3.12.10-slim-bookworm
RUN useradd -m -u 1000 studio
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir transformers==5.5.0
COPY --chown=studio:studio app.py engine.py /app/
COPY --chown=studio:studio static /app/static
COPY --chown=studio:studio source.json /app/source.json
USER studio
ENV HOME=/home/studio HF_HOME=/home/studio/.cache/huggingface MODEL_ENABLED=1
EXPOSE 7860
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "4", "--timeout", "120", "app:create_app()"]
