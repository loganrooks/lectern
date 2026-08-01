"""YouTube Data API playlist discovery: transport, parsing, and identity.

The only module in the automation package that opens a network connection.
Keeping it here is the point: the state store cannot reach a transport, so a
read surface built over the store does not transitively acquire one.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from lectern.records import (
    AutomationError,
    SourceItem,
    SourceKind,
    SourcePolicy,
    SourceRecord,
    make_source_id,
    make_source_item_id,
    metadata_to_json,
    now_timestamp,
)

DEFAULT_YOUTUBE_API_KEY_ENV = "YOUTUBE_API_KEY"

YOUTUBE_PLAYLIST_ITEMS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlistItems"

YOUTUBE_PLAYLIST_PARTS = "snippet,contentDetails"

YOUTUBE_PLAYLIST_PAGE_SIZE = 50

YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE = 1

YOUTUBE_REQUEST_TIMEOUT_S = 20.0

YOUTUBE_METADATA_ONLY_ERROR = (
    "YouTube media acquisition is not implemented; M4 supports metadata-only discovery"
)

YOUTUBE_PLACEHOLDER_TITLES = frozenset({"Private video", "Deleted video"})

HttpGet = Callable[[str, float], bytes]


class YouTubeAPIError(AutomationError):
    """Raised when YouTube Data API returns a structured request failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class YouTubePreflight:
    playlist_id: str
    api_key_env: str
    credential_present: bool
    reachable: bool
    pages_checked: int
    estimated_units_consumed: int
    error: str | None

    @property
    def ok(self) -> bool:
        return self.credential_present and self.reachable and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "playlist_id": self.playlist_id,
            "api_key_env": self.api_key_env,
            "credential_present": self.credential_present,
            "reachable": self.reachable,
            "pages_checked": self.pages_checked,
            "estimated_units_consumed": self.estimated_units_consumed,
            "error": self.error,
            "ok": self.ok,
        }


def normalize_youtube_playlist_id(playlist: str) -> str:
    value = playlist.strip()
    if not value:
        raise AutomationError("YouTube playlist ID is required")
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError as exc:
        # urlparse raises on inputs like "http://[broken"; callers (CLI included)
        # only handle AutomationError, so let the domain error carry it.
        raise AutomationError("YouTube playlist ID or URL could not be parsed") from exc
    if parsed.scheme or parsed.netloc:
        query = urllib.parse.parse_qs(parsed.query)
        values = query.get("list", [])
        value = values[0].strip() if values else ""
        if not value:
            raise AutomationError("YouTube playlist URL must include a non-empty list parameter")
    if any(character.isspace() for character in value):
        raise AutomationError("YouTube playlist ID must not contain whitespace")
    return value


def _metadata_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(metadata_to_json(payload).encode("utf-8")).hexdigest()


