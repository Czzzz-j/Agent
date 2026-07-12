from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


def main() -> None:
    parser = argparse.ArgumentParser(description="Download local reranker models")
    parser.add_argument(
        "--repo-id",
        default="BAAI/bge-reranker-base",
        help="Hugging Face repository id",
    )
    parser.add_argument(
        "--target-dir",
        default="models/bge-reranker-base",
        help="Local directory for the model snapshot",
    )
    parser.add_argument("--revision", default=None)
    args = parser.parse_args()

    target_dir = Path(args.target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=args.repo_id,
        local_dir=str(target_dir),
        revision=args.revision,
        local_dir_use_symlinks=False,
    )
    print(f"Downloaded {args.repo_id} to {target_dir}")


if __name__ == "__main__":
    main()

