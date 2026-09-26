from __future__ import annotations

import io
import posixpath
import re
import warnings
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any, Callable, Iterable, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from PIL import Image, ImageOps, UnidentifiedImageError

from ..search.fc2_images import fc2_image_path_allowed
from ..indexers.metadata_catalog import image_url_allowed
from ..config.source_catalog import METADATA_CATALOG, METADATA_PROFILES
from ..net.http_client import DEFAULT_HEADERS
from .sources import MediaMetadata, MetadataImageCandidate
from ..net.network_guard import PublicHostResolver
from ..net.pinned_http import PinnedHTTPHandler, PinnedHTTPSHandler


ImageOrientation = Literal["portrait", "landscape", "square"]

DEFAULT_IMAGE_TIMEOUT_SECONDS = 15.0
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000
MAX_REDIRECTS = 3

_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/avif",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }
)
_IMAGE_ACCEPT = "image/avif,image/webp,image/png,image/jpeg;q=0.9,*/*;q=0.1"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_JAVBUS_EXTERNAL_IMAGE_HOSTS = frozenset({"awsimgsrc.dmm.co.jp", "pics.dmm.co.jp"})
_JAVBUS_CDN_IMAGE_PATHS = (
    "/pics/",
    "/cover/",
    "/covers/",
    "/sample/",
    "/samples/",
    "/thumb/",
    "/thumbs/",
)


class MetadataImageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedMetadataImage:
    body: bytes = field(repr=False)
    width: int
    height: int
    orientation: ImageOrientation
    source_id: str
    parser_profile: str
    source_kind: str

    @property
    def jpeg_bytes(self) -> bytes:
        return self.body


@dataclass(frozen=True, slots=True)
class MetadataArtworkSelection:
    portrait: DecodedMetadataImage | None
    landscape: DecodedMetadataImage | None


@dataclass(frozen=True, slots=True)
class _ImagePolicy:
    profile: str
    base_origin: tuple[str, str, int]
    initial_origin: tuple[str, str, int]
    referer: str
    product_id: str | None = None