def _object_field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AutomationError(f"YouTube API response field is not an object: {key}")
    return cast(dict[str, Any], value)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _youtube_source_item(
    source: SourceRecord,
    playlist_id: str,
    raw: object,
    *,
    page_index: int,
) -> SourceItem:
    if not isinstance(raw, dict):
        raise AutomationError("YouTube API response item was not an object")
    item = cast(dict[str, Any], raw)
    snippet = _object_field(item, "snippet")
    content_details = _object_field(item, "contentDetails")
    playlist_item_id = _optional_string(item.get("id"))
    video_id = _optional_string(content_details.get("videoId"))
    if video_id is None:
        resource_id = _object_field(snippet, "resourceId")
        video_id = _optional_string(resource_id.get("videoId"))
    if video_id is None:
        raise AutomationError("YouTube playlist item missing video ID")
    # Identity is the video, not the playlist slot: playlist-item IDs change on
    # remove-and-re-add and repeat when one video is listed twice in a playlist.
    relative_path = f"{playlist_id}/{video_id}"
    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    video_url = (
        "https://www.youtube.com/watch?"
        f"{urllib.parse.urlencode({'v': video_id, 'list': playlist_id})}"
    )
    title = _optional_string(snippet.get("title"))
    channel_id = _optional_string(snippet.get("channelId"))
    channel_title = _optional_string(snippet.get("channelTitle"))
    published_at = _optional_string(snippet.get("publishedAt"))
    # videoOwnerChannel* are snippet properties; contentDetails carries videoPublishedAt.
    video_owner_channel_id = _optional_string(snippet.get("videoOwnerChannelId"))
    video_owner_channel_title = _optional_string(snippet.get("videoOwnerChannelTitle"))
    video_published_at = _optional_string(content_details.get("videoPublishedAt"))
    position = _optional_int(snippet.get("position"))
    # Title alone does not identify a tombstone: a real public video may be
    # titled "Private video". API tombstones also drop the availability fields
    # real entries carry, so require their absence before excluding the item.
    placeholder = (
        title in YOUTUBE_PLACEHOLDER_TITLES
        and video_owner_channel_id is None
        and video_published_at is None
    )
    # Digest holds content-meaningful fields only. Playlist item ID, position,
    # and timestamps are positional/curation noise excluded per accepted design
    # constraint H1: including them turns reorders and remove-and-re-adds into
    # spurious re-enqueues.
    digest_payload = {
        "playlist_id": playlist_id,
        "video_id": video_id,
        "title": title,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "video_owner_channel_id": video_owner_channel_id,
        "video_owner_channel_title": video_owner_channel_title,
    }
    metadata = {
        "source": {
            "kind": SourceKind.YOUTUBE_PLAYLIST.value,
            "source_id": source.id,
            "source_name": source.name,
        },
        "playlist": {
            "id": playlist_id,
            "url": playlist_url,
        },
        "playlist_item": {
            "id": playlist_item_id,
            "position": position,
            "published_at": published_at,
        },
        "video": {
            "id": video_id,
            "url": video_url,
            "title": title,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "video_owner_channel_id": video_owner_channel_id,
            "video_owner_channel_title": video_owner_channel_title,
            "published_at": video_published_at,
            "placeholder": placeholder,
        },
        "discovery": {
            "adapter": "youtube-playlist",
            "api": "youtube-data-api-v3",
            "method": "playlistItems.list",
            "part": YOUTUBE_PLAYLIST_PARTS,
            "page_index": page_index,
            "units_per_page": YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE,
        },
    }
    now = now_timestamp()
    return SourceItem(
        id=make_source_item_id(source.id, relative_path),
        source_id=source.id,
        relative_path=relative_path,
        absolute_path=video_url,
        sha256=_metadata_digest(digest_payload),
        size_bytes=0,
        mtime_ns=0,
        present=True,
        created_at=now,
        updated_at=now,
        metadata=metadata,
    )


