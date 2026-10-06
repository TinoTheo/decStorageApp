FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    STATE_DIR=/state

RUN useradd --create-home --uid 10001 agent \
    && mkdir /state && chown agent:agent /state
COPY dstore_agent.py /app/dstore_agent.py
RUN sed -i 's/\r$//' /app/dstore_agent.py

USER agent
VOLUME /state
CMD ["python", "/app/dstore_agent.py"]
