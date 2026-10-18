import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .constants import POLYHAVEN_API, USER_AGENT


def _ssl_context(verify_ssl=True):
    if verify_ssl:
        return ssl.create_default_context()
    return ssl._create_unverified_context()  # noqa: SLF001 - explicit user opt-out


def _with_query(url, params=None):
    if not params:
        return url
    query = urllib.parse.urlencode(params, doseq=True)
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}{query}"


def http_json(url, verify_ssl=True, timeout=30, headers=None, params=None):
    url = _with_query(url, params)
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, context=_ssl_context(verify_ssl), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"HTTP {exc.code} while requesting {url}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while requesting {url}: {exc.reason}") from exc



def http_text(url, verify_ssl=True, timeout=30, headers=None, params=None):
    url = _with_query(url, params)
    request_headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, context=_ssl_context(verify_ssl), timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"HTTP {exc.code} while requesting {url}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while requesting {url}: {exc.reason}") from exc

def get_assets(asset_type="all", verify_ssl=True):
    return http_json(f"{POLYHAVEN_API}/assets?t={asset_type}", verify_ssl=verify_ssl)


def get_files(slug, verify_ssl=True):
    return http_json(f"{POLYHAVEN_API}/files/{slug}", verify_ssl=verify_ssl)


def md5sum(path):
    digest = hashlib.md5()  # noqa: S324 - CDN integrity only
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request(url, headers, verify_ssl, timeout):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, context=_ssl_context(verify_ssl), timeout=timeout)


def download_file(
    url,
    destination,
    expected_md5=None,
    verify_ssl=True,
    cancel_event=None,
    retries=3,
    headers=None,
):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        if not expected_md5 or md5sum(destination).lower() == expected_md5.lower():
            return destination

    tmp = destination.with_suffix(destination.suffix + ".part")
    last_error = None

    for attempt in range(max(1, retries)):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Download cancelled")

        resume_from = tmp.stat().st_size if tmp.exists() else 0
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if headers:
            request_headers.update(headers)
        if resume_from > 0:
            request_headers["Range"] = f"bytes={resume_from}-"

        try:
            with _request(url, request_headers, verify_ssl, timeout=90) as response:
                status = getattr(response, "status", 200)
                append = resume_from > 0 and status == 206
                mode = "ab" if append else "wb"
                if not append:
                    resume_from = 0
                with open(tmp, mode) as handle:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise InterruptedError("Download cancelled")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
            last_error = None
            break
        except InterruptedError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= max(1, retries):
                break
            time.sleep(0.75 * (2**attempt))

    if last_error is not None:
        raise RuntimeError(f"Failed downloading {destination.name}: {last_error}") from last_error

    if expected_md5 and md5sum(tmp).lower() != expected_md5.lower():
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"Checksum mismatch for {destination.name}")

    tmp.replace(destination)
    return destination
