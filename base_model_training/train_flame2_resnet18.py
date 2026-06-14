#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
from torchvision.models import ResNet18_Weights, resnet18
from tqdm import tqdm


IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass
class Sample:
    path: Path
    frame_id: int
    label: int
    block_id: int = -1


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    torch.backends.cudnn.benchmark = True


def extract_frame_id(path: Path) -> int:
    """
    Expected filename:
      254p RGB Frame (1).jpg
      254p RGB Frame (13701).jpg
    """
    m = re.search(r"\((\d+)\)", path.name)

    if not m:
        raise ValueError(f"Could not extract frame id from filename: {path.name}")

    return int(m.group(1))


def load_label_intervals(labels_file: Path) -> List[Tuple[int, int, str]]:
    """
    Expected useful lines:
      1       13700   NN
      13701   14699   YY
      14700   15980   YN
      15981   19802   YY

    Code:
      first char  = Fire Y/N
      second char = Smoke Y/N
    """
    intervals: List[Tuple[int, int, str]] = []

    with labels_file.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()

            m = re.match(r"^(\d+)\s+(\d+)\s+([YN])([YN])$", line)

            if not m:
                continue

            start = int(m.group(1))
            end = int(m.group(2))
            code = m.group(3) + m.group(4)

            intervals.append((start, end, code))

    if not intervals:
        raise RuntimeError(f"No label intervals found in: {labels_file}")

    intervals.sort(key=lambda x: x[0])

    print(f"[labels] Loaded {len(intervals)} intervals")
    print("[labels] First intervals:")

    for row in intervals[:10]:
        print(" ", row)

    return intervals


def label_for_frame(
    frame_id: int,
    intervals: List[Tuple[int, int, str]],
    task: str,
) -> int:
    for start, end, code in intervals:
        if start <= frame_id <= end:
            if task == "fire":
                return 1 if code[0] == "Y" else 0

            if task == "smoke":
                return 1 if code[1] == "Y" else 0

            raise ValueError(f"Invalid task: {task}")

    raise ValueError(f"No label interval found for frame {frame_id}")


def build_samples(
    image_dir: Path,
    labels_file: Path,
    task: str,
) -> List[Sample]:
    intervals = load_label_intervals(labels_file)

    image_paths = sorted(
        p
        for p in image_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMG_EXTS
    )

    if not image_paths:
        raise RuntimeError(f"No images found in: {image_dir}")

    samples: List[Sample] = []
    skipped = 0

    for p in tqdm(image_paths, desc="[data] indexing images", ncols=120):
        try:
            frame_id = extract_frame_id(p)
            label = label_for_frame(frame_id, intervals, task)
            samples.append(Sample(path=p, frame_id=frame_id, label=label))
        except Exception:
            skipped += 1

    if not samples:
        raise RuntimeError("No valid samples were built.")

    labels = np.array([s.label for s in samples])
    unique, counts = np.unique(labels, return_counts=True)

    print(f"[data] Found images: {len(image_paths)}")
    print(f"[data] Valid samples: {len(samples)}")
    print(f"[data] Skipped: {skipped}")
    print("[data] Class counts:")

    for u, c in zip(unique, counts):
        name = "positive" if u == 1 else "negative"
        print(f"  {u} ({name}): {c}")

    return samples


def random_image_split(
    samples: List[Sample],
    seed: int,
    val_ratio: float,
    test_ratio: float,
) -> Tuple[List[Sample], List[Sample], List[Sample]]:
    """
    Baseline split. Not recommended for final video-frame evaluation because it
    can place adjacent frames into train/val/test.
    """
    y = np.array([s.label for s in samples])

    train_val, test = train_test_split(
        samples,
        test_size=test_ratio,
        random_state=seed,
        stratify=y,
    )

    y_train_val = np.array([s.label for s in train_val])
    adjusted_val_ratio = val_ratio / (1.0 - test_ratio)

    train, val = train_test_split(
        train_val,
        test_size=adjusted_val_ratio,
        random_state=seed,
        stratify=y_train_val,
    )

    return train, val, test


