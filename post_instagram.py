from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree
from zipfile import ZipFile

import requests

from validate_assets import Inventory, QuestionFolder, collect_inventory, format_summary

DEFAULT_PUBLIC_BASE_URL = "https://kanyanta1000.github.io/drivetrain-publisher"
DEFAULT_TARGET_SUBDIR = "en"
DEFAULT_GRAPH_API_VERSION = "v25.0"
DEFAULT_POSTING_SCHEDULE_NAME = "posting_schedule.xlsx"
DEFAULT_POSTING_SCHEDULE_SHEET = "Posting Schedule"
REQUEST_TIMEOUT_SECONDS = 30
HTTP_RETRY_LIMIT = 3
HTTP_RETRY_BACKOFF_SECONDS = 2
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 600
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
READY_STATUS_CODES = {"FINISHED", "PUBLISHED"}
FAILED_STATUS_CODES = {"ERROR", "EXPIRED"}
REPO_ROOT = Path(__file__).resolve().parent
STATE_PATH = REPO_ROOT / "posted.json"
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class PublisherError(RuntimeError):
    pass


class GraphAPIError(PublisherError):
    pass


@dataclass
class PublisherConfig:
    ig_user_id: str
    ig_access_token: str
    public_base_url: str
    target_subdir: str
    graph_api_version: str
    posting_schedule_path: Path
    posting_schedule_sheet: str


@dataclass(frozen=True)
class ScheduleEntry:
    order: int
    folder_name: str
    status: str | None
    date_posted: str | None
    sheet_row: int


@dataclass
class PublishResult:
    folder: QuestionFolder
    slide_urls: list[str]
    child_container_ids: list[str]
    carousel_container_id: str
    media_publish_id: str
    caption_path: str | None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"posted_folders": {}}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PublisherError(f"State file is not valid JSON: {path}") from error

    if not isinstance(data, dict):
        raise PublisherError(f"State file must contain a JSON object: {path}")

    posted_folders = data.get("posted_folders")
    if posted_folders is None:
        data["posted_folders"] = {}
        return data
    if not isinstance(posted_folders, dict):
        raise PublisherError("posted.json must contain an object at posted_folders")

    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def build_public_url(path: Path, public_base_url: str) -> str:
    relative_path = path.relative_to(REPO_ROOT).as_posix()
    return f"{public_base_url.rstrip('/')}/{quote(relative_path, safe='/')}"


def format_repo_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def read_caption(question: QuestionFolder) -> tuple[str | None, str | None]:
    caption_path = question.path / "caption.txt"
    if not caption_path.exists():
        return None, None
    caption = caption_path.read_text(encoding="utf-8").rstrip("\n")
    return caption, str(caption_path.relative_to(REPO_ROOT))


def find_question(inventory: Inventory, folder_name: str) -> QuestionFolder | None:
    for question in inventory.questions:
        if question.name == folder_name:
            return question
    return None


def find_schedule_entry(schedule_entries: tuple[ScheduleEntry, ...], folder_name: str) -> ScheduleEntry | None:
    for entry in schedule_entries:
        if entry.folder_name == folder_name:
            return entry
    return None


def resolve_repo_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def ns_tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def column_name_from_reference(cell_reference: str) -> str:
    letters: list[str] = []
    for character in cell_reference:
        if character.isalpha():
            letters.append(character)
            continue
        break
    return "".join(letters)


def normalize_header(value: str) -> str:
    return " ".join(value.split()).strip().lower()


def read_inline_text(element: ElementTree.Element) -> str:
    return "".join(text for text in element.itertext() if text)


def load_shared_strings(workbook: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []

    root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    shared_strings: list[str] = []
    for item in root.findall(f".//{ns_tag(SPREADSHEET_NS, 'si')}"):
        shared_strings.append(read_inline_text(item))
    return shared_strings


def read_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline_string = cell.find(ns_tag(SPREADSHEET_NS, "is"))
        return read_inline_text(inline_string) if inline_string is not None else ""

    value = cell.findtext(ns_tag(SPREADSHEET_NS, "v"), default="")
    value = value.strip()
    if not value:
        return ""
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError) as error:
            raise PublisherError("Posting schedule shared string index is invalid.") from error
    return value


