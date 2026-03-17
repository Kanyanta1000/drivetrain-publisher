# DriveTrain Instagram Publisher

This repository hosts Instagram carousel images publicly on GitHub Pages and posts one question folder at a time to Instagram with Meta's official publishing API.

The asset tree stays exactly at the repository root so GitHub Pages can serve image URLs like:

`https://kanyanta1000.github.io/drivetrain-publisher/en/question_001/slide_1.png`

GitHub Pages hosting is public. Anyone with the URL can load these images.

## What this project does

- Publishes root-level assets from `en/` through GitHub Pages.
- Validates question folders and slide numbering before posting.
- Posts the next unposted folder in ascending order, or a specific folder on demand.
- Tracks completed posts in `posted.json` so reruns do not duplicate posts.
- Runs automatically on a daily GitHub Actions schedule and can also be run manually.

## Repository structure

```text
.
├── .github/workflows/post-instagram.yml
├── .nojekyll
├── auth/callback.html
├── en/
│   ├── question_001/
│   │   ├── slide_1.png
│   │   ├── slide_2.png
│   │   ├── slide_3.png
│   │   ├── slide_4.png
│   │   └── caption.txt        # optional
│   └── question_800/
├── index.html
├── post_instagram.py
├── posted.json
├── requirements.txt
└── validate_assets.py
```

## Why `en/` stays at the repo root

`en/` must remain at the repository root because GitHub Pages is configured to serve directly from the `main` branch root. That keeps the public asset URLs stable and avoids a `docs/` wrapper or duplicated images.

## Local setup

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Validation

Run the validator:

```bash
python3 validate_assets.py
```

The validator:

- scans `en/`
- finds `question_\d+` folders
- finds `slide_\d+.(png|jpg|jpeg)` files
- sorts numerically, not lexicographically
- requires 2 to 10 slides per folder
- requires slide numbering to be contiguous starting at `slide_1`
- exits non-zero if any folder is invalid

## Dry run

Preview the next folder without calling Instagram:

```bash
python3 post_instagram.py --dry-run
```

This prints:

- the folder that would be posted
- the selected slide files
- the exact public GitHub Pages URLs that would be sent to Instagram

## Test a specific folder locally

Preview a specific question folder:

```bash
python3 post_instagram.py --dry-run --folder question_001
```

Publish a specific folder for real:

```bash
export IG_USER_ID="your-instagram-user-id"
export IG_ACCESS_TOKEN="your-access-token"
export PUBLIC_BASE_URL="https://kanyanta1000.github.io/drivetrain-publisher"
export TARGET_SUBDIR="en"
export GRAPH_API_VERSION="v25.0"
python3 post_instagram.py --folder question_001
```

Run validation only from the posting script:

```bash
python3 post_instagram.py --validate-only
```

## Optional captions

If a question folder contains `caption.txt`, its contents are used as the carousel caption. If `caption.txt` is missing, the post is published with no caption.

## How `posted.json` works

`posted.json` is the durable posting ledger for this repository.

- A folder is added only after a successful `media_publish` call.
- Each entry stores the question number, slide paths, public URLs, publish ids, and timestamp.
- On reruns, the next folder is chosen from the lowest-numbered folder not already recorded.
- If all folders are already recorded, the script exits cleanly without posting anything.

Example shape:

```json
{
  "posted_folders": {
    "question_001": {
      "posted_at_utc": "2026-03-17T18:00:00Z",
      "media_publish_id": "1789..."
    }
  }
}
```

## GitHub Pages setup

Enable GitHub Pages from the repository root:

1. Open the repository on GitHub.
2. Go to `Settings` -> `Pages`.
3. Under `Build and deployment`, choose `Deploy from a branch`.
4. Select branch `main`.
5. Select folder `/ (root)`.
6. Save the changes.

Expected public URL pattern:

`https://kanyanta1000.github.io/drivetrain-publisher/en/question_001/slide_1.png`

The root page is served from `index.html`, and `auth/callback.html` is available to capture the OAuth `code` parameter during Instagram authorization.

## GitHub Actions setup

The workflow file is:

`/.github/workflows/post-instagram.yml`

What it does:

- supports `workflow_dispatch` manual runs
- runs daily on cron `17 7 * * *`
- validates assets first
- runs the posting script second
- commits `posted.json` back to `main` only after a successful publish
- blocks overlapping runs with workflow concurrency

GitHub Actions cron uses UTC, not your local timezone.

To change the schedule, edit the `cron` value in `.github/workflows/post-instagram.yml`.

## Required GitHub Actions secrets

Add these repository secrets in:

`Settings` -> `Secrets and variables` -> `Actions`

Secrets:

- `IG_USER_ID`
- `IG_ACCESS_TOKEN`

Workflow environment defaults:

- `PUBLIC_BASE_URL=https://kanyanta1000.github.io/drivetrain-publisher`
- `TARGET_SUBDIR=en`
- `GRAPH_API_VERSION=v25.0`

## Instagram auth callback helper

During the OAuth authorization flow, set the redirect URI to:

`https://kanyanta1000.github.io/drivetrain-publisher/auth/callback.html`

After approval, the page displays the `code` query parameter so it can be copied and exchanged for an access token.

## Refreshing the Instagram token later

Access tokens expire. Before the current token expires:

1. Refresh or regenerate a fresh long-lived Instagram access token using your Meta app's token flow for the same Instagram account.
2. Replace the `IG_ACCESS_TOKEN` repository secret with the new token.
3. Run the GitHub Actions workflow manually once to confirm authentication still works.

If you change apps, permissions, or the connected Instagram account, also verify that `IG_USER_ID` is still correct.

## Troubleshooting

- `Validation failed`: run `python3 validate_assets.py` and fix the folder listed under `Invalid folders`.
- `PUBLIC_BASE_URL` looks wrong: GitHub Pages must be enabled from `main` branch root, not `docs/`.
- Instagram rejects image URLs: confirm the exact URL opens publicly in a browser with no authentication required.
- A folder will not post again: check `posted.json`; the script refuses to repost folders already recorded there.
- OAuth callback page shows no code: confirm the redirect URI matches `auth/callback.html` exactly and that the app returned a `code` query parameter.
- Workflow runs at the wrong local time: GitHub cron is UTC, so convert the schedule from your timezone before editing the cron expression.