def temporal_block_split(
    samples: List[Sample],
    block_size: int,
    seed: int,
    val_ratio: float,
    test_ratio: float,
) -> Tuple[List[Sample], List[Sample], List[Sample]]:
    """
    Temporal block split.

    Frames are grouped by:
      block_id = (frame_id - 1) // block_size

    Then whole blocks go to train, val, or test.

    This avoids the most obvious temporal leakage:
      frame 13705 in train, frame 13706 in val, frame 13707 in test.
    """
    block_to_samples: Dict[int, List[Sample]] = {}

    for s in samples:
        block_id = (s.frame_id - 1) // block_size
        s.block_id = block_id
        block_to_samples.setdefault(block_id, []).append(s)

    block_ids = sorted(block_to_samples.keys())

    block_labels = []

    for b in block_ids:
        labels = [x.label for x in block_to_samples[b]]
        dominant_label = int(np.mean(labels) >= 0.5)
        block_labels.append(dominant_label)

    block_ids_np = np.array(block_ids)
    block_labels_np = np.array(block_labels)

    print(f"[block split] block_size={block_size}")
    print(f"[block split] total_blocks={len(block_ids)}")

    unique_blocks, counts_blocks = np.unique(block_labels_np, return_counts=True)

    print("[block split] block class counts by dominant label:")
    for u, c in zip(unique_blocks, counts_blocks):
        name = "positive" if u == 1 else "negative"
        print(f"  {u} ({name}): {c} blocks")

    can_stratify_test = len(np.unique(block_labels_np)) == 2 and np.min(counts_blocks) >= 2

    train_val_blocks, test_blocks = train_test_split(
        block_ids_np,
        test_size=test_ratio,
        random_state=seed,
        stratify=block_labels_np if can_stratify_test else None,
    )

    train_val_labels = np.array(
        [
            int(np.mean([x.label for x in block_to_samples[int(b)]]) >= 0.5)
            for b in train_val_blocks
        ]
    )

    unique_tv, counts_tv = np.unique(train_val_labels, return_counts=True)
    can_stratify_val = len(unique_tv) == 2 and np.min(counts_tv) >= 2

    adjusted_val_ratio = val_ratio / (1.0 - test_ratio)

    train_blocks, val_blocks = train_test_split(
        train_val_blocks,
        test_size=adjusted_val_ratio,
        random_state=seed,
        stratify=train_val_labels if can_stratify_val else None,
    )

    train_block_set = set(map(int, train_blocks))
    val_block_set = set(map(int, val_blocks))
    test_block_set = set(map(int, test_blocks))

    train: List[Sample] = []
    val: List[Sample] = []
    test: List[Sample] = []

    for b, items in block_to_samples.items():
        if b in train_block_set:
            train.extend(items)
        elif b in val_block_set:
            val.extend(items)
        elif b in test_block_set:
            test.extend(items)

    train = sorted(train, key=lambda x: x.frame_id)
    val = sorted(val, key=lambda x: x.frame_id)
    test = sorted(test, key=lambda x: x.frame_id)

    return train, val, test


def print_split_stats(name: str, samples: List[Sample]) -> None:
    labels = np.array([s.label for s in samples])
    unique, counts = np.unique(labels, return_counts=True)

    frame_min = min(s.frame_id for s in samples)
    frame_max = max(s.frame_id for s in samples)

    blocks = sorted(set(s.block_id for s in samples))

    print(f"[split:{name}] samples={len(samples)} frames={frame_min}-{frame_max} blocks={len(blocks)}")

    for u, c in zip(unique, counts):
        label_name = "positive" if u == 1 else "negative"
        print(f"  class {u} ({label_name}): {c}")


class FlameFrameDataset(Dataset):
    def __init__(self, samples: List[Sample], transform):
        self.samples = samples
        self.transform = transform

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        s = self.samples[idx]

        img = Image.open(s.path).convert("RGB")
        img = self.transform(img)

        y = torch.tensor(s.label, dtype=torch.long)

        return img, y


