#!/usr/bin/env python3
"""
Download Hugging Face assets for air-gapped MiroShark deploys.

Populates:
  * HF model cache  ($HF_HOME/hub/…) — reranker + Twitter recsys models
  * Demographic parquet snapshots (backend/data/nemotron/<country>/) — optional

Run on a network-connected host, then copy the output directories to the
air-gapped machine (see docker-compose.offline-cache.yml).

Example (repo root, connected machine):
  docker compose -f docker-compose.offline-cache.yml run --rm prepare-offline-cache

Example with demographic datasets:
  docker compose -f docker-compose.offline-cache.yml run --rm prepare-offline-cache \\
    --include-demographic-datasets --demographic-country us --demographic-country sg
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("miroshark.prepare_offline_cache")

# Default matches Config.RERANKER_MODEL (app/config.py). Runtime loads this env var via
# RerankerService — offline prep must cache the same repo, not a hardcoded duplicate.
_DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
# Hardcoded in wonderwall/social_platform/recsys.py (no RECSYS_MODEL env).
RECSYS_MODEL = "Twitter/twhin-bert-base"


def _resolve_reranker_model() -> str:
    return os.environ.get("RERANKER_MODEL", _DEFAULT_RERANKER_MODEL).strip()


def _default_hf_models() -> list[str]:
    return [_resolve_reranker_model(), RECSYS_MODEL]

COUNTRIES_DIR = Path(__file__).resolve().parent.parent / "app" / "countries"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Hugging Face models/datasets for offline MiroShark.",
    )
    parser.add_argument(
        "--hf-home",
        default=os.environ.get("HF_HOME", "/output/hf_cache"),
        help="Hugging Face cache root (default: $HF_HOME or /output/hf_cache).",
    )
    parser.add_argument(
        "--backend-data-dir",
        default=os.environ.get("BACKEND_DATA_DIR", "/output/backend_data"),
        help="Backend data root for Nemotron snapshots (default: $BACKEND_DATA_DIR or /output/backend_data).",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        metavar="REPO_ID",
        help="Extra HF model repo to cache (repeatable). Defaults: $RERANKER_MODEL + twhin.",
    )
    parser.add_argument(
        "--include-demographic-datasets",
        action="store_true",
        help="Download demographic persona datasets for all packs in backend/app/countries/.",
    )
    parser.add_argument(
        "--demographic-country",
        action="append",
        dest="demographic_countries",
        metavar="CODE",
        help="Limit demographic download to country codes (us, sg). Repeatable; implies --include-demographic-datasets.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip offline load test after download.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def _load_country_packs(codes: list[str]) -> list[dict]:
    if not COUNTRIES_DIR.is_dir():
        raise FileNotFoundError(f"Country packs not found: {COUNTRIES_DIR}")

    packs: list[dict] = []
    for path in sorted(COUNTRIES_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            pack = json.load(fh)
        code = (pack.get("code") or path.stem).lower()
        if codes != ["all"] and code not in codes:
            continue
        packs.append(pack)
    if not packs:
        wanted = ", ".join(codes)
        raise ValueError(f"No country packs matched: {wanted}")
    return packs


def _download_hf_models(hf_home: Path, models: list[str]) -> None:
    from huggingface_hub import snapshot_download

    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)

    for repo_id in models:
        logger.info("Downloading HF model: %s", repo_id)
        snapshot_download(repo_id=repo_id)
        logger.info("Cached model: %s", repo_id)


def _resolve_pack_download_dir(pack: dict, backend_data_dir: Path) -> Path:
    """Resolve download_dir from a country pack (repo-relative paths)."""
    ds = pack.get("dataset") or {}
    download_dir = ds.get("download_dir")
    if download_dir:
        prefix = "backend/data/"
        rel = download_dir.strip()
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
        return backend_data_dir / rel
    code = pack.get("code", "unknown")
    return backend_data_dir / "nemotron" / code


def _download_demographic_datasets(backend_data_dir: Path, packs: list[dict]) -> None:
    from huggingface_hub import snapshot_download

    for pack in packs:
        ds = pack.get("dataset") or {}
        repo_id = ds.get("repo_id")
        if not repo_id:
            logger.warning("Skipping %s — no dataset.repo_id", pack.get("code"))
            continue

        code = pack.get("code", "unknown")
        download_dir = _resolve_pack_download_dir(pack, backend_data_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading demographic dataset %s -> %s", repo_id, download_dir)
        snapshot_download(
            repo_id=repo_id,
            repo_type="dataset",
            allow_patterns=ds.get("allow_patterns") or ["data/train-*", "README.md"],
            local_dir=str(download_dir),
            max_workers=4,
        )
        parquet_glob = list(download_dir.glob("data/train-*"))
        if not parquet_glob:
            raise RuntimeError(
                f"Demographic dataset download for {code} finished but no data/train-* files in {download_dir}"
            )
        logger.info("Demographic dataset ready for %s (%d parquet shard(s))", code, len(parquet_glob))


def _verify_offline(hf_home: Path, models: list[str], verbose: bool) -> None:
    """Verify cached models load offline, in a fresh subprocess.

    huggingface_hub reads HF_HUB_OFFLINE into a module-level constant at
    import time (constants.py). _download_hf_models() above already imported
    huggingface_hub before any offline env var existed, so setting
    os.environ here would not change that already-cached constant for the
    rest of this process — some internal calls (e.g. sentence-transformers'
    post-load model-info lookup) check the constant, not local_files_only,
    and would silently still hit the network. A subprocess started with the
    env vars already set sidesteps that.
    """
    env = dict(os.environ)
    env["HF_HOME"] = str(hf_home)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["_MIROSHARK_VERIFY_MODELS"] = ",".join(models)
    env["_MIROSHARK_VERIFY_RERANKER_MODEL"] = _resolve_reranker_model()

    logger.info("Verifying offline load in a fresh subprocess (HF_HUB_OFFLINE=1)…")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_verify-worker"]
    if verbose:
        cmd.append("-v")
    subprocess.run(cmd, env=env, check=True)


def _verify_offline_worker(verbose: bool) -> None:
    """Runs inside the subprocess spawned by _verify_offline(), with
    HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE already set before any HF library
    was imported."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )
    models = [m for m in os.environ.get("_MIROSHARK_VERIFY_MODELS", "").split(",") if m]
    reranker_model = os.environ.get("_MIROSHARK_VERIFY_RERANKER_MODEL", "").strip()

    if reranker_model and reranker_model in models:
        from sentence_transformers import CrossEncoder

        CrossEncoder(
            reranker_model,
            max_length=512,
            device="cpu",
            local_files_only=True,
        )
        logger.info("Verified reranker: %s", reranker_model)

    if RECSYS_MODEL in models:
        from transformers import AutoModel, AutoTokenizer

        AutoTokenizer.from_pretrained(RECSYS_MODEL, local_files_only=True)
        AutoModel.from_pretrained(RECSYS_MODEL, local_files_only=True)
        logger.info("Verified recsys model: %s", RECSYS_MODEL)

    known = {reranker_model, RECSYS_MODEL} - {""}
    for repo_id in models:
        if repo_id in known:
            continue
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=repo_id, local_files_only=True)
        logger.info("Verified cached model: %s", repo_id)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    seen: set[Path] = set()
    total = 0
    for entry in path.rglob("*"):
        if not entry.is_file():
            continue
        try:
            real = entry.resolve()
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        try:
            total += real.stat().st_size
        except OSError:
            continue
    return total