def load_worksheet_path(workbook: ZipFile, sheet_name: str) -> str:
    workbook_root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
    rels_root = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    relationships = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_root.findall(f".//{ns_tag(PACKAGE_REL_NS, 'Relationship')}")
    }

    available_sheets: list[str] = []
    for sheet in workbook_root.findall(f".//{ns_tag(SPREADSHEET_NS, 'sheet')}"):
        available_sheets.append(sheet.attrib.get("name", "<unnamed>"))
        if sheet.attrib.get("name") != sheet_name:
            continue

        rel_id = sheet.attrib.get(ns_tag(REL_NS, "id"))
        if not rel_id or rel_id not in relationships:
            raise PublisherError(f"Posting schedule sheet {sheet_name!r} is missing its worksheet relationship.")

        target = relationships[rel_id]
        if target.startswith("/"):
            return target.lstrip("/")
        return str(PurePosixPath("xl") / target)

    available = ", ".join(available_sheets) if available_sheets else "none"
    raise PublisherError(
        f"Posting schedule sheet {sheet_name!r} was not found. Available sheets: {available}."
    )


def load_posting_schedule(path: Path, sheet_name: str) -> tuple[ScheduleEntry, ...]:
    if not path.exists():
        raise PublisherError(f"Posting schedule file does not exist: {path}")
    if not path.is_file():
        raise PublisherError(f"Posting schedule path is not a file: {path}")

    try:
        with ZipFile(path) as workbook:
            worksheet_path = load_worksheet_path(workbook, sheet_name)
            shared_strings = load_shared_strings(workbook)
            worksheet_root = ElementTree.fromstring(workbook.read(worksheet_path))
    except (OSError, KeyError, ElementTree.ParseError) as error:
        raise PublisherError(f"Posting schedule workbook could not be read: {path}") from error

    sheet_data = worksheet_root.find(ns_tag(SPREADSHEET_NS, "sheetData"))
    if sheet_data is None:
        raise PublisherError(f"Posting schedule sheet {sheet_name!r} does not contain sheet data.")

    rows = sheet_data.findall(ns_tag(SPREADSHEET_NS, "row"))
    if not rows:
        raise PublisherError(f"Posting schedule sheet {sheet_name!r} is empty.")

    header_cells = {
        column_name_from_reference(cell.attrib.get("r", "")): read_cell_value(cell, shared_strings)
        for cell in rows[0].findall(ns_tag(SPREADSHEET_NS, "c"))
    }
    header_map = {normalize_header(value): column for column, value in header_cells.items() if value}

    required_headers = {"posting order": "Posting Order", "folder": "Folder"}
    missing_headers = [
        display_name
        for normalized_name, display_name in required_headers.items()
        if normalized_name not in header_map
    ]
    if missing_headers:
        raise PublisherError(
            "Posting schedule is missing required columns: " + ", ".join(missing_headers)
        )

    order_column = header_map["posting order"]
    folder_column = header_map["folder"]
    status_column = header_map.get("status")
    date_column = header_map.get("date posted")

    schedule_entries: list[ScheduleEntry] = []
    for row in rows[1:]:
        row_number = int(row.attrib.get("r", "0") or 0)
        row_cells = {
            column_name_from_reference(cell.attrib.get("r", "")): read_cell_value(cell, shared_strings).strip()
            for cell in row.findall(ns_tag(SPREADSHEET_NS, "c"))
        }

        if not any(row_cells.values()):
            continue

        order_value = row_cells.get(order_column, "")
        folder_name = row_cells.get(folder_column, "")
        if not order_value or not folder_name:
            raise PublisherError(
                f"Posting schedule row {row_number} must include both Posting Order and Folder."
            )

        try:
            order = int(order_value)
        except ValueError as error:
            raise PublisherError(
                f"Posting schedule row {row_number} has a non-integer Posting Order: {order_value!r}."
            ) from error

        status_value = row_cells.get(status_column) if status_column else None
        date_posted_value = row_cells.get(date_column) if date_column else None

        schedule_entries.append(
            ScheduleEntry(
                order=order,
                folder_name=folder_name,
                status=status_value or None,
                date_posted=date_posted_value or None,
                sheet_row=row_number,
            )
        )

    if not schedule_entries:
        raise PublisherError(f"Posting schedule sheet {sheet_name!r} does not contain any schedule rows.")

    entries_by_order: dict[int, int] = {}
    entries_by_folder: dict[str, int] = {}
    duplicate_orders: set[int] = set()
    duplicate_folders: set[str] = set()

    for entry in schedule_entries:
        if entry.order in entries_by_order:
            duplicate_orders.add(entry.order)
        entries_by_order[entry.order] = entries_by_order.get(entry.order, 0) + 1

        if entry.folder_name in entries_by_folder:
            duplicate_folders.add(entry.folder_name)
        entries_by_folder[entry.folder_name] = entries_by_folder.get(entry.folder_name, 0) + 1

    if duplicate_orders:
        raise PublisherError(
            "Posting schedule contains duplicate Posting Order values: "
            + ", ".join(str(order) for order in sorted(duplicate_orders))
        )
    if duplicate_folders:
        raise PublisherError(
            "Posting schedule contains duplicate Folder values: "
            + ", ".join(sorted(duplicate_folders))
        )

    schedule_entries.sort(key=lambda entry: entry.order)
    expected_orders = list(range(1, len(schedule_entries) + 1))
    actual_orders = [entry.order for entry in schedule_entries]
    if actual_orders != expected_orders:
        raise PublisherError(
            "Posting schedule must use contiguous Posting Order values starting at 1. "
            f"Found first values: {actual_orders[:10]}"
        )

    return tuple(schedule_entries)