def make_transforms(img_size: int):
    train_tf = transforms.Compose(
        [
            transforms.Resize((img_size, img_size)),
            transforms.RandomHorizontalFlip(p=0.5),
            transforms.RandomRotation(degrees=8),
            transforms.ColorJitter(
                brightness=0.12,
                contrast=0.12,
                saturation=0.08,
            ),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            ),
        ]
    )

    eval_tf = transforms.Compose(
        [
            transforms.Resize((img_size, img_size)),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            ),
        ]
    )

    return train_tf, eval_tf


def make_model(num_classes: int = 2, pretrained: bool = True) -> nn.Module:
    weights = ResNet18_Weights.DEFAULT if pretrained else None

    model = resnet18(weights=weights)
    model.fc = nn.Linear(model.fc.in_features, num_classes)

    return model


def run_epoch(
    model,
    loader,
    criterion,
    optimizer,
    device,
    train: bool,
    scaler,
    epoch: int,
    total_epochs: int,
):
    model.train(train)

    total_loss = 0.0
    total_correct = 0
    total = 0

    mode = "train" if train else "val"

    pbar = tqdm(
        loader,
        desc=f"[{mode} {epoch:03d}/{total_epochs:03d}]",
        leave=False,
        ncols=120,
    )

    for x, y in pbar:
        x = x.to(device, non_blocking=True)
        y = y.to(device, non_blocking=True)

        with torch.set_grad_enabled(train):
            with torch.amp.autocast("cuda", enabled=(device.type == "cuda")):
                logits = model(x)
                loss = criterion(logits, y)

            if train:
                optimizer.zero_grad(set_to_none=True)

                if scaler is not None:
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    loss.backward()
                    optimizer.step()

        bs = x.size(0)

        total_loss += loss.item() * bs
        total_correct += (torch.argmax(logits, dim=1) == y).sum().item()
        total += bs

        running_loss = total_loss / total
        running_acc = total_correct / total

        pbar.set_postfix(
            {
                "loss": f"{running_loss:.4f}",
                "acc": f"{running_acc:.4f}",
            }
        )

    return total_loss / total, total_correct / total


@torch.no_grad()
def evaluate(
    model,
    loader,
    criterion,
    device,
):
    model.eval()

    total_loss = 0.0
    total = 0

    ys = []
    preds = []

    pbar = tqdm(
        loader,
        desc="[test]",
        leave=False,
        ncols=120,
    )

    for x, y in pbar:
        x = x.to(device, non_blocking=True)
        y = y.to(device, non_blocking=True)

        with torch.amp.autocast("cuda", enabled=(device.type == "cuda")):
            logits = model(x)
            loss = criterion(logits, y)

        pred = torch.argmax(logits, dim=1)

        bs = x.size(0)

        total_loss += loss.item() * bs
        total += bs

        ys.extend(y.cpu().numpy().tolist())
        preds.extend(pred.cpu().numpy().tolist())

        running_loss = total_loss / total
        running_acc = accuracy_score(ys, preds)

        pbar.set_postfix(
            {
                "loss": f"{running_loss:.4f}",
                "acc": f"{running_acc:.4f}",
            }
        )

    metrics = {
        "loss": float(total_loss / total),
        "acc": float(accuracy_score(ys, preds)),
        "balanced_acc": float(balanced_accuracy_score(ys, preds)),
        "precision": float(precision_score(ys, preds, zero_division=0)),
        "recall": float(recall_score(ys, preds, zero_division=0)),
        "f1": float(f1_score(ys, preds, zero_division=0)),
        "confusion_matrix": confusion_matrix(ys, preds).tolist(),
    }

    return metrics


def save_history(history, path: Path):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "epoch",
                "train_loss",
                "train_acc",
                "val_loss",
                "val_acc",
                "lr",
                "seconds",
            ],
        )
        writer.writeheader()
        writer.writerows(history)


def save_split_csv(split_name: str, samples: List[Sample], writer):
    for s in samples:
        writer.writerow(
            {
                "split": split_name,
                "frame_id": s.frame_id,
                "block_id": s.block_id,
                "path": str(s.path),
                "label": s.label,
            }
        )


