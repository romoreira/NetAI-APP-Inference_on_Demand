#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd
from tqdm import tqdm


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--splits-csv",
        type=str,
        required=True,
        help="splits.csv gerado no treino.",
    )

    parser.add_argument(
        "--out-dir",
        type=str,
        default="dataset/test",
        help="Diretório lido pelo pod/sender.",
    )

    parser.add_argument(
        "--max-frames",
        type=int,
        default=2000,
        help="Use -1 para todos os frames de teste.",
    )

    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copia arquivos em vez de criar symlink.",
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="Limpa o diretório de saída antes de popular.",
    )

    args = parser.parse_args()

    splits_csv = Path(args.splits_csv)
    out_dir = Path(args.out_dir)

    if not splits_csv.exists():
        raise FileNotFoundError(f"Não encontrei splits.csv: {splits_csv}")

    if args.clean and out_dir.exists():
        for p in out_dir.glob("*"):
            if p.is_file() or p.is_symlink():
                p.unlink()

    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(splits_csv)
    df = df[df["split"] == "test"].copy()
    df = df.sort_values("frame_id")

    if args.max_frames > 0:
        df = df.head(args.max_frames).copy()

    if df.empty:
        raise RuntimeError("Nenhum frame com split == test encontrado.")

    rows = []

    for i, row in tqdm(
        list(df.iterrows()),
        total=len(df),
        desc="[prepare] dataset/test",
        ncols=120,
    ):
        src = Path(row["path"])

        if not src.exists():
            raise FileNotFoundError(f"Imagem não existe: {src}")

        frame_id = int(row["frame_id"])
        label = int(row["label"])

        dst = out_dir / f"frame_{len(rows):06d}_src_{frame_id:06d}_y{label}.jpg"

        if dst.exists() or dst.is_symlink():
            dst.unlink()

        if args.copy:
            shutil.copy2(src, dst)
        else:
            try:
                dst.symlink_to(src)
            except Exception:
                shutil.copy2(src, dst)

        rows.append(
            {
                "sender_index": len(rows),
                "source_frame_id": frame_id,
                "label": label,
                "src_path": str(src),
                "dst_path": str(dst),
            }
        )

    labels_df = pd.DataFrame(rows)
    labels_df.to_csv(out_dir / "labels.csv", index=False)

    print("\n========== READY ==========")
    print(f"out_dir: {out_dir}")
    print(f"images:  {len(rows)}")
    print(f"labels:  {out_dir / 'labels.csv'}")


if __name__ == "__main__":
    main()