def validate_posting_schedule(
    inventory: Inventory,
    schedule_entries: tuple[ScheduleEntry, ...],
    schedule_path: Path,
    sheet_name: str,
) -> None:
    inventory_names = {question.name for question in inventory.questions}
    schedule_names = {entry.folder_name for entry in schedule_entries}

    missing_folders = sorted(inventory_names - schedule_names)
    extra_folders = sorted(schedule_names - inventory_names)
    if missing_folders or extra_folders:
        messages: list[str] = []
        if missing_folders:
            messages.append(
                "missing folders: " + ", ".join(missing_folders[:10]) + ("..." if len(missing_folders) > 10 else "")
            )
        if extra_folders:
            messages.append(
                "unknown folders: " + ", ".join(extra_folders[:10]) + ("..." if len(extra_folders) > 10 else "")
            )
        raise PublisherError(
            f"Posting schedule {format_repo_path(schedule_path)} ({sheet_name}) does not match the "
            f"validated asset folders: {'; '.join(messages)}"
        )


def select_next_question(
    inventory: Inventory,
    state: dict[str, Any],
    requested_folder: str | None,
    schedule_entries: tuple[ScheduleEntry, ...],
) -> tuple[QuestionFolder, ScheduleEntry | None] | None:
    posted_folders = state.get("posted_folders", {})

    if requested_folder:
        question = find_question(inventory, requested_folder)
        if question is None:
            raise PublisherError(
                f"Requested folder {requested_folder} was not found among valid folders in "
                f"{inventory.target_subdir}/."
            )
        if requested_folder in posted_folders:
            raise PublisherError(
                f"{requested_folder} is already recorded in posted.json. Refusing to post it again."
            )
        return question, find_schedule_entry(schedule_entries, requested_folder)

    inventory_by_name = {question.name: question for question in inventory.questions}
    for schedule_entry in schedule_entries:
        if schedule_entry.folder_name in posted_folders:
            continue
        question = inventory_by_name.get(schedule_entry.folder_name)
        if question is None:
            raise PublisherError(
                f"Posting schedule references {schedule_entry.folder_name}, but it is not a valid folder in "
                f"{inventory.target_subdir}/."
            )
        return question, schedule_entry
    return None


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise PublisherError(f"Required environment variable is missing: {name}")
    return value


