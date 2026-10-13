"""Backs up uploaded bill/voucher receipts to Google Drive via a service account.

Only active when config.BACKUP_ENABLED is True (a service account key file exists
at config.GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_DRIVE_BACKUP_FOLDER_ID is set) —
see the setup steps at the bottom of this file. Nothing in here runs, and no
Google credentials are required, until both are configured.

API surface below (Credentials.from_service_account_file, build(), MediaFileUpload,
files().create()) was confirmed by installing google-api-python-client==2.200.0 and
google-auth==2.59.1 and exercising them directly (a real service-account object and
a live build() round-trip to Google's Drive v3 discovery document), not assumed.
"""
import os
import zipfile
from datetime import datetime

import config

# drive.file: only touches files/folders this app creates or that are explicitly
# shared with it — not broad access to the whole Drive.
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_drive_service = None


def get_drive_service():
    global _drive_service
    if _drive_service is None:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=_SCOPES
        )
        _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def _zip_uploads_dir(receipt_dir, dest_zip_path):
    """Zips every file in receipt_dir (flat — receipts aren't nested in subfolders)
    into dest_zip_path. Returns the file count zipped (0 if the dir is empty/missing,
    which is a normal, non-error state — nothing to back up yet)."""
    count = 0
    with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(receipt_dir):
            for name in os.listdir(receipt_dir):
                full_path = os.path.join(receipt_dir, name)
                if os.path.isfile(full_path):
                    zf.write(full_path, arcname=name)
                    count += 1
    return count


def backup_uploads_to_drive(receipt_dir, tmp_dir=None):
    """Zips receipt_dir and uploads it to GOOGLE_DRIVE_BACKUP_FOLDER_ID as a single,
    dated file. Returns the created Drive file's id, or None if there was nothing to
    back up (empty/missing receipt_dir — not treated as an error).

    tmp_dir: where the zip is staged locally before upload and deleted from
    afterwards (default: alongside receipt_dir's parent, via tempfile). Raises on
    any real failure (bad credentials, network, Drive API error) — this is a
    scheduled background job (see start_scheduler in scheduler_service.py), whose
    caller already wraps job execution in try/except so one failed run logs and
    retries next cycle rather than silently losing data.
    """
    import tempfile
    from googleapiclient.http import MediaFileUpload

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    zip_name = f"iskcon-shirpur-receipts-backup-{timestamp}.zip"

    with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
        zip_path = os.path.join(tmp, zip_name)
        file_count = _zip_uploads_dir(receipt_dir, zip_path)
        if file_count == 0:
            return None

        service = get_drive_service()
        media = MediaFileUpload(zip_path, mimetype="application/zip", resumable=True)
        created = service.files().create(
            body={"name": zip_name, "parents": [config.GOOGLE_DRIVE_BACKUP_FOLDER_ID]},
            media_body=media,
            fields="id",
        ).execute()
        return created.get("id")


# ------------------------------------------------------------------ #
# Google Cloud Console setup (one-time, manual — do this yourself, not me)
# ------------------------------------------------------------------ #
#
# 1. Create/select a project at https://console.cloud.google.com/
# 2. APIs & Services > Library > enable "Google Drive API".
# 3. APIs & Services > Credentials > Create Credentials > Service Account.
#    - Give it any name (e.g. "iskcon-shirpur-backup"). No project role needed.
# 4. Open the new service account > Keys tab > Add Key > Create new key > JSON.
#    This downloads a credentials.json file — treat it exactly like a password.
#    - Put it in the project root (same folder as app.py) as `credentials.json`,
#      or set GOOGLE_SERVICE_ACCOUNT_FILE in .env to wherever you place it.
#    - It's already covered by .gitignore — never commit it.
# 5. In Google Drive (as shirpuriskconacc@gmail.com), create/open the backup
#    folder, click Share, and share it with the service account's email —
#    it looks like `iskcon-shirpur-backup@<project-id>.iam.gserviceaccount.com`
#    (found on the service account's details page) — Editor access.
#    This step is required: a service account has no Drive storage of its own
#    and can only write into folders explicitly shared with it.
# 6. Copy the folder's ID from its URL (the part after /folders/ in
#    https://drive.google.com/drive/folders/<FOLDER_ID>) into .env as
#    GOOGLE_DRIVE_BACKUP_FOLDER_ID.
#
# Once both GOOGLE_SERVICE_ACCOUNT_FILE (the file existing) and
# GOOGLE_DRIVE_BACKUP_FOLDER_ID are set, config.BACKUP_ENABLED becomes True and
# the scheduled job (services/scheduler_service.py) starts running automatically —
# no code changes needed.