class _RestrictedImageRedirectHandler(HTTPRedirectHandler):
    def __init__(
        self,
        candidate: MetadataImageCandidate,
        policy: _ImagePolicy,
        resolver: PublicHostResolver,
    ) -> None:
        super().__init__()
        self._candidate = candidate
        self._policy = policy
        self._resolver = resolver
        self._redirects = 0

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        if code not in _REDIRECT_STATUSES or self._redirects >= MAX_REDIRECTS:
            raise MetadataImageError("metadata image redirect was rejected")
        absolute = urljoin(req.full_url, newurl)
        if _origin(_split_http_url(absolute)) != self._policy.initial_origin:
            raise MetadataImageError(
                "metadata image cross-origin redirect was rejected"
            )
        _validate_image_url(
            absolute,
            candidate=self._candidate,
            policy=self._policy,
            resolver=self._resolver,
        )
        self._redirects += 1
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def fetch_metadata_image(
    candidate: MetadataImageCandidate,
    *,
    timeout: float = DEFAULT_IMAGE_TIMEOUT_SECONDS,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> DecodedMetadataImage:
    if not isinstance(candidate, MetadataImageCandidate):
        raise MetadataImageError("metadata image candidate is invalid")
    if timeout <= 0 or timeout > 60:
        raise MetadataImageError("metadata image timeout is invalid")
    if max_bytes <= 0 or max_bytes > 64 * 1024 * 1024:
        raise MetadataImageError("metadata image byte limit is invalid")
    if max_pixels <= 0 or max_pixels > 200_000_000:
        raise MetadataImageError("metadata image pixel limit is invalid")

    resolver = PublicHostResolver(max_hosts=3)
    policy = _policy_for_candidate(candidate)
    _validate_image_url(
        candidate.url,
        candidate=candidate,
        policy=policy,
        resolver=resolver,
    )
    headers = dict(DEFAULT_HEADERS)
    headers.update({"Accept": _IMAGE_ACCEPT, "Referer": policy.referer})
    request = Request(candidate.url, headers=headers)
    opener = build_opener(
        ProxyHandler(),
        PinnedHTTPHandler(),
        PinnedHTTPSHandler(),
        _RestrictedImageRedirectHandler(candidate, policy, resolver),
    )
    response = None
    try:
        response = opener.open(request, timeout=timeout)
        final_url = str(response.geturl() or candidate.url)
        final = _validate_image_url(
            final_url,
            candidate=candidate,
            policy=policy,
            resolver=resolver,
        )
        if _origin(final) != policy.initial_origin:
            raise MetadataImageError("metadata image response origin was rejected")
        content_type = _content_type(response.headers)
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise MetadataImageError("metadata image content type was rejected")
        declared_length = _content_length(response.headers)
        if declared_length is not None and declared_length > max_bytes:
            raise MetadataImageError("metadata image exceeded its byte limit")
        body = response.read(max_bytes + 1)
        if not isinstance(body, bytes) or len(body) > max_bytes:
            raise MetadataImageError("metadata image exceeded its byte limit")
        if declared_length is not None and len(body) != declared_length:
            raise MetadataImageError("metadata image length did not match its response")
    except MetadataImageError:
        raise
    except HTTPError:
        raise MetadataImageError(
            "metadata image upstream returned an HTTP error"
        ) from None
    except (URLError, HTTPException, TimeoutError, OSError, ValueError):
        raise MetadataImageError("metadata image request failed") from None
    finally:
        if response is not None:
            response.close()

    jpeg_bytes, width, height = _decode_as_jpeg(
        body,
        max_pixels=max_pixels,
        max_output_bytes=max_bytes,
    )
    return DecodedMetadataImage(
        body=jpeg_bytes,
        width=width,
        height=height,
        orientation=classify_image_orientation(width, height),
        source_id=candidate.source_id,
        parser_profile=candidate.parser_profile,
        source_kind=candidate.kind,
    )


def classify_image_orientation(width: int, height: int) -> ImageOrientation:
    if isinstance(width, bool) or isinstance(height, bool) or width <= 0 or height <= 0:
        raise MetadataImageError("metadata image dimensions are invalid")
    if width >= height * 1.15:
        return "landscape"
    if height >= width * 1.15:
        return "portrait"
    return "square"


def select_metadata_artwork(
    candidates: Iterable[MetadataImageCandidate],
    *,
    fetcher: Callable[
        [MetadataImageCandidate], DecodedMetadataImage
    ] = fetch_metadata_image,
    max_candidates_per_profile: int = 12,
    need_portrait: bool = True,
    need_landscape: bool = True,
) -> MetadataArtworkSelection:
    """Select real portrait/landscape images with JavBus-first fallback."""

    limit = max(1, min(int(max_candidates_per_profile), 24))
    grouped = tuple(candidates)
    portrait: DecodedMetadataImage | None = None
    landscape: DecodedMetadataImage | None = None
    spread_cover_fallbacks: list[DecodedMetadataImage] = []
    center_crop_fallbacks: list[DecodedMetadataImage] = []
    for profile in ("javbus", "javdb", "fc2", *METADATA_CATALOG):
        needed_portrait = need_portrait and portrait is None
        needed_landscape = need_landscape and landscape is None
        if not needed_portrait and not needed_landscape:
            break
        fetched = 0
        profile_portraits: list[DecodedMetadataImage] = []
        profile_landscapes: list[DecodedMetadataImage] = []
        profile_spread_covers: list[DecodedMetadataImage] = []
        profile_center_covers: list[DecodedMetadataImage] = []
        for candidate in grouped:
            if candidate.parser_profile != profile:
                continue
            kind = str(candidate.kind or "")
            wants_portrait = needed_portrait and kind in {"cover", "backdrop"}
            wants_landscape = needed_landscape and (
                kind in {"backdrop", "cover", "sample"}
            )
            if not wants_portrait and not wants_landscape:
                continue
            if fetched >= limit:
                break
            fetched += 1
            try:
                image = fetcher(candidate)
            except MetadataImageError:
                continue
            if wants_portrait and image.orientation == "portrait":
                profile_portraits.append(image)
            if wants_landscape and image.orientation == "landscape":
                profile_landscapes.append(image)
            if wants_portrait and kind in {"cover", "backdrop"}:
                if _looks_like_spread_cover(image):
                    profile_spread_covers.append(image)
                elif kind == "cover" and image.orientation != "portrait":
                    profile_center_covers.append(image)
        if needed_portrait and profile_portraits:
            portrait = max(
                profile_portraits, key=lambda image: image.width * image.height
            )
        if needed_landscape and profile_landscapes:
            landscape = max(
                profile_landscapes,
                key=lambda image: image.width * image.height,
            )
        if profile_spread_covers:
            spread_cover_fallbacks.append(
                max(
                    profile_spread_covers,
                    key=lambda image: image.width * image.height,
                )
            )
        if profile_center_covers:
            center_crop_fallbacks.append(
                max(
                    profile_center_covers,
                    key=lambda image: image.width * image.height,
                )
            )
    if need_portrait:
        front: DecodedMetadataImage | None = None
        for spread in spread_cover_fallbacks:
            try:
                front = _front_cover_from_spread(spread)
            except MetadataImageError:
                continue
            break
        # Sites often expose only a thumbnail-sized native portrait (JavBus
        # search cards are 147x200). The front half of the full-size spread
        # is the same artwork at print resolution, so prefer it when larger.
        if front is not None and (
            portrait is None
            or portrait.width * portrait.height
            < _SMALL_PORTRAIT_RATIO * front.width * front.height
        ):
            portrait = front
        if portrait is None:
            # Square or 16:9 covers (common for FC2) have no printed front
            # half; a centred 2:3 crop still gives media servers a poster.
            for cover in center_crop_fallbacks:
                try:
                    portrait = _center_portrait(cover)
                except MetadataImageError:
                    continue
                break
    return MetadataArtworkSelection(portrait=portrait, landscape=landscape)


_SMALL_PORTRAIT_RATIO = 0.6
_MIN_CENTER_PORTRAIT_WIDTH = 120


def _center_portrait(image: DecodedMetadataImage) -> DecodedMetadataImage:
    if image.orientation == "portrait":
        raise MetadataImageError("metadata cover is already portrait")
    try:
        with Image.open(io.BytesIO(image.body)) as source:
            normalized = ImageOps.exif_transpose(source)
            normalized.load()
            width, height = normalized.size
            crop_width = min(width, (height * 2) // 3)
            if crop_width < _MIN_CENTER_PORTRAIT_WIDTH:
                raise MetadataImageError("metadata cover is too small to crop")
            left = (width - crop_width) // 2
            front = normalized.crop((left, 0, left + crop_width, height)).convert("RGB")
            output = io.BytesIO()
            front.save(output, format="JPEG", quality=92, optimize=True, progressive=True)
    except MetadataImageError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MetadataImageError("metadata cover could not be cropped safely") from exc
    body = output.getvalue()
    front_width, front_height = front.size
    if len(body) > MAX_IMAGE_BYTES:
        raise MetadataImageError("metadata cover exceeded its encoded byte limit")
    if classify_image_orientation(front_width, front_height) != "portrait":
        raise MetadataImageError("metadata centred cover is not portrait")
    return DecodedMetadataImage(
        body=body,
        width=front_width,
        height=front_height,
        orientation="portrait",
        source_id=image.source_id,
        parser_profile=image.parser_profile,
        source_kind=image.source_kind,
    )


def _looks_like_spread_cover(image: DecodedMetadataImage) -> bool:
    # JavBus labels its full-size spread "backdrop"; other sites call it cover.
    if image.source_kind not in {"cover", "backdrop"} or image.orientation != "landscape":
        return False
    ratio = image.width / image.height
    return 1.35 <= ratio <= 1.65 and image.width >= 2 and image.height >= 2


def _front_cover_from_spread(
    image: DecodedMetadataImage,
) -> DecodedMetadataImage:
    if not _looks_like_spread_cover(image):
        raise MetadataImageError("metadata cover is not a supported spread")
    try:
        with Image.open(io.BytesIO(image.body)) as source:
            normalized = ImageOps.exif_transpose(source)
            normalized.load()
            width, height = normalized.size
            if width != image.width or height != image.height:
                raise MetadataImageError("metadata cover dimensions changed")
            front = normalized.crop((width // 2, 0, width, height)).convert("RGB")
            output = io.BytesIO()
            front.save(
                output,
                format="JPEG",
                quality=92,
                optimize=True,
                progressive=True,
            )
    except MetadataImageError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MetadataImageError("metadata cover could not be cropped safely") from exc
    body = output.getvalue()
    front_width, front_height = front.size
    if len(body) > MAX_IMAGE_BYTES:
        raise MetadataImageError("metadata cover exceeded its encoded byte limit")
    if classify_image_orientation(front_width, front_height) != "portrait":
        raise MetadataImageError("metadata front cover is not portrait")
    return DecodedMetadataImage(
        body=body,
        width=front_width,
        height=front_height,
        orientation="portrait",
        source_id=image.source_id,
        parser_profile=image.parser_profile,
        source_kind=image.source_kind,
    )


def select_artwork(
    metadata: MediaMetadata,
    settings: dict[str, Any] | None = None,
) -> tuple[DecodedMetadataImage | None, DecodedMetadataImage | None]:
    """Return publisher-compatible portrait and landscape artwork."""

    del settings  # Candidates already carry the server-validated source policy.
    if not isinstance(metadata, MediaMetadata):
        raise MetadataImageError("media metadata is invalid")
    selected = select_metadata_artwork(metadata.image_candidates)
    return selected.portrait, selected.landscape


def _policy_for_candidate(candidate: MetadataImageCandidate) -> _ImagePolicy:
    profile = str(candidate.parser_profile or "")
    if profile not in METADATA_PROFILES:
        raise MetadataImageError("metadata image source profile is invalid")
    base = _split_http_url(candidate.base_url)
    detail = _split_http_url(candidate.detail_url)
    fanza_video_detail = profile == "fanza" and _origin(detail) == ("https", "video.dmm.co.jp", 443)
    if profile != "fc2" and not fanza_video_detail and _origin(detail) != _origin(base):
        raise MetadataImageError("metadata image detail origin is invalid")
    initial = _split_http_url(candidate.url)
    product_id = _fc2_detail_product_id(detail) if profile == "fc2" else None
    if profile == "fc2" and product_id is None:
        raise MetadataImageError("FC2 metadata detail identity is invalid")
    return _ImagePolicy(
        profile=profile,
        base_origin=_origin(base),
        initial_origin=_origin(initial),
        referer=candidate.detail_url,
        product_id=product_id,
    )


def _validate_image_url(
    value: str,
    *,
    candidate: MetadataImageCandidate,
    policy: _ImagePolicy,
    resolver: PublicHostResolver,
) -> SplitResult:
    parsed = _split_http_url(value)
    if _origin(parsed) != policy.initial_origin:
        raise MetadataImageError("metadata image origin is invalid")
    hostname = str(parsed.hostname or "").rstrip(".").lower()
    if not resolver.is_public(hostname):
        raise MetadataImageError("metadata image host must resolve publicly")
    path = _normalized_path(parsed.path)
    if policy.profile == "javbus":
        _validate_javbus_path(parsed, path, policy)
    elif policy.profile == "javdb":
        # JavDB uses changing public CDNs. The exact origin is pinned to the
        # server-side detail candidate for the whole redirect chain.
        if (
            _origin(parsed) != policy.base_origin
            and parsed.port is not None
            and parsed.port != (443 if parsed.scheme.lower() == "https" else 80)
        ) or not _looks_like_raster_path(path):
            raise MetadataImageError("metadata image path is invalid")
    elif policy.profile == "fc2":
        _validate_fc2_path(parsed, path, policy)
    elif policy.profile in METADATA_CATALOG:
        if not image_url_allowed(policy.profile, value, candidate.base_url):
            raise MetadataImageError("metadata image path is invalid")
    else:
        raise MetadataImageError("metadata image source profile is invalid")
    if value != candidate.url and _origin(parsed) != policy.initial_origin:
        raise MetadataImageError("metadata image redirect origin is invalid")
    return parsed


def _validate_javbus_path(parsed: SplitResult, path: str, policy: _ImagePolicy) -> None:
    scheme, hostname, port = _origin(parsed)
    _, base_host, _ = policy.base_origin
    if hostname == base_host:
        allowed = _origin(parsed) == policy.base_origin and path.startswith("/pics/")
    else:
        site_domain = base_host.removeprefix("www.")
        allowed_cdn = (
            bool(site_domain)
            and scheme == "https"
            and port == 443
            and hostname == f"pics.{site_domain}"
            and path.startswith(_JAVBUS_CDN_IMAGE_PATHS)
        )
        allowed_dmm = (
            scheme == "https"
            and port == 443
            and hostname in _JAVBUS_EXTERNAL_IMAGE_HOSTS
            and path.startswith("/pics_dig/")
        )
        allowed = allowed_cdn or allowed_dmm
    if not allowed or not _looks_like_raster_path(path):
        raise MetadataImageError("metadata image path is invalid")


def _fc2_detail_product_id(parsed: SplitResult) -> str | None:
    scheme, hostname, port = _origin(parsed)
    if scheme != "https" or port != 443 or parsed.username or parsed.password:
        return None
    if hostname not in {"adult.contents.fc2.com", "ppvdatabank.com"}:
        return None
    match = re.fullmatch(r"/article/(\d{2,9})/", parsed.path)
    return match.group(1) if match else None


def _validate_fc2_path(
    parsed: SplitResult,
    path: str,
    policy: _ImagePolicy,
) -> None:
    scheme, hostname, port = _origin(parsed)
    product_id = policy.product_id or ""
    if scheme != "https" or port != 443 or not product_id or parsed.query:
        raise MetadataImageError("metadata image path is invalid")
    if not fc2_image_path_allowed(hostname, path, product_id=product_id):
        raise MetadataImageError("metadata image path is invalid")


def _split_http_url(value: object) -> SplitResult:
    raw = str(value or "")
    if (
        not raw
        or len(raw) > 8192
        or raw != raw.strip()
        or "#" in raw
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
    ):
        raise MetadataImageError("metadata image URL is invalid")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise MetadataImageError("metadata image URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise MetadataImageError("metadata image URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise MetadataImageError("metadata image URL credentials are forbidden")
    if port is not None and not 1 <= port <= 65535:
        raise MetadataImageError("metadata image URL is invalid")
    return parsed


def _normalized_path(path: str) -> str:
    decoded = str(path or "")
    for _ in range(6):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if "%" in decoded or "\\" in decoded or "\x00" in decoded:
        raise MetadataImageError("metadata image path is invalid")
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise MetadataImageError("metadata image path is invalid")
    normalized = posixpath.normpath(decoded)
    if not normalized.startswith("/") or normalized.startswith("//"):
        raise MetadataImageError("metadata image path is invalid")
    return normalized.casefold()


def _origin(parsed: SplitResult) -> tuple[str, str, int]:
    scheme = parsed.scheme.lower()
    hostname = str(parsed.hostname or "").rstrip(".").lower()
    return scheme, hostname, parsed.port or (443 if scheme == "https" else 80)


def _looks_like_raster_path(path: str) -> bool:
    return path.endswith((".avif", ".jpeg", ".jpg", ".png", ".webp"))


def _content_type(headers: Any) -> str:
    return str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()


def _content_length(headers: Any) -> int | None:
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _decode_as_jpeg(
    body: bytes,
    *,
    max_pixels: int,
    max_output_bytes: int,
) -> tuple[bytes, int, int]:
    if not body:
        raise MetadataImageError("metadata image response was empty")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    raise MetadataImageError("metadata image exceeded its pixel limit")
                source.verify()
            with Image.open(io.BytesIO(body)) as source:
                normalized = ImageOps.exif_transpose(source)
                normalized.load()
                width, height = normalized.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    raise MetadataImageError("metadata image exceeded its pixel limit")
                rgb = normalized.convert("RGB")
                output = io.BytesIO()
                rgb.save(
                    output,
                    format="JPEG",
                    quality=92,
                    optimize=True,
                    progressive=True,
                )
                if output.tell() > max_output_bytes:
                    raise MetadataImageError(
                        "metadata image exceeded its encoded byte limit"
                    )
                return output.getvalue(), width, height
    except MetadataImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MetadataImageError("metadata image exceeded its pixel limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MetadataImageError("metadata image could not be decoded safely") from exc


__all__ = [
    "DecodedMetadataImage",
    "ImageOrientation",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MetadataImageError",
    "MetadataArtworkSelection",
    "classify_image_orientation",
    "fetch_metadata_image",
    "select_artwork",
    "select_metadata_artwork",
]