def load_config(require_instagram_auth: bool) -> PublisherConfig:
    ig_user_id = require_env("IG_USER_ID") if require_instagram_auth else os.getenv("IG_USER_ID", "").strip()
    ig_access_token = (
        require_env("IG_ACCESS_TOKEN") if require_instagram_auth else os.getenv("IG_ACCESS_TOKEN", "").strip()
    )
    public_base_url = os.getenv("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip()
    target_subdir = os.getenv("TARGET_SUBDIR", DEFAULT_TARGET_SUBDIR).strip() or DEFAULT_TARGET_SUBDIR
    graph_api_version = os.getenv("GRAPH_API_VERSION", DEFAULT_GRAPH_API_VERSION).strip() or DEFAULT_GRAPH_API_VERSION
    default_schedule_path = str(Path(target_subdir) / DEFAULT_POSTING_SCHEDULE_NAME)
    posting_schedule_path_value = (
        os.getenv("POSTING_SCHEDULE_PATH", default_schedule_path).strip() or default_schedule_path
    )
    posting_schedule_sheet = (
        os.getenv("POSTING_SCHEDULE_SHEET", DEFAULT_POSTING_SCHEDULE_SHEET).strip()
        or DEFAULT_POSTING_SCHEDULE_SHEET
    )

    if not public_base_url:
        raise PublisherError("PUBLIC_BASE_URL cannot be empty")

    return PublisherConfig(
        ig_user_id=ig_user_id,
        ig_access_token=ig_access_token,
        public_base_url=public_base_url,
        target_subdir=target_subdir,
        graph_api_version=graph_api_version,
        posting_schedule_path=resolve_repo_path(posting_schedule_path_value),
        posting_schedule_sheet=posting_schedule_sheet,
    )


class GraphAPIClient:
    def __init__(self, config: PublisherConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"https://graph.facebook.com/{self.config.graph_api_version}/{path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = dict(params or {})
        payload = dict(data or {})

        if method.upper() == "GET":
            query["access_token"] = self.config.ig_access_token
        else:
            payload["access_token"] = self.config.ig_access_token

        for attempt in range(1, HTTP_RETRY_LIMIT + 1):
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=self._url(path),
                    params=query or None,
                    data=payload or None,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                if attempt == HTTP_RETRY_LIMIT:
                    raise GraphAPIError(f"Network error calling {path}: {error}") from error
                wait_seconds = HTTP_RETRY_BACKOFF_SECONDS * attempt
                print(
                    f"Network error calling {path} (attempt {attempt}/{HTTP_RETRY_LIMIT}). "
                    f"Retrying in {wait_seconds} seconds..."
                )
                time.sleep(wait_seconds)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < HTTP_RETRY_LIMIT:
                wait_seconds = HTTP_RETRY_BACKOFF_SECONDS * attempt
                print(
                    f"Received HTTP {response.status_code} from {path} "
                    f"(attempt {attempt}/{HTTP_RETRY_LIMIT}). Retrying in {wait_seconds} seconds..."
                )
                time.sleep(wait_seconds)
                continue

            try:
                body = response.json()
            except ValueError as error:
                raise GraphAPIError(
                    f"Graph API returned a non-JSON response for {path}: HTTP {response.status_code}"
                ) from error

            if response.ok and "error" not in body:
                return body

            error_info = body.get("error", {}) if isinstance(body, dict) else {}
            message = error_info.get("message") or response.text
            error_type = error_info.get("type")
            error_code = error_info.get("code")
            error_subcode = error_info.get("error_subcode")
            raise GraphAPIError(
                f"Graph API error for {path}: HTTP {response.status_code}; "
                f"type={error_type}; code={error_code}; subcode={error_subcode}; message={message}"
            )

        raise GraphAPIError(f"Graph API request failed for {path}")

    def create_child_container(self, image_url: str) -> str:
        response = self.request(
            "POST",
            f"{self.config.ig_user_id}/media",
            data={"image_url": image_url, "is_carousel_item": "true"},
        )
        container_id = response.get("id")
        if not container_id:
            raise GraphAPIError("Graph API did not return a child media container id")
        return str(container_id)

    def create_carousel_container(self, child_container_ids: list[str], caption: str | None) -> str:
        payload: dict[str, Any] = {
            "media_type": "CAROUSEL",
            "children": ",".join(child_container_ids),
        }
        if caption is not None:
            payload["caption"] = caption

        response = self.request("POST", f"{self.config.ig_user_id}/media", data=payload)
        container_id = response.get("id")
        if not container_id:
            raise GraphAPIError("Graph API did not return a carousel container id")
        return str(container_id)

    def get_container_status(self, container_id: str) -> str:
        response = self.request(
            "GET",
            container_id,
            params={"fields": "status_code,status"},
        )
        status = response.get("status_code") or response.get("status")
        if not status:
            raise GraphAPIError(f"Graph API did not return a status for container {container_id}")
        return str(status).upper()

    def wait_until_ready(self, container_id: str, label: str) -> None:
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        last_status = "UNKNOWN"

        while time.monotonic() < deadline:
            last_status = self.get_container_status(container_id)
            print(f"{label} {container_id} status: {last_status}")
            if last_status in READY_STATUS_CODES:
                return
            if last_status in FAILED_STATUS_CODES:
                raise GraphAPIError(f"{label} {container_id} failed with status {last_status}")
            time.sleep(POLL_INTERVAL_SECONDS)

        raise GraphAPIError(
            f"Timed out waiting for {label} {container_id}. Last status was {last_status}."
        )

    def publish_media(self, carousel_container_id: str) -> str:
        response = self.request(
            "POST",
            f"{self.config.ig_user_id}/media_publish",
            data={"creation_id": carousel_container_id},
        )
        publish_id = response.get("id")
        if not publish_id:
            raise GraphAPIError("Graph API did not return a media publish id")
        return str(publish_id)


def print_dry_run(
    question: QuestionFolder,
    slide_urls: list[str],
    already_posted: bool,
    schedule_entry: ScheduleEntry | None,
    schedule_path: Path,
) -> None:
    print(f"Dry run target folder: {question.name}")
    if already_posted:
        print(f"Note: {question.name} is already recorded in posted.json.")
    if schedule_entry is not None:
        print(
            f"Schedule order: {schedule_entry.order} "
            f"(sheet row {schedule_entry.sheet_row} in {format_repo_path(schedule_path)})"
        )
    print("Selected slide files:")
    for slide in question.slides:
        print(f"- {slide.path.relative_to(REPO_ROOT).as_posix()}")
    print("Public URLs:")
    for slide_url in slide_urls:
        print(f"- {slide_url}")


def publish_question(
    question: QuestionFolder,
    config: PublisherConfig,
    schedule_entry: ScheduleEntry | None,
) -> PublishResult:
    caption, caption_path = read_caption(question)
    slide_urls = [build_public_url(slide.path, config.public_base_url) for slide in question.slides]

    print(f"Posting folder: {question.name}")
    if schedule_entry is not None:
        print(
            f"Schedule order: {schedule_entry.order} "
            f"(sheet row {schedule_entry.sheet_row} in {format_repo_path(config.posting_schedule_path)})"
        )
    for slide, slide_url in zip(question.slides, slide_urls):
        print(f"- {slide.path.relative_to(REPO_ROOT).as_posix()} -> {slide_url}")

    client = GraphAPIClient(config)
    child_container_ids: list[str] = []

    for index, slide_url in enumerate(slide_urls, start=1):
        print(f"Creating child container for slide {index}...")
        child_container_id = client.create_child_container(slide_url)
        child_container_ids.append(child_container_id)
        client.wait_until_ready(child_container_id, f"Child container {index}")

    print("Creating carousel container...")
    carousel_container_id = client.create_carousel_container(child_container_ids, caption)
    client.wait_until_ready(carousel_container_id, "Carousel container")

    print("Publishing carousel...")
    media_publish_id = client.publish_media(carousel_container_id)
    print(f"Publish completed. Media id: {media_publish_id}")

    return PublishResult(
        folder=question,
        slide_urls=slide_urls,
        child_container_ids=child_container_ids,
        carousel_container_id=carousel_container_id,
        media_publish_id=media_publish_id,
        caption_path=caption_path,
    )


def update_posted_state(
    state: dict[str, Any],
    result: PublishResult,
    schedule_entry: ScheduleEntry | None,
    schedule_path: Path,
) -> None:
    posted_folders = state.setdefault("posted_folders", {})
    posted_folders[result.folder.name] = {
        "caption_path": result.caption_path,
        "carousel_container_id": result.carousel_container_id,
        "child_container_ids": result.child_container_ids,
        "media_publish_id": result.media_publish_id,
        "posted_at_utc": utc_now_iso(),
        "question_number": result.folder.number,
        "schedule_order": schedule_entry.order if schedule_entry is not None else None,
        "schedule_row": schedule_entry.sheet_row if schedule_entry is not None else None,
        "schedule_source": format_repo_path(schedule_path),
        "slide_paths": [slide.path.relative_to(REPO_ROOT).as_posix() for slide in result.folder.slides],
        "slide_urls": result.slide_urls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish the next Instagram carousel folder using the official Meta publishing flow."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview the next post without calling Instagram.")
    parser.add_argument("--folder", help="Specific question folder to publish, for example question_001.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the asset tree and exit without selecting or publishing a folder.",
    )
    args = parser.parse_args()

    try:
        config = load_config(require_instagram_auth=not (args.dry_run or args.validate_only))
        inventory = collect_inventory(config.target_subdir)
        schedule_entries = load_posting_schedule(
            config.posting_schedule_path,
            config.posting_schedule_sheet,
        )
        validate_posting_schedule(
            inventory,
            schedule_entries,
            config.posting_schedule_path,
            config.posting_schedule_sheet,
        )
    except (PublisherError, FileNotFoundError, NotADirectoryError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if inventory.invalid_folders:
        print(format_summary(inventory), file=sys.stderr)
        return 1

    print(
        f"Validated {inventory.total_question_folders} question folders and "
        f"{inventory.total_slide_files} slide files under {inventory.target_subdir}/."
    )
    print(
        f"Validated posting schedule with {len(schedule_entries)} rows from "
        f"{format_repo_path(config.posting_schedule_path)} "
        f"(sheet: {config.posting_schedule_sheet})."
    )

    if args.validate_only:
        print("Validation only mode completed successfully.")
        return 0

    try:
        state = load_state(STATE_PATH)
        requested_folder = args.folder.strip() if args.folder else None

        if requested_folder and args.dry_run:
            question = find_question(inventory, requested_folder)
            if question is None:
                raise PublisherError(
                    f"Requested folder {requested_folder} was not found among valid folders in "
                    f"{inventory.target_subdir}/."
                )
            slide_urls = [build_public_url(slide.path, config.public_base_url) for slide in question.slides]
            schedule_entry = find_schedule_entry(schedule_entries, requested_folder)
            print_dry_run(
                question,
                slide_urls,
                already_posted=requested_folder in state.get("posted_folders", {}),
                schedule_entry=schedule_entry,
                schedule_path=config.posting_schedule_path,
            )
            return 0

        selection = select_next_question(inventory, state, requested_folder, schedule_entries)
        if selection is None:
            print("No unposted question folders remain.")
            return 0
        question, schedule_entry = selection

        slide_urls = [build_public_url(slide.path, config.public_base_url) for slide in question.slides]

        if args.dry_run:
            print_dry_run(
                question,
                slide_urls,
                already_posted=False,
                schedule_entry=schedule_entry,
                schedule_path=config.posting_schedule_path,
            )
            return 0

        result = publish_question(question, config, schedule_entry)
        update_posted_state(state, result, schedule_entry, config.posting_schedule_path)
        save_state(STATE_PATH, state)
        print(f"Updated state file: {STATE_PATH.name}")
        return 0
    except PublisherError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