def _human_size(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def _print_summary(hf_home: Path, backend_data_dir: Path, downloaded_demographics: bool) -> None:
    nemotron_dir = backend_data_dir / "nemotron"
    print("\n=== Offline cache ready ===")
    print(f"HF cache:              {hf_home} ({_human_size(_dir_size(hf_home))})")
    if downloaded_demographics:
        print(f"Nemotron datasets:     {nemotron_dir} ({_human_size(_dir_size(nemotron_dir))})")
    print("\nExport volumes for transfer (connected host):")
    print("  docker compose -f docker-compose.offline-cache.yml --profile export run --rm export-offline-cache")
    print("\nImport on the air-gapped host (after copying offline-cache-export/*.tgz):")
    print("  docker compose -f docker-compose.offline-cache.yml --profile import run --rm import-offline-cache")
    print("\nSet in the runtime environment:")
    print("  HF_HUB_OFFLINE=1")
    print("  TRANSFORMERS_OFFLINE=1")


def main() -> int:
    if "--_verify-worker" in sys.argv:
        _verify_offline_worker(verbose="-v" in sys.argv or "--verbose" in sys.argv)
        return 0

    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    hf_home = Path(args.hf_home).resolve()
    backend_data_dir = Path(args.backend_data_dir).resolve()
    models = _default_hf_models()
    if args.models:
        for extra in args.models:
            if extra not in models:
                models.append(extra)

    extra = os.environ.get("EXTRA_HF_MODELS", "").strip()
    if extra:
        for item in extra.split(","):
            item = item.strip()
            if item and item not in models:
                models.append(item)

    try:
        _download_hf_models(hf_home, models)

        downloaded_demographics = False
        if args.include_demographic_datasets or args.demographic_countries:
            codes = [c.lower() for c in (args.demographic_countries or ["all"])]
            packs = _load_country_packs(codes)
            _download_demographic_datasets(backend_data_dir, packs)
            downloaded_demographics = True

        if not args.skip_verify:
            _verify_offline(hf_home, models, args.verbose)

        _print_summary(hf_home, backend_data_dir, downloaded_demographics)
        return 0
    except Exception as exc:
        logger.error("Offline cache preparation failed: %s", exc)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