def save_confusion_matrix_csv(cm, path: Path):
    arr = np.array(cm)

    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["", "pred_0", "pred_1"])
        writer.writerow(["true_0", int(arr[0, 0]), int(arr[0, 1])])
        writer.writerow(["true_1", int(arr[1, 0]), int(arr[1, 1])])


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--image-dir",
        type=str,
        default="/mnt/d/#9) 254p Frame Pairs/254p RGB Images",
    )

    parser.add_argument(
        "--labels-file",
        type=str,
        default="/mnt/d/#10) Frame Pair Labels.txt",
    )

    parser.add_argument(
        "--outdir",
        type=str,
        default="./runs_flame2_resnet18",
    )

    parser.add_argument(
        "--task",
        choices=["fire", "smoke"],
        default="fire",
    )

    parser.add_argument(
        "--split-mode",
        choices=["block", "random"],
        default="block",
        help="Use 'block' for temporal block split. Use 'random' only for sanity checks.",
    )

    parser.add_argument(
        "--block-size",
        type=int,
        default=1000,
        help="Number of consecutive frames per temporal block.",
    )

    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch", type=int, default=128)
    parser.add_argument("--img-size", type=int, default=224)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--no-pretrained", action="store_true")

    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--test-ratio", type=float, default=0.15)

    args = parser.parse_args()

    seed_everything(args.seed)

    image_dir = Path(args.image_dir)
    labels_file = Path(args.labels_file)

    if not image_dir.exists():
        raise FileNotFoundError(f"Image directory not found: {image_dir}")

    if not labels_file.exists():
        raise FileNotFoundError(f"Labels file not found: {labels_file}")

    samples = build_samples(
        image_dir=image_dir,
        labels_file=labels_file,
        task=args.task,
    )

    if args.split_mode == "block":
        train, val, test = temporal_block_split(
            samples=samples,
            block_size=args.block_size,
            seed=args.seed,
            val_ratio=args.val_ratio,
            test_ratio=args.test_ratio,
        )
    else:
        train, val, test = random_image_split(
            samples=samples,
            seed=args.seed,
            val_ratio=args.val_ratio,
            test_ratio=args.test_ratio,
        )

        for s in train + val + test:
            s.block_id = (s.frame_id - 1) // args.block_size

    print(f"[split] mode={args.split_mode}")
    print(f"[split] train={len(train)} val={len(val)} test={len(test)}")

    print_split_stats("train", train)
    print_split_stats("val", val)
    print_split_stats("test", test)

    run_name = (
        f"resnet18_{args.task}_rgb_"
        f"{args.split_mode}_bs{args.block_size}_"
        f"img{args.img_size}_seed{args.seed}_"
        f"{time.strftime('%Y%m%d-%H%M%S')}"
    )

    run_dir = Path(args.outdir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    with (run_dir / "args.json").open("w") as f:
        json.dump(vars(args), f, indent=2)

    with (run_dir / "splits.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["split", "frame_id", "block_id", "path", "label"],
        )
        writer.writeheader()

        save_split_csv("train", train, writer)
        save_split_csv("val", val, writer)
        save_split_csv("test", test, writer)

    train_tf, eval_tf = make_transforms(args.img_size)

    train_ds = FlameFrameDataset(train, train_tf)
    val_ds = FlameFrameDataset(val, eval_tf)
    test_ds = FlameFrameDataset(test, eval_tf)

    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch,
        shuffle=True,
        num_workers=args.workers,
        pin_memory=True,
        persistent_workers=args.workers > 0,
    )

    val_loader = DataLoader(
        val_ds,
        batch_size=args.batch,
        shuffle=False,
        num_workers=args.workers,
        pin_memory=True,
        persistent_workers=args.workers > 0,
    )

    test_loader = DataLoader(
        test_ds,
        batch_size=args.batch,
        shuffle=False,
        num_workers=args.workers,
        pin_memory=True,
        persistent_workers=args.workers > 0,
    )

    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    print(f"[device] {device}")

    model = make_model(
        num_classes=2,
        pretrained=not args.no_pretrained,
    ).to(device)

    train_labels = np.array([s.label for s in train])
    class_counts = np.bincount(train_labels, minlength=2)

    class_weights = class_counts.sum() / np.maximum(class_counts, 1)
    class_weights = class_weights / class_weights.mean()

    print(f"[loss] class_counts={class_counts.tolist()}")
    print(f"[loss] class_weights={class_weights.tolist()}")

    criterion = nn.CrossEntropyLoss(
        weight=torch.tensor(
            class_weights,
            dtype=torch.float32,
        ).to(device)
    )

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=args.lr,
        weight_decay=args.weight_decay,
    )

    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer,
        T_max=args.epochs,
    )

    scaler = torch.amp.GradScaler("cuda") if device.type == "cuda" else None

    best_val_acc = -1.0
    history = []

    print(f"[output] run_dir={run_dir}")

    for epoch in range(1, args.epochs + 1):
        t0 = time.time()

        train_loss, train_acc = run_epoch(
            model=model,
            loader=train_loader,
            criterion=criterion,
            optimizer=optimizer,
            device=device,
            train=True,
            scaler=scaler,
            epoch=epoch,
            total_epochs=args.epochs,
        )

        val_loss, val_acc = run_epoch(
            model=model,
            loader=val_loader,
            criterion=criterion,
            optimizer=None,
            device=device,
            train=False,
            scaler=None,
            epoch=epoch,
            total_epochs=args.epochs,
        )

        scheduler.step()

        row = {
            "epoch": epoch,
            "train_loss": float(train_loss),
            "train_acc": float(train_acc),
            "val_loss": float(val_loss),
            "val_acc": float(val_acc),
            "lr": float(optimizer.param_groups[0]["lr"]),
            "seconds": float(time.time() - t0),
        }

        history.append(row)
        save_history(history, run_dir / "history.csv")

        print(
            f"[epoch {epoch:03d}/{args.epochs:03d}] "
            f"train_loss={train_loss:.4f} "
            f"train_acc={train_acc:.4f} "
            f"val_loss={val_loss:.4f} "
            f"val_acc={val_acc:.4f} "
            f"lr={row['lr']:.2e} "
            f"time={row['seconds']:.1f}s"
        )

        checkpoint = {
            "epoch": epoch,
            "task": args.task,
            "arch": "resnet18",
            "split_mode": args.split_mode,
            "block_size": args.block_size,
            "img_size": args.img_size,
            "num_classes": 2,
            "class_names": ["negative", "positive"],
            "state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "val_acc": float(val_acc),
            "val_loss": float(val_loss),
            "args": vars(args),
        }

        torch.save(checkpoint, run_dir / "last.pth")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(checkpoint, run_dir / "best.pth")
            print(f"[save] best.pth updated: val_acc={best_val_acc:.4f}")

    print("[test] Evaluating best.pth")

    best = torch.load(run_dir / "best.pth", map_location=device)
    model.load_state_dict(best["state_dict"])

    test_metrics = evaluate(
        model=model,
        loader=test_loader,
        criterion=criterion,
        device=device,
    )

    with (run_dir / "test_metrics.json").open("w") as f:
        json.dump(test_metrics, f, indent=2)

    save_confusion_matrix_csv(
        test_metrics["confusion_matrix"],
        run_dir / "confusion_matrix.csv",
    )

    print("\n========== TEST METRICS ==========")
    print(json.dumps(test_metrics, indent=2))

    print("\n========== SAVED ==========")
    print(f"run_dir:          {run_dir}")
    print(f"best model:       {run_dir / 'best.pth'}")
    print(f"last model:       {run_dir / 'last.pth'}")
    print(f"history:          {run_dir / 'history.csv'}")
    print(f"test metrics:     {run_dir / 'test_metrics.json'}")
    print(f"confusion matrix: {run_dir / 'confusion_matrix.csv'}")
    print(f"splits:           {run_dir / 'splits.csv'}")


if __name__ == "__main__":
    main()