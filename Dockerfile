FROM python:3.12-slim

# Install uv for fast, reproducible dependency installs
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy lockfile first so dependency layer is cached separately from source
COPY pyproject.toml uv.lock ./

# Force CPU-only PyTorch — sentence-transformers otherwise pulls ~2.5 GB of
# NVIDIA CUDA wheels that are useless on a Mac/NAS deployment.
ENV UV_TORCH_BACKEND=cpu
RUN uv sync --frozen --no-dev

# Copy application source
COPY . .

# Make the venv's Python the default
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Chainlit needs a writable home for its runtime cache
ENV HOME=/tmp

CMD ["python", "main.py", "ui"]
