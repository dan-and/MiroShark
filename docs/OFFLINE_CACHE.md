# Offline Hugging Face cache

MiroShark downloads a few Hugging Face **models** and (optionally) **datasets** at runtime. In environments without access to `huggingface.co`, you must pre-populate those assets on a connected machine and transfer them to the air-gapped host.

This repo ships a dedicated Docker workflow: HF models land in the **`miroshark_hf_cache`** named volume (shared with runtime compose files), and Nemotron persona parquets land under **`./backend/data/nemotron/`** — the same bind-mount path the runtime stack uses (`./backend/data:/app/backend/data`).

## Storage layout

| Location | Mounted in runtime at | Used for |
| -------- | --------------------- | -------- |
| `miroshark_hf_cache` (named volume) | `/root/.cache/huggingface` | Reranker + Twitter recsys models |
| `./backend/data/nemotron/` (host bind mount) | `/app/backend/data/nemotron` | Demographic grounding parquets (optional) |
| `./backend/uploads/` (host bind mount) | `/app/backend/uploads` | Uploaded documents + simulation artifacts |

Runtime compose files bind-mount `./backend/data` so Nemotron datasets survive container recreation (see [#238](https://github.com/aaronjmars/MiroShark/pull/238)).

## What gets cached

| Asset | Hugging Face id | Used by | Path | Approx. size |
| ----- | --------------- | ------- | ---- | ------------ |
| Reranker | `BAAI/bge-reranker-v2-m3` | Graph-memory hybrid search (`RerankerService`) | `miroshark_hf_cache/hub/models--BAAI--bge-reranker-v2-m3/` | ~2.2 GB |
| Twitter recsys | `Twitter/twhin-bert-base` | Wonderwall Twitter timeline embeddings | `miroshark_hf_cache/hub/models--Twitter--twhin-bert-base/` | ~1.1 GB |
| US personas (optional) | `nvidia/Nemotron-Personas` | Demographic grounding (`DEMOGRAPHICS_COUNTRY=us`) | `backend/data/nemotron/usa/data/train-*.parquet` | hundreds of MB |
| Singapore personas (optional) | `nvidia/Nemotron-Personas-Singapore` | Demographic grounding (`DEMOGRAPHICS_COUNTRY=sg`) | `backend/data/nemotron/singapore/data/train-*.parquet` | hundreds of MB |

**Standard `miroshark_hf_cache` bundle (reranker + twhin): ~3.2 GB.**

Embeddings for graph search use your configured LLM/Ollama provider — they are **not** part of this cache.

### Bundle sizes

| Bundle | Contents | When you need it |
| ------ | -------- | ---------------- |
| **Minimal** | `bge-reranker-v2-m3` only | Reranker on, no Twitter platform |
| **Standard** | + `twhin-bert-base` | Default multi-platform simulations |
| **Full** | + Nemotron parquets | `DEMOGRAPHICS_COUNTRY` set to `us` and/or `sg` |

## Quick start (connected machine)

From the repo root, with Docker and outbound HTTPS:

```bash
# Build the prep image (first run only) and download core models
docker compose -f docker-compose.offline-cache.yml run --rm prepare-offline-cache
```

Output is written to:

- `miroshark_hf_cache` — Hugging Face Hub model cache (named volume)
- `./backend/data/nemotron/` — only populated when `--include-demographic-datasets` is used

The script finishes with an **offline verification** step: it re-execs itself in a fresh subprocess with `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1` set before any Hugging Face library is imported (setting those vars mid-process wouldn't work — `huggingface_hub` reads them into a module constant once, at import time), loads each model with `local_files_only=True`, and fails if anything is missing.

Start the runtime stack normally — it mounts the same paths:

```bash
docker compose up -d
```

### Include demographic datasets

```bash
docker compose -f docker-compose.offline-cache.yml run --rm prepare-offline-cache \
  --include-demographic-datasets --demographic-country us --demographic-country sg
```

To fetch every country pack in `backend/app/countries/`:

```bash
docker compose -f docker-compose.offline-cache.yml run --rm prepare-offline-cache \
  --include-demographic-datasets
```

### Custom reranker model

If you override `RERANKER_MODEL` in `.env`, cache that repo too:

```bash
RERANKER_MODEL=my-org/my-reranker \
  docker compose -f docker-compose.offline-cache.yml run --rm prepare-offline-cache
```

Or pass `--model` explicitly:

```bash
docker compose -f docker-compose.offline-cache.yml run --rm prepare-offline-cache \
  --model my-org/my-reranker
```

Comma-separated extras via env (compose forwards this):

```bash
EXTRA_HF_MODELS=org/model-a,org/model-b \
  docker compose -f docker-compose.offline-cache.yml run --rm prepare-offline-cache
```

## Files involved

| File | Role |
| ---- | ---- |
| [`Dockerfile.offline-cache`](../Dockerfile.offline-cache) | Slim image: backend Python deps + prep script |
| [`docker-compose.offline-cache.yml`](../docker-compose.offline-cache.yml) | Prep, export, and import services |
| [`backend/scripts/prepare_offline_cache.py`](../backend/scripts/prepare_offline_cache.py) | Download, verify, and print transfer instructions |

## Script reference

Run inside the prep container (default entrypoint) or locally after `cd backend && uv sync`:

```bash
uv run python scripts/prepare_offline_cache.py --help
```

| Flag / env | Meaning |
| ---------- | ------- |
| `--hf-home` / `HF_HOME` | Hugging Face cache root (default `/output/hf_cache` in Docker) |
| `--backend-data-dir` / `BACKEND_DATA_DIR` | Backend data root for Nemotron snapshots (default `/output/backend_data` in Docker) |
| `--model REPO_ID` | Extra HF model to cache (repeatable) |
| `--include-demographic-datasets` | Download Nemotron datasets for all country packs |
| `--demographic-country CODE` | Limit download to `us`, `sg`, … (repeatable) |
| `--skip-verify` | Skip offline load test |
| `-v` / `--verbose` | Debug logging |
| `RERANKER_MODEL` | If set, also cached (in addition to defaults) |
| `EXTRA_HF_MODELS` | Comma-separated extra model repos |
| `OFFLINE_CACHE_ARCHIVE_DIR` | Host directory for `.tgz` export/import (default `./offline-cache-export`) |

### Export / import services

| Service | Profile | Command |
| ------- | ------- | ------- |
| `export-offline-cache` | `export` | `docker compose -f docker-compose.offline-cache.yml --profile export run --rm export-offline-cache` |
| `import-offline-cache` | `import` | `docker compose -f docker-compose.offline-cache.yml --profile import run --rm import-offline-cache` |

## Deploy on the air-gapped host

### Option A — Re-run prep on the offline host

If the offline host can run Docker images (but not reach Hugging Face), transfer the prep image and run the same `prepare-offline-cache` command there with outbound access disabled after the image is loaded.

### Option B — Export / import archives

After prep finishes on the connected machine, export the HF cache volume to a portable `.tgz` file (default output directory: `./offline-cache-export/`):

```bash
docker compose -f docker-compose.offline-cache.yml --profile export run --rm export-offline-cache
```

This writes:

- `offline-cache-export/miroshark_hf_cache.tgz` — always (fails if the HF volume is empty)
- `offline-cache-export/miroshark_nemotron_data.tgz` — only when `backend/data/nemotron/` has data

Copy the archives **and** the `backend/data/nemotron/` directory to the air-gapped host (`scp`, `rsync`, USB, etc.). Override the host directory with `OFFLINE_CACHE_ARCHIVE_DIR` if needed:

```bash
OFFLINE_CACHE_ARCHIVE_DIR=/path/to/archives \
  docker compose -f docker-compose.offline-cache.yml --profile export run --rm export-offline-cache
```

On the offline host, place the `.tgz` files in `offline-cache-export/` (or your `OFFLINE_CACHE_ARCHIVE_DIR`) and import:

```bash
docker compose -f docker-compose.offline-cache.yml --profile import run --rm import-offline-cache
```

Compose creates `miroshark_hf_cache` if it does not exist yet. Nemotron parquets are extracted into `./backend/data/nemotron/` on the host.

<details>
<summary>Manual <code>docker run</code> equivalent (no compose)</summary>

On the connected machine:

```bash
docker run --rm \
  -v miroshark_hf_cache:/data \
  -v $(pwd):/backup \
  alpine tar -czf /backup/miroshark_hf_cache.tgz -C /data .

# if you downloaded Nemotron datasets:
tar -czf miroshark_nemotron_data.tgz -C backend/data/nemotron .
```

On the offline host:

```bash
docker volume create miroshark_hf_cache
docker run --rm \
  -v miroshark_hf_cache:/data \
  -v $(pwd):/backup \
  alpine sh -c "cd /data && tar -xzf /backup/miroshark_hf_cache.tgz"

mkdir -p backend/data/nemotron
tar -xzf miroshark_nemotron_data.tgz -C backend/data/nemotron
```

</details>

### Enable offline mode

Set in `.env` or compose `environment`:

```bash
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

`docker-compose.yml`, `docker-compose.traefik.yml`, and `docker-compose.ollama.yml` default both to `0` (online). Set to `1` on air-gapped hosts.

The reranker also passes `local_files_only=True` to `CrossEncoder` when `HF_HUB_OFFLINE=1` (see `backend/app/storage/reranker_service.py`).

### Demographic datasets (optional)

If you use demographic grounding, ensure parquets are present at the paths declared in `backend/app/countries/*.json`:

```
backend/data/nemotron/usa/data/train-*.parquet
backend/data/nemotron/singapore/data/train-*.parquet
```

These paths are bind-mounted into the container at `/app/backend/data/nemotron/…`.

### Python dependencies

The runtime image must include `protobuf` and `sentencepiece` (declared in `backend/pyproject.toml`). They are required to load the BGE reranker tokenizer offline.

### Verify

After importing volumes and starting MiroShark, successful logs look like:

```
Loading cross-encoder reranker: BAAI/bge-reranker-v2-m3 (device=cpu, local_files_only=True)
Reranker ready: BAAI/bge-reranker-v2-m3
```

## Cache directory layout

Inside `miroshark_hf_cache`, Hugging Face Hub stores models under `hub/`:

```
hub/
├── models--BAAI--bge-reranker-v2-m3/
│   ├── blobs/
│   ├── refs/main
│   └── snapshots/<revision>/
│       ├── config.json
│       ├── model.safetensors
│       ├── tokenizer.json
│       └── sentencepiece.bpe.model
└── models--Twitter--twhin-bert-base/
    └── snapshots/<revision>/
        └── ...
```

**Always export/import the entire `hub/models--…` tree** (including `blobs/`). Copying only `snapshots/` breaks symlinks.

Nemotron dataset layout under `backend/data/nemotron/`:

```
usa/data/train-*.parquet
singapore/data/train-*.parquet
```

## Running without Docker

On a connected host with the backend venv:

```bash
cd backend
uv sync

export HF_HOME=/path/to/hf_cache
export BACKEND_DATA_DIR=/path/to/repo/backend/data
uv run python scripts/prepare_offline_cache.py
```

Use the same flags as in the Docker examples (`--include-demographic-datasets`, `--demographic-country`, etc.).

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `requires the protobuf library` | Missing Python package | Rebuild image with current `pyproject.toml` (`protobuf`, `sentencepiece`) |
| `Connection reset by peer` during reranker load | Hub metadata call after weights loaded | Set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` |
| Weights load to 100% then failure | Same as above | Ensure offline env vars; re-run prep script to verify cache |
| `Reranker load failed` → fused scores fallback | Cache incomplete or offline flags unset | Re-run `prepare-offline-cache`; check logs for `local_files_only=True` |
| Twitter sim fails loading `twhin-bert-base` | Model not in cache | Include default bundle or download `Twitter/twhin-bert-base` |
| Demographics silently skipped | Parquets missing | Run with `--include-demographic-datasets`; ensure `backend/data/nemotron/` is populated |
| Cache larger than expected on disk | Normal — `du` on volume dedupes blob storage | Prep script reports deduplicated size in its summary |
| Empty HF cache after `docker compose up` | Prep not run yet | Run `docker-compose.offline-cache.yml` first on a connected host |

### Disable reranker entirely

If you cannot ship the cache and do not need cross-encoder reranking:

```bash
RERANKER_ENABLED=false
```

Hybrid vector + BM25 search still works; only the final rerank step is skipped.

## Related configuration

See [Configuration](CONFIGURATION.md) for:

- `RERANKER_ENABLED`, `RERANKER_MODEL`, `RERANKER_DEVICE`
- `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`
- `DEMOGRAPHICS_COUNTRY` — [Demographics](DEMOGRAPHICS.md)

See [Install](INSTALL.md) for general Docker deployment paths.
