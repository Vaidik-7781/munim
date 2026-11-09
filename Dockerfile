FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]"
ENV MUNIM_ROOT=/data
VOLUME /data
# secrets come from the environment (see .env.example); never bake them into the image
CMD ["python", "-m", "munim.app"]
