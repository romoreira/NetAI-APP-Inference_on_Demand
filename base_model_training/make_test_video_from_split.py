#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import pandas as pd
from tqdm import tqdm


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--splits-csv",
        type=str,
        required=True,
        help="Path to splits.csv generated during training.",
    )

    parser.add_argument(
        "--out-video",
        type=str,
        default="test_video.mp4",
    )

    parser.add_argument(
        "--out-labels",
        type=str,
        default="test_video_labels.csv",
    )

    parser.add_argument(
        "--fps",
        type=float,
        default=30.0,
    )

    parser.add_argument(
        "--max-frames",
        type=int,
        default=3000,
        help="Use -1 for all test frames.",
    )

    parser.add_argument(
        "--only-contiguous",
        action="store_true",
        help="Keep only the largest contiguous sequence inside the test split.",
    )

    args = parser.parse_args()

    splits_csv = Path(args.splits_csv)
    out_video = Path(args.out_video)
    out_labels = Path(args.out_labels)

    df = pd.read_csv(splits_csv)
    df = df[df["split"] == "test"].copy()
    df = df.sort_values("frame_id")

    if df.empty:
        raise RuntimeError("No test frames found in splits.csv")

    if args.only_contiguous:
        frame_ids = df["frame_id"].to_numpy()

        groups = []
        current = [0]

        for i in range(1, len(frame_ids)):
            if frame_ids[i] == frame_ids[i - 1] + 1:
                current.append(i)
            else:
                groups.append(current)
                current = [i]

        groups.append(current)

        largest = max(groups, key=len)
        df = df.iloc[largest].copy()
        df = df.sort_values("frame_id")

        print(
            f"[contiguous] selected frames "
            f"{df['frame_id'].min()}-{df['frame_id'].max()} "
            f"n={len(df)}"
        )

    if args.max_frames > 0:
        df = df.head(args.max_frames).copy()

    first_path = Path(df.iloc[0]["path"])
    first_img = cv2.imread(str(first_path))

    if first_img is None:
        raise RuntimeError(f"Could not read first image: {first_path}")

    h, w = first_img.shape[:2]

    out_video.parent.mkdir(parents=True, exist_ok=True)
    out_labels.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_video), fourcc, args.fps, (w, h))

    rows = []

    for out_idx, row in tqdm(
        list(df.iterrows()),
        total=len(df),
        desc="[video] writing",
        ncols=120,
    ):
        img_path = Path(row["path"])
        img = cv2.imread(str(img_path))

        if img is None:
            print(f"[warning] skipping unreadable image: {img_path}")
            continue

        if img.shape[1] != w or img.shape[0] != h:
            img = cv2.resize(img, (w, h))

        writer.write(img)

        rows.append(
            {
                "video_frame_index": len(rows),
                "source_frame_id": int(row["frame_id"]),
                "block_id": int(row["block_id"]) if "block_id" in row else -1,
                "label": int(row["label"]),
                "path": str(img_path),
            }
        )

    writer.release()

    with out_labels.open("w", newline="") as f:
        writer_csv = csv.DictWriter(
            f,
            fieldnames=[
                "video_frame_index",
                "source_frame_id",
                "block_id",
                "label",
                "path",
            ],
        )
        writer_csv.writeheader()
        writer_csv.writerows(rows)

    print("\n========== SAVED ==========")
    print(f"video:  {out_video}")
    print(f"labels: {out_labels}")
    print(f"frames: {len(rows)}")
    print(f"size:   {w}x{h}")
    print(f"fps:    {args.fps}")


if __name__ == "__main__":
    main()
