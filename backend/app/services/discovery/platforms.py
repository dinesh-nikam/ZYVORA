"""Social-platform registry: domain allowlist, profile-URL parsing and
canonicalisation, search scoping and live-check policy.

Adding a platform = adding one ``Platform`` entry with a parser.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import parse_qs, unquote

import httpx


class FetchPolicy(StrEnum):
    OFFICIAL_API = "official_api"   # verified through a documented, official API
    PUBLIC_PAGE = "public_page"     # public, robots-permitted preview page
    NONE = "none"                   # no live checks (ToS / login walls); index evidence only


@dataclass(frozen=True, slots=True)
class ProfileRef:
    platform: str
    kind: str
    handle: str | None
    canonical_url: str


Parser = Callable[[list[str], httpx.URL], ProfileRef | None]


@dataclass(frozen=True, slots=True)
class Platform:
    key: str
    name: str
    domains: tuple[str, ...]
    fetch_policy: FetchPolicy
    search_sites: tuple[str, ...]
    parser: Parser
    limitation: str
    extra_limitations: tuple[str, ...] = field(default_factory=tuple)

    def parse(self, url: httpx.URL) -> ProfileRef | None:
        segments = [unquote(s) for s in url.path.split("/") if s]
        return self.parser(segments, url)


_NO_LIVE = ("Live profile checks are not performed for this platform (automated access is restricted by its terms "
            "and no official lookup API is configured); status relies on public search-index evidence only.")


def _ref(platform: str, kind: str, handle: str | None, url: str) -> ProfileRef:
    return ProfileRef(platform=platform, kind=kind, handle=handle, canonical_url=url)


# --------------------------------------------------------------------- parsers
_TG_HANDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_TG_RESERVED = frozenset({
    "addstickers", "addemoji", "share", "proxy", "socks", "iv", "login", "setlanguage", "addtheme", "bg",
    "confirmphone", "invoice", "boost", "contact", "addlist", "joinchat", "k", "a", "z", "web",
})


def _telegram(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if not seg:
        return None
    first = seg[0]
    if first == "s" and len(seg) >= 2:
        first = seg[1]
    if first.startswith("+") or (first == "joinchat" and len(seg) >= 2):
        code = first[1:] if first.startswith("+") else seg[1]
        if not re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", code):
            return None
        return _ref("telegram", "invite", None, f"https://t.me/+{code}")
    if first.lower() in _TG_RESERVED or not _TG_HANDLE.match(first):
        return None
    handle = first.lower()
    return _ref("telegram", "username", handle, f"https://t.me/{handle}")


def _whatsapp(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    host = url.host.lower()
    if host.endswith("wa.me"):
        if len(seg) == 1 and seg[0].isdigit() and 7 <= len(seg[0]) <= 15:
            return _ref("whatsapp", "click_to_chat", seg[0], f"https://wa.me/{seg[0]}")
        if len(seg) == 2 and seg[0] == "c" and seg[1].isdigit():
            return _ref("whatsapp", "business_catalog", seg[1], f"https://wa.me/c/{seg[1]}")
        if len(seg) == 2 and seg[0] == "message" and re.fullmatch(r"[A-Z0-9]{6,32}", seg[1]):
            return _ref("whatsapp", "message_link", seg[1], f"https://wa.me/message/{seg[1]}")
        return None
    if host.startswith("chat.whatsapp.com") and len(seg) == 1 and re.fullmatch(r"[A-Za-z0-9]{10,40}", seg[0]):
        return _ref("whatsapp", "group_invite", None, f"https://chat.whatsapp.com/{seg[0]}")
    if len(seg) == 2 and seg[0] == "channel" and re.fullmatch(r"[A-Za-z0-9]{10,40}", seg[1]):
        return _ref("whatsapp", "channel", seg[1], f"https://whatsapp.com/channel/{seg[1]}")
    if seg[:1] == ["send"]:
        phone = parse_qs(url.query.decode()).get("phone", [""])[0]
        digits = re.sub(r"\D", "", phone)
        if 7 <= len(digits) <= 15:
            return _ref("whatsapp", "click_to_chat", digits, f"https://wa.me/{digits}")
    return None


_IG_RESERVED = frozenset({
    "p", "reel", "reels", "explore", "accounts", "stories", "tv", "direct", "about", "legal", "developer", "web",
    "challenge", "emails", "press", "api", "static", "graphql", "privacy", "terms", "oauth", "session", "ar",
    "create", "lite", "directory", "topics", "locations", "nametag",
})
_IG_HANDLE = re.compile(r"^(?!.*\.\.)(?!\.)[A-Za-z0-9._]{1,30}(?<!\.)$")


def _instagram(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if not seg or seg[0].lower() in _IG_RESERVED or not _IG_HANDLE.match(seg[0]):
        return None
    handle = seg[0].lower()
    return _ref("instagram", "profile", handle, f"https://www.instagram.com/{handle}/")


_FB_RESERVED = frozenset({
    "groups", "events", "watch", "marketplace", "login", "login.php", "help", "sharer", "sharer.php", "share",
    "story.php", "photo.php", "photo", "photos", "permalink.php", "hashtag", "policies", "privacy", "gaming",
    "business", "ads", "settings", "notifications", "messages", "friends", "search", "home.php", "recover",
    "reel", "reels", "videos", "l.php", "dialog", "plugins", "legal", "terms", "about", "careers",
    "fundraisers", "bookmarks", "media", "stories", "checkpoint", "tr", "posts", "public", "places", "pages",
    "people", "profile.php", "signup", "r.php", "reg", "help.php", "mobile", "directory", "live", "lite",
})


def _facebook(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if not seg:
        return None
    if seg[0] == "profile.php":
        pid = parse_qs(url.query.decode()).get("id", [""])[0]
        if pid.isdigit() and 5 <= len(pid) <= 20:
            return _ref("facebook", "profile", pid, f"https://www.facebook.com/profile.php?id={pid}")
        return None
    if seg[0] in ("people", "pages") and len(seg) >= 3 and seg[2].isdigit():
        return _ref("facebook", "profile" if seg[0] == "people" else "page", seg[2],
                    f"https://www.facebook.com/profile.php?id={seg[2]}")
    if seg[0].lower() in _FB_RESERVED:
        return None
    if re.fullmatch(r"[A-Za-z0-9.]{5,50}", seg[0]):
        handle = seg[0].lower()
        return _ref("facebook", "profile", handle, f"https://www.facebook.com/{handle}")
    return None


_LI_SLUG = re.compile(r"^[A-Za-z0-9\-_%.]{3,100}$")


def _linkedin(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if len(seg) < 2 or not _LI_SLUG.match(seg[1]):
        return None
    kinds = {"in": "profile", "pub": "profile", "company": "company", "school": "school", "showcase": "company"}
    kind = kinds.get(seg[0].lower())
    if kind is None:
        return None
    slug = seg[1].lower()
    section = "in" if seg[0].lower() == "pub" else seg[0].lower()
    return _ref("linkedin", kind, slug, f"https://www.linkedin.com/{section}/{slug}/")


_X_RESERVED = frozenset({
    "home", "explore", "search", "i", "intent", "share", "hashtag", "settings", "login", "signup", "tos",
    "privacy", "notifications", "messages", "compose", "logout", "account", "about", "download", "jobs",
    "lists", "communities", "premium", "verified-choose", "rules", "help", "en", "web", "oauth", "who_to_follow",
})


def _x(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if not seg or seg[0].lower() in _X_RESERVED or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", seg[0]):
        return None
    handle = seg[0].lower()
    return _ref("x", "profile", handle, f"https://x.com/{handle}")


def _youtube(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if not seg:
        return None
    if seg[0].startswith("@") and re.fullmatch(r"@[A-Za-z0-9._\-]{3,30}", seg[0]):
        handle = seg[0][1:].lower()
        return _ref("youtube", "channel", handle, f"https://www.youtube.com/@{handle}")
    if seg[0] == "channel" and len(seg) >= 2 and re.fullmatch(r"UC[A-Za-z0-9_\-]{22}", seg[1]):
        return _ref("youtube", "channel_id", seg[1], f"https://www.youtube.com/channel/{seg[1]}")
    if seg[0] in ("c", "user") and len(seg) >= 2 and re.fullmatch(r"[A-Za-z0-9._\-]{2,100}", seg[1]):
        return _ref("youtube", "legacy_" + seg[0], seg[1].lower(),
                    f"https://www.youtube.com/{seg[0]}/{seg[1].lower()}")
    return None


def _tiktok(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if seg and re.fullmatch(r"@[A-Za-z0-9._]{2,24}", seg[0]):
        handle = seg[0][1:].lower()
        return _ref("tiktok", "profile", handle, f"https://www.tiktok.com/@{handle}")
    return None


def _reddit(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if len(seg) >= 2 and seg[0].lower() in ("user", "u") and re.fullmatch(r"[A-Za-z0-9_\-]{3,20}", seg[1]):
        name = seg[1].lower()
        return _ref("reddit", "user", name, f"https://www.reddit.com/user/{name}/")
    return None


_GH_RESERVED = frozenset({
    "about", "account", "apps", "blog", "collections", "contact", "customer-stories", "enterprise", "events",
    "explore", "features", "github", "issues", "join", "login", "logout", "marketplace", "new", "notifications",
    "orgs", "organizations", "pricing", "pulls", "readme", "search", "security", "settings", "site", "sponsors",
    "team", "topics", "trending", "codespaces", "copilot", "dashboard", "discussions", "resources", "solutions",
    "signup", "password_reset", "sessions", "stars", "watching", "premium-support", "mobile", "partners",
})
_GH_HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def _github(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if len(seg) != 1 or seg[0].lower() in _GH_RESERVED or not _GH_HANDLE.match(seg[0]):
        return None
    handle = seg[0].lower()
    return _ref("github", "user", handle, f"https://github.com/{handle}")


def _at_handle(platform: str, base: str, pattern: str) -> Parser:
    def parse(seg: list[str], url: httpx.URL) -> ProfileRef | None:
        if seg and re.fullmatch("@" + pattern, seg[0]):
            handle = seg[0][1:].lower()
            return _ref(platform, "profile", handle, f"{base}/@{handle}")
        return None
    return parse


def _snapchat(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if len(seg) >= 2 and seg[0] == "add" and re.fullmatch(r"[A-Za-z][A-Za-z0-9._\-]{2,14}", seg[1]):
        handle = seg[1].lower()
        return _ref("snapchat", "profile", handle, f"https://www.snapchat.com/add/{handle}")
    return None


_PIN_RESERVED = frozenset({"pin", "search", "ideas", "today", "business", "login", "settings", "_", "categories",
                           "about", "explore", "videos", "resource"})


def _pinterest(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if len(seg) == 1 and seg[0].lower() not in _PIN_RESERVED and re.fullmatch(r"[A-Za-z0-9_]{3,30}", seg[0]):
        handle = seg[0].lower()
        return _ref("pinterest", "profile", handle, f"https://www.pinterest.com/{handle}/")
    return None


_VK_RESERVED = frozenset({"feed", "im", "friends", "groups", "search", "settings", "login", "join", "video",
                          "music", "apps", "support", "about", "dev", "legal", "terms", "privacy", "away.php"})


def _vk(seg: list[str], url: httpx.URL) -> ProfileRef | None:
    if len(seg) == 1 and seg[0].lower() not in _VK_RESERVED and re.fullmatch(r"[A-Za-z0-9_.]{3,32}", seg[0]):
        handle = seg[0].lower()
        return _ref("vk", "profile", handle, f"https://vk.com/{handle}")
    return None


# -------------------------------------------------------------------- registry
PLATFORMS: dict[str, Platform] = {p.key: p for p in (
    Platform("telegram", "Telegram", ("t.me", "telegram.me", "telegram.dog"), FetchPolicy.PUBLIC_PAGE,
             ("t.me",), _telegram,
             "Telegram accounts are never looked up by phone number; only public usernames/channels are checked "
             "via the official Bot API or the public t.me preview page.",
             ("Invite links (t.me/+...) are private and are never opened.",)),
    Platform("whatsapp", "WhatsApp", ("wa.me", "whatsapp.com", "chat.whatsapp.com", "api.whatsapp.com"),
             FetchPolicy.NONE, ("wa.me", "whatsapp.com"), _whatsapp,
             "WhatsApp registration status is never checked: there is no official public API for it and "
             "undocumented endpoints are not used. Click-to-chat links are format-only."),
    Platform("instagram", "Instagram", ("instagram.com",), FetchPolicy.NONE, ("instagram.com",), _instagram,
             _NO_LIVE),
    Platform("facebook", "Facebook", ("facebook.com", "fb.com"), FetchPolicy.NONE, ("facebook.com",), _facebook,
             _NO_LIVE),
    Platform("linkedin", "LinkedIn", ("linkedin.com",), FetchPolicy.NONE, ("linkedin.com/in", "linkedin.com/company"),
             _linkedin, _NO_LIVE),
    Platform("x", "X (Twitter)", ("x.com", "twitter.com"), FetchPolicy.NONE, ("x.com", "twitter.com"), _x, _NO_LIVE),
    Platform("youtube", "YouTube", ("youtube.com",), FetchPolicy.OFFICIAL_API, ("youtube.com",), _youtube,
             "YouTube channels are checked only through the official YouTube Data API v3 when an API key is configured."),
    Platform("tiktok", "TikTok", ("tiktok.com",), FetchPolicy.NONE, ("tiktok.com",), _tiktok, _NO_LIVE),
    Platform("reddit", "Reddit", ("reddit.com",), FetchPolicy.OFFICIAL_API, ("reddit.com/user",), _reddit,
             "Reddit users are checked only through the official OAuth API when client credentials are configured."),
    Platform("github", "GitHub", ("github.com",), FetchPolicy.OFFICIAL_API, ("github.com",), _github,
             "GitHub users are checked through the official GitHub REST API (public profile fields only)."),
    Platform("threads", "Threads", ("threads.net", "threads.com"), FetchPolicy.NONE, ("threads.net",),
             _at_handle("threads", "https://www.threads.net", r"[A-Za-z0-9._]{1,30}"), _NO_LIVE),
    Platform("snapchat", "Snapchat", ("snapchat.com",), FetchPolicy.NONE, ("snapchat.com/add",), _snapchat, _NO_LIVE),
    Platform("pinterest", "Pinterest", ("pinterest.com",), FetchPolicy.NONE, ("pinterest.com",), _pinterest, _NO_LIVE),
    Platform("vk", "VK", ("vk.com",), FetchPolicy.NONE, ("vk.com",), _vk, _NO_LIVE),
    Platform("medium", "Medium", ("medium.com",), FetchPolicy.NONE, ("medium.com",),
             _at_handle("medium", "https://medium.com", r"[A-Za-z0-9._\-]{1,50}"), _NO_LIVE),
)}

ALL_DOMAINS: frozenset[str] = frozenset(d for p in PLATFORMS.values() for d in p.domains)
DEFAULT_PLATFORMS: tuple[str, ...] = tuple(PLATFORMS)


def platform_for_host(host: str) -> Platform | None:
    host = host.lower().rstrip(".")
    for platform in PLATFORMS.values():
        if any(host == d or host.endswith("." + d) for d in platform.domains):
            return platform
    return None


def identify(url: str | httpx.URL) -> tuple[Platform, ProfileRef] | None:
    """Return (platform, profile) for a recognised public profile URL, else None."""
    try:
        parsed = url if isinstance(url, httpx.URL) else httpx.URL(url.strip())
    except (httpx.InvalidURL, ValueError):
        return None
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return None
    platform = platform_for_host(parsed.host)
    if platform is None:
        return None
    ref = platform.parse(parsed)
    if ref is None:
        return None
    return platform, ref


_DOMAIN_ALTERNATION = "|".join(sorted((re.escape(d) for d in ALL_DOMAINS), key=len, reverse=True))
_URL_IN_TEXT = re.compile(
    rf"(?<![\w./@\-])(?:https?://)?(?:[a-z0-9\-]+\.)*(?:{_DOMAIN_ALTERNATION})/[^\s\"'<>()\[\]{{}}|\\^`,;]+",
    re.IGNORECASE,
)


def extract_profile_urls(text: str | None) -> list[tuple[Platform, ProfileRef]]:
    """Find recognised profile URLs mentioned inside free text (deduplicated)."""
    if not text:
        return []
    found: dict[str, tuple[Platform, ProfileRef]] = {}
    for raw in _URL_IN_TEXT.findall(text[:100_000]):
        candidate = raw.rstrip(".!?…")
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = "https://" + candidate
        hit = identify(candidate)
        if hit is not None:
            found.setdefault(hit[1].canonical_url, hit)
    return list(found.values())


def whatsapp_click_to_chat(e164: str) -> ProfileRef:
    digits = re.sub(r"\D", "", e164)
    return _ref("whatsapp", "click_to_chat", digits, f"https://wa.me/{digits}")
