"""
capabilities/r2_storage.py
Zero-extra-dependency Cloudflare R2 Object Storage client using S3-compatible
AWS Signature Version 4 (HMAC-SHA256) over HTTPS.
Organizes all Sovereign media downloads (MP3/MP4), full database backups,
source snapshots, enhancement notes, and changelogs into structured YYYY/MM/DD folders.
"""

import os
import hmac
import hashlib
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Any, Optional
import requests

from profiles import get_secret_or_env


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(secret_key: str, date_stamp: str, region: str = "auto", service: str = "s3") -> bytes:
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


class CloudflareR2Manager:
    """
    Manages persistent storage on Cloudflare R2 with automatic YYYY/MM/DD folder organization:
      - media/YYYY/MM/DD/<filename>.mp3|.mp4
      - backups/YYYY/MM/DD/databases/<db_filename>
      - backups/YYYY/MM/DD/source/Aurora-KoA-Full-<HHMMSS>.zip
      - backups/YYYY/MM/DD/changelogs/CHANGELOG_<HHMMSS>.md
      - backups/latest/<db_filename>
    """

    def __init__(self):
        self.account_id = get_secret_or_env("R2_ACCOUNT_ID", "")
        self.access_key = get_secret_or_env("R2_ACCESS_KEY_ID", "")
        self.secret_key = get_secret_or_env("R2_SECRET_ACCESS_KEY", "")
        self.bucket = get_secret_or_env("R2_BUCKET_NAME", "aurora-sovereign-vault")
        self.public_url = get_secret_or_env("R2_PUBLIC_URL", "").rstrip("/")
        self.region = "auto"
        self.last_r2_status: str = "Not synced yet"

    @property
    def is_configured(self) -> bool:
        return bool(self.account_id and self.access_key and self.secret_key and self.bucket)

    @property
    def endpoint_host(self) -> str:
        return f"{self.account_id}.r2.cloudflarestorage.com"

    @staticmethod
    def build_dated_key(root_folder: str, filename: str, subfolder: str = "") -> str:
        now = datetime.now(timezone.utc)
        date_path = now.strftime("%Y/%m/%d")
        clean_root = root_folder.strip("/")
        clean_sub = subfolder.strip("/")
        clean_file = os.path.basename(filename).replace(" ", "_")
        if clean_sub:
            return f"{clean_root}/{date_path}/{clean_sub}/{clean_file}"
        return f"{clean_root}/{date_path}/{clean_file}"

    def _signed_request(
        self,
        method: str,
        object_key: str = "",
        query_params: Optional[Dict[str, str]] = None,
        payload: bytes = b"",
        content_type: str = "application/octet-stream",
    ) -> requests.Response:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        clean_key = object_key.lstrip("/")
        encoded_key = urllib.parse.quote(clean_key, safe="/-_.~")
        canonical_uri = f"/{self.bucket}/{encoded_key}" if encoded_key else f"/{self.bucket}"

        qp = query_params or {}
        sorted_qp = sorted((urllib.parse.quote(k, safe="-_.~"), urllib.parse.quote(str(v), safe="-_.~")) for k, v in qp.items())
        canonical_querystring = "&".join(f"{k}={v}" for k, v in sorted_qp)

        payload_hash = hashlib.sha256(payload).hexdigest()
        headers_to_sign = {
            "host": self.endpoint_host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if method in ("PUT", "POST"):
            headers_to_sign["content-type"] = content_type

        sorted_header_keys = sorted(headers_to_sign.keys())
        canonical_headers = "".join(f"{k}:{headers_to_sign[k]}\n" for k in sorted_header_keys)
        signed_headers = ";".join(sorted_header_keys)

        canonical_request = "\n".join([
            method,
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            algorithm,
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        signing_key = _get_signature_key(self.secret_key, date_stamp, self.region, "s3")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization_header = (
            f"{algorithm} Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        req_headers = {
            "Authorization": authorization_header,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }
        if method in ("PUT", "POST"):
            req_headers["Content-Type"] = content_type

        url = f"https://{self.endpoint_host}{canonical_uri}"
        if canonical_querystring:
            url = f"{url}?{canonical_querystring}"

        return requests.request(method, url, headers=req_headers, data=payload, timeout=35)

    def upload_bytes(self, object_key: str, data: bytes, content_type: str = "application/octet-stream") -> Tuple[bool, str]:
        if not self.is_configured:
            return False, "Cloudflare R2 credentials not configured in Secrets."
        try:
            resp = self._signed_request("PUT", object_key=object_key, payload=data, content_type=content_type)
            if resp.status_code in (200, 201, 204):
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                self.last_r2_status = f"Uploaded {object_key} ({ts})"
                return True, f"Stored in R2: `{object_key}`"
            return False, f"R2 upload error ({resp.status_code}): {resp.text[:200]}"
        except Exception as e:
            self.last_r2_status = f"R2 error: {e}"
            return False, f"R2 upload exception: {e}"

    def download_bytes(self, object_key: str) -> Tuple[bool, bytes, str]:
        if not self.is_configured:
            return False, b"", "Cloudflare R2 credentials not configured."
        try:
            resp = self._signed_request("GET", object_key=object_key)
            if resp.status_code == 200:
                return True, resp.content, f"Downloaded `{object_key}` ({len(resp.content):,} bytes)"
            return False, b"", f"R2 fetch returned {resp.status_code}"
        except Exception as e:
            return False, b"", f"R2 download exception: {e}"

    def list_objects(self, prefix: str = "", max_keys: int = 100) -> List[Dict[str, Any]]:
        if not self.is_configured:
            return []
        try:
            qp = {"list-type": "2", "max-keys": str(max_keys)}
            if prefix:
                qp["prefix"] = prefix
            resp = self._signed_request("GET", object_key="", query_params=qp)
            if resp.status_code != 200:
                return []

            root = ET.fromstring(resp.text)
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            items: List[Dict[str, Any]] = []
            for contents in root.findall(".//s3:Contents", ns) or root.findall(".//Contents"):
                key_el = contents.find("s3:Key", ns) if contents.find("s3:Key", ns) is not None else contents.find("Key")
                size_el = contents.find("s3:Size", ns) if contents.find("s3:Size", ns) is not None else contents.find("Size")
                mod_el = contents.find("s3:LastModified", ns) if contents.find("s3:LastModified", ns) is not None else contents.find("LastModified")
                if key_el is not None and key_el.text:
                    key_str = key_el.text
                    items.append({
                        "key": key_str,
                        "size": int(size_el.text) if (size_el is not None and size_el.text) else 0,
                        "last_modified": mod_el.text if (mod_el is not None and mod_el.text) else "",
                        "public_url": f"{self.public_url}/{key_str}" if self.public_url else "",
                    })
            return sorted(items, key=lambda x: x["last_modified"], reverse=True)
        except Exception as e:
            print(f"[R2 List Notice]: {e}")
            return []

    def archive_sovereign_media(self, filename: str, data: bytes, mime_type: str = "audio/mpeg") -> Tuple[bool, str, str]:
        dated_key = self.build_dated_key("media", filename)
        ok, msg = self.upload_bytes(dated_key, data, content_type=mime_type)
        public_link = f"{self.public_url}/{dated_key}" if (ok and self.public_url) else ""
        return ok, dated_key, public_link or msg

    def run_full_dated_backup(
        self,
        db_snapshots: Dict[str, bytes],
        project_zip_bytes: bytes,
        changelog_markdown: str,
    ) -> Tuple[bool, str]:
        if not self.is_configured:
            return False, "Cloudflare R2 is not configured."

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%H%M%S")
        uploaded_count = 0
        errors: List[str] = []

        for db_name, raw_bytes in db_snapshots.items():
            if not raw_bytes:
                continue
            dated_db_key = self.build_dated_key("backups", db_name, subfolder="databases")
            ok, msg = self.upload_bytes(dated_db_key, raw_bytes, content_type="application/octet-stream")
            if ok:
                uploaded_count += 1
                self.upload_bytes(f"backups/latest/{os.path.basename(db_name)}", raw_bytes, content_type="application/octet-stream")
            else:
                errors.append(msg)

        if project_zip_bytes:
            zip_key = self.build_dated_key("backups", f"Aurora-KoA-Full-{stamp}.zip", subfolder="source")
            ok_z, msg_z = self.upload_bytes(zip_key, project_zip_bytes, content_type="application/zip")
            if ok_z:
                uploaded_count += 1
            else:
                errors.append(msg_z)

        if changelog_markdown:
            log_key = self.build_dated_key("backups", f"CHANGELOG_{stamp}.md", subfolder="changelogs")
            ok_c, msg_c = self.upload_bytes(log_key, changelog_markdown.encode("utf-8"), content_type="text/markdown")
            if ok_c:
                uploaded_count += 1
            else:
                errors.append(msg_c)

        date_folder = now.strftime("backups/%Y/%m/%d/")
        if errors:
            return False, f"R2 partial backup ({uploaded_count} ok) in `{date_folder}`: {'; '.join(errors[:2])}"
        return True, f"Full R2 backup archived ({uploaded_count} artifacts) under `{date_folder}`"