def urllib_get(url: str, timeout_s: float) -> bytes:
    """The adapter's default HTTP transport.

    Public so a caller can substitute it without reaching into a private
    name: the adapter resolves it from this module at construction time.
    """

    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return cast(bytes, response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except (http.client.HTTPException, OSError):
            # A truncated error body must not escape as a raw protocol error:
            # exceptions raised inside this handler bypass the sibling handlers
            # below, and the status code alone still makes a usable domain error.
            body = b""
        raise _youtube_error_from_response(exc.code, body) from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        # http.client raises HTTPException (IncompleteRead and friends) outside the
        # URLError/OSError hierarchy, so a truncated response would otherwise escape
        # as a raw protocol error instead of an AutomationError.
        detail = getattr(exc, "reason", exc)
        raise AutomationError(f"YouTube Data API request failed: {detail}") from exc


def _youtube_error_from_response(status_code: int, body: bytes) -> YouTubeAPIError:
    reason: str | None = None
    message: str | None = None
    try:
        payload_obj: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload_obj = None
    if isinstance(payload_obj, dict):
        payload = cast(dict[str, Any], payload_obj)
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            error = cast(dict[str, Any], error_obj)
            message = _optional_string(error.get("message"))
            errors_obj = error.get("errors")
            if isinstance(errors_obj, list) and errors_obj:
                errors = cast(list[object], errors_obj)
                first_error = errors[0]
                if isinstance(first_error, dict):
                    reason = _optional_string(cast(dict[str, Any], first_error).get("reason"))
    reason_part = f" {reason}" if reason else ""
    detail = message or "request failed"
    return YouTubeAPIError(
        f"YouTube Data API error ({status_code}{reason_part}): {detail}",
        status_code=status_code,
        reason=reason,
    )


class YouTubePlaylistAdapter:
    """Discover public YouTube playlist metadata with an API key."""

    def __init__(
        self,
        api_key: str,
        *,
        api_key_env: str = DEFAULT_YOUTUBE_API_KEY_ENV,
        transport: HttpGet | None = None,
        max_pages: int | None = None,
        max_results: int = YOUTUBE_PLAYLIST_PAGE_SIZE,
        timeout_s: float = YOUTUBE_REQUEST_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise AutomationError(f"missing YouTube API key; set {api_key_env}")
        if not 1 <= max_results <= YOUTUBE_PLAYLIST_PAGE_SIZE:
            raise AutomationError("YouTube playlist page size must be between 1 and 50")
        if max_pages is not None and max_pages < 1:
            # A nonpositive cap would return an empty page-zero result that carries
            # no truncation marker, which scan_source would treat as a complete
            # scan and mark every stored item removed.
            raise AutomationError("max_pages must be a positive integer")
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._transport = transport or urllib_get
        self._max_pages = max_pages
        self._max_results = max_results
        self._timeout_s = timeout_s
        self._scan_metadata: dict[str, Any] = {}
        self._pages_attempted = 0

    @classmethod
    def from_environment(
        cls,
        *,
        api_key_env: str = DEFAULT_YOUTUBE_API_KEY_ENV,
        environ: Mapping[str, str] | None = None,
        transport: HttpGet | None = None,
        max_pages: int | None = None,
        max_results: int = YOUTUBE_PLAYLIST_PAGE_SIZE,
    ) -> YouTubePlaylistAdapter:
        env = environ if environ is not None else os.environ
        return cls(
            env.get(api_key_env, ""),
            api_key_env=api_key_env,
            transport=transport,
            max_pages=max_pages,
            max_results=max_results,
        )

    @property
    def scan_metadata(self) -> dict[str, Any]:
        return dict(self._scan_metadata)

    @property
    def pages_attempted(self) -> int:
        """Page requests issued during the last discover, including failed ones."""

        return self._pages_attempted

    @property
    def attempted_quota_units(self) -> int:
        """Quota units the last discover attempted, whether or not it succeeded."""

        return self._pages_attempted * YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE

    def discover(self, source: SourceRecord) -> list[SourceItem]:
        if source.kind is not SourceKind.YOUTUBE_PLAYLIST:
            raise AutomationError(
                f"YouTube playlist adapter cannot scan source kind: {source.kind.value}"
            )
        self._scan_metadata = {}
        self._pages_attempted = 0
        playlist_id = normalize_youtube_playlist_id(source.root_path)
        page_token: str | None = None
        pages_fetched = 0
        items: list[SourceItem] = []
        next_page_token_present = False
        requested_page_tokens: set[str] = set()

        while True:
            if self._max_pages is not None and pages_fetched >= self._max_pages:
                next_page_token_present = page_token is not None
                break
            payload = self._fetch_playlist_page(playlist_id, page_token=page_token)
            page_index = pages_fetched
            pages_fetched += 1
            raw_items_obj = payload.get("items")
            if not isinstance(raw_items_obj, list):
                raise AutomationError("YouTube API response missing items list")
            raw_items = cast(list[object], raw_items_obj)
            items.extend(
                _youtube_source_item(source, playlist_id, raw, page_index=page_index)
                for raw in raw_items
            )
            raw_next_page_token = payload.get("nextPageToken")
            # A present-but-non-string token is not an end-of-playlist signal:
            # treating it as one would commit a partial scan as a complete one
            # and mark every unfetched item removed.
            if raw_next_page_token is not None and not isinstance(raw_next_page_token, str):
                raise AutomationError("YouTube API returned a malformed nextPageToken")
            if isinstance(raw_next_page_token, str) and raw_next_page_token:
                # A token equal to one already requested would page the same
                # results forever, consuming quota without completing the scan.
                if raw_next_page_token in requested_page_tokens:
                    raise AutomationError("YouTube API returned a repeated nextPageToken")
                requested_page_tokens.add(raw_next_page_token)
                page_token = raw_next_page_token
                next_page_token_present = True
                continue
            next_page_token_present = False
            break

        estimated_units = pages_fetched * YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE
        self._scan_metadata = {
            "source_kind": SourceKind.YOUTUBE_PLAYLIST.value,
            "youtube": {
                "playlist_id": playlist_id,
                "api": "youtube-data-api-v3",
                "method": "playlistItems.list",
                "part": YOUTUBE_PLAYLIST_PARTS,
            },
            "quota": {
                "units_per_page": YOUTUBE_PLAYLIST_QUOTA_UNITS_PER_PAGE,
                "pages_fetched": pages_fetched,
                "estimated_units_consumed": estimated_units,
                "next_page_token_present": next_page_token_present,
                "truncated_by_max_pages": (self._max_pages is not None and next_page_token_present),
            },
        }
        return items

    def _fetch_playlist_page(
        self,
        playlist_id: str,
        *,
        page_token: str | None,
    ) -> dict[str, Any]:
        params = {
            "part": YOUTUBE_PLAYLIST_PARTS,
            "playlistId": playlist_id,
            "maxResults": str(self._max_results),
            "key": self._api_key,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        url = f"{YOUTUBE_PLAYLIST_ITEMS_ENDPOINT}?{urllib.parse.urlencode(params)}"
        # Count the page before the call: the quota unit is spent as soon as the
        # request is issued, so a post-request failure still consumed it.
        self._pages_attempted += 1
        body = self._transport(url, self._timeout_s)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AutomationError("YouTube API response was not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise AutomationError("YouTube API response was not a JSON object")
        return cast(dict[str, Any], payload)


def preflight_youtube_playlist(
    playlist: str,
    *,
    api_key: str | None = None,
    api_key_env: str = DEFAULT_YOUTUBE_API_KEY_ENV,
    environ: Mapping[str, str] | None = None,
    transport: HttpGet | None = None,
) -> YouTubePreflight:
    # Resolve the credential before normalization so an invalid playlist still
    # reports whether a key was supplied; reporting "absent" would send the
    # operator after a credential they already have.
    env = environ if environ is not None else os.environ
    resolved_api_key = api_key if api_key is not None else env.get(api_key_env, "")
    try:
        playlist_id = normalize_youtube_playlist_id(playlist)
    except AutomationError as exc:
        return YouTubePreflight(
            playlist_id=playlist,
            api_key_env=api_key_env,
            credential_present=bool(resolved_api_key),
            reachable=False,
            pages_checked=0,
            estimated_units_consumed=0,
            error=str(exc),
        )

    if not resolved_api_key:
        return YouTubePreflight(
            playlist_id=playlist_id,
            api_key_env=api_key_env,
            credential_present=False,
            reachable=False,
            pages_checked=0,
            estimated_units_consumed=0,
            error=f"missing YouTube API key; set {api_key_env}",
        )

    adapter = YouTubePlaylistAdapter(
        resolved_api_key,
        api_key_env=api_key_env,
        transport=transport,
        max_pages=1,
        max_results=1,
    )
    source = SourceRecord(
        id=make_source_id(SourceKind.YOUTUBE_PLAYLIST.value, playlist_id),
        kind=SourceKind.YOUTUBE_PLAYLIST,
        name="youtube-preflight",
        root_path=playlist_id,
        policy=SourcePolicy.SCAN_ONLY,
        created_at=now_timestamp(),
        updated_at=now_timestamp(),
    )
    try:
        adapter.discover(source)
    except AutomationError as exc:
        # A page request that failed after it was issued still spent its quota
        # unit; reporting zero would understate what the preflight consumed.
        return YouTubePreflight(
            playlist_id=playlist_id,
            api_key_env=api_key_env,
            credential_present=True,
            reachable=False,
            pages_checked=adapter.pages_attempted,
            estimated_units_consumed=adapter.attempted_quota_units,
            error=str(exc),
        )
    metadata = adapter.scan_metadata
    quota = cast(dict[str, Any], metadata.get("quota", {}))
    pages_checked = int(quota.get("pages_fetched", 1))
    estimated_units = int(quota.get("estimated_units_consumed", pages_checked))
    return YouTubePreflight(
        playlist_id=playlist_id,
        api_key_env=api_key_env,
        credential_present=True,
        reachable=True,
        pages_checked=pages_checked,
        estimated_units_consumed=estimated_units,
        error=None,
    )
