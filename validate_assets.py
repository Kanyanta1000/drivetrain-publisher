from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

QUESTION_DIR_RE = re.compile(r"^question_(\d+)$")
SLIDE_FILE_RE = re.compile(r"^slide_(\d+)\.(png|jpg|jpeg)$", re.IGNORECASE)
ALLOWED_EXTRA_FILES = {"caption.txt"}
REPO_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class SlideAsset:
    number: int
    path: Path


@dataclass(frozen=True)
class QuestionFolder:
    number: int
    name: str
    path: Path
    slides: tuple[SlideAsset, ...]


@dataclass(frozen=True)
class Inventory:
    root: Path
    target_subdir: str
    questions: tuple[QuestionFolder, ...]
    total_question_folders: int
    total_slide_files: int
    first_question_number: int | None
    last_question_number: int | None
    invalid_folders: tuple[str, ...]


def resolve_asset_root(target_subdir: str) -> Path:
    asset_root = (REPO_ROOT / target_subdir).resolve()
    if not asset_root.exists():
        raise FileNotFoundError(f"Asset directory does not exist: {asset_root}")
    if not asset_root.is_dir():
        raise NotADirectoryError(f"Asset path is not a directory: {asset_root}")
    return asset_root


def collect_inventory(target_subdir: str = "en") -> Inventory:
    asset_root = resolve_asset_root(target_subdir)
    valid_questions: list[QuestionFolder] = []
    invalid_folders: list[str] = []
    all_question_numbers: list[int] = []
    total_slide_files = 0

    question_dirs: list[tuple[int, Path]] = []
    for child in asset_root.iterdir():
        if not child.is_dir():
            continue
        match = QUESTION_DIR_RE.fullmatch(child.name)
        if match:
            question_dirs.append((int(match.group(1)), child))

    question_dirs.sort(key=lambda item: item[0])

    for question_number, question_path in question_dirs:
        all_question_numbers.append(question_number)
        slide_assets: list[SlideAsset] = []
        folder_errors: list[str] = []

        for child in question_path.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_file() and child.name in ALLOWED_EXTRA_FILES:
                continue
            if child.is_file():
                slide_match = SLIDE_FILE_RE.fullmatch(child.name)
                if slide_match:
                    slide_assets.append(SlideAsset(number=int(slide_match.group(1)), path=child))
                    continue
                folder_errors.append(f"unexpected file {child.name}")
                continue
            folder_errors.append(f"unexpected directory {child.name}")

        slide_assets.sort(key=lambda slide: slide.number)
        total_slide_files += len(slide_assets)

        slide_numbers = [slide.number for slide in slide_assets]
        expected_numbers = list(range(1, len(slide_assets) + 1))

        if not 2 <= len(slide_assets) <= 10:
            folder_errors.append(
                f"expected 2 to 10 slides, found {len(slide_assets)}"
            )
        if slide_numbers != expected_numbers:
            folder_errors.append(
                f"expected contiguous slide numbering starting at 1, found {slide_numbers}"
            )

        if folder_errors:
            invalid_folders.append(f"{question_path.name}: " + "; ".join(folder_errors))
            continue

        valid_questions.append(
            QuestionFolder(
                number=question_number,
                name=question_path.name,
                path=question_path,
                slides=tuple(slide_assets),
            )
        )

    return Inventory(
        root=asset_root,
        target_subdir=target_subdir,
        questions=tuple(valid_questions),
        total_question_folders=len(question_dirs),
        total_slide_files=total_slide_files,
        first_question_number=all_question_numbers[0] if all_question_numbers else None,
        last_question_number=all_question_numbers[-1] if all_question_numbers else None,
        invalid_folders=tuple(invalid_folders),
    )


def format_question_label(number: int | None) -> str:
    if number is None:
        return "n/a"
    return f"question_{number:03d}"


def format_summary(inventory: Inventory) -> str:
    lines = [
        f"Validation summary for {inventory.target_subdir}/",
        f"Total question folders: {inventory.total_question_folders}",
        f"Total slide files: {inventory.total_slide_files}",
        f"First question number found: {format_question_label(inventory.first_question_number)}",
        f"Last question number found: {format_question_label(inventory.last_question_number)}",
    ]

    if inventory.invalid_folders:
        lines.append("Invalid folders:")
        lines.extend(f"- {message}" for message in inventory.invalid_folders)
    else:
        lines.append("Invalid folders: none")

    return "\n".join(lines)


def print_summary(inventory: Inventory) -> None:
    print(format_summary(inventory))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Instagram carousel assets by folder and slide numbering."
    )
    parser.add_argument(
        "--target-subdir",
        default=os.getenv("TARGET_SUBDIR", "en"),
        help="Repository subdirectory that contains question folders.",
    )
    args = parser.parse_args()

    try:
        inventory = collect_inventory(args.target_subdir)
    except (FileNotFoundError, NotADirectoryError) as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        return 1

    print_summary(inventory)
    return 1 if inventory.invalid_folders else 0


if __name__ == "__main__":
    raise SystemExit(main())
