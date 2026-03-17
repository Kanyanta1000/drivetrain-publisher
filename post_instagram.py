from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from validate_assets import Inventory, QuestionFolder, collect_inventory, format_summary

DEFAULT_PUBLIC_BASE_URL = "https://kanyanta1000.github.io/drivetrain-publisher"
DEFAULT_TARGET_SUBDIR = "en"
DEFAULT_GRAPH_API_VERSION = "v25.0"
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


def select_next_question(
    inventory: Inventory,
    state: dict[str, Any],
    requested_folder: str | None,
) -> QuestionFolder | None:
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
        return question

    for question in inventory.questions:
        if question.name not in posted_folders:
            return question
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

    if not public_base_url:
        raise PublisherError("PUBLIC_BASE_URL cannot be empty")

    return PublisherConfig(
        ig_user_id=ig_user_id,
        ig_access_token=ig_access_token,
        public_base_url=public_base_url,
        target_subdir=target_subdir,
        graph_api_version=graph_api_version,
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


def print_dry_run(question: QuestionFolder, slide_urls: list[str], already_posted: bool) -> None:
    print(f"Dry run target folder: {question.name}")
    if already_posted:
        print(f"Note: {question.name} is already recorded in posted.json.")
    print("Selected slide files:")
    for slide in question.slides:
        print(f"- {slide.path.relative_to(REPO_ROOT).as_posix()}")
    print("Public URLs:")
    for slide_url in slide_urls:
        print(f"- {slide_url}")


def publish_question(question: QuestionFolder, config: PublisherConfig) -> PublishResult:
    caption, caption_path = read_caption(question)
    slide_urls = [build_public_url(slide.path, config.public_base_url) for slide in question.slides]

    print(f"Posting folder: {question.name}")
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


def update_posted_state(state: dict[str, Any], result: PublishResult) -> None:
    posted_folders = state.setdefault("posted_folders", {})
    posted_folders[result.folder.name] = {
        "caption_path": result.caption_path,
        "carousel_container_id": result.carousel_container_id,
        "child_container_ids": result.child_container_ids,
        "media_publish_id": result.media_publish_id,
        "posted_at_utc": utc_now_iso(),
        "question_number": result.folder.number,
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
            print_dry_run(
                question,
                slide_urls,
                already_posted=requested_folder in state.get("posted_folders", {}),
            )
            return 0

        question = select_next_question(inventory, state, requested_folder)
        if question is None:
            print("No unposted question folders remain.")
            return 0

        slide_urls = [build_public_url(slide.path, config.public_base_url) for slide in question.slides]

        if args.dry_run:
            print_dry_run(question, slide_urls, already_posted=False)
            return 0

        result = publish_question(question, config)
        update_posted_state(state, result)
        save_state(STATE_PATH, state)
        print(f"Updated state file: {STATE_PATH.name}")
        return 0
    except PublisherError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
