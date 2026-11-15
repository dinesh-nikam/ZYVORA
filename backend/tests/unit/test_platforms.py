from __future__ import annotations

import pytest

from app.services.discovery.platforms import extract_profile_urls, identify, whatsapp_click_to_chat


@pytest.mark.parametrize(("url", "platform", "kind", "canonical"), [
    ("https://t.me/AcmeHelp", "telegram", "username", "https://t.me/acmehelp"),
    ("http://telegram.me/acmehelp/", "telegram", "username", "https://t.me/acmehelp"),
    ("https://t.me/s/durov", "telegram", "username", "https://t.me/durov"),
    ("https://t.me/acmenews/123", "telegram", "username", "https://t.me/acmenews"),
    ("https://t.me/+AbCdEfGhIjKl", "telegram", "invite", "https://t.me/+AbCdEfGhIjKl"),
    ("https://wa.me/919876543210", "whatsapp", "click_to_chat", "https://wa.me/919876543210"),
    ("https://api.whatsapp.com/send?phone=+91%2098765%2043210", "whatsapp", "click_to_chat",
     "https://wa.me/919876543210"),
    ("https://whatsapp.com/channel/0029VaABCDEFGHIJ1234", "whatsapp", "channel",
     "https://whatsapp.com/channel/0029VaABCDEFGHIJ1234"),
    ("https://www.instagram.com/Acme_Support/?hl=en", "instagram", "profile", "https://www.instagram.com/acme_support/"),
    ("https://m.facebook.com/profile.php?id=100012345678", "facebook", "profile",
     "https://www.facebook.com/profile.php?id=100012345678"),
    ("https://www.facebook.com/acme.store", "facebook", "profile", "https://www.facebook.com/acme.store"),
    ("https://in.linkedin.com/in/jane-doe-1234/", "linkedin", "profile", "https://www.linkedin.com/in/jane-doe-1234/"),
    ("https://www.linkedin.com/company/acme", "linkedin", "company", "https://www.linkedin.com/company/acme/"),
    ("https://twitter.com/AcmeCo", "x", "profile", "https://x.com/acmeco"),
    ("https://x.com/acmeco/status/1", "x", "profile", "https://x.com/acmeco"),
    ("https://www.youtube.com/@AcmeTV", "youtube", "channel", "https://www.youtube.com/@acmetv"),
    ("https://www.youtube.com/channel/UC1234567890abcdefghijAB", "youtube", "channel_id",
     "https://www.youtube.com/channel/UC1234567890abcdefghijAB"),
    ("https://www.tiktok.com/@acme.official", "tiktok", "profile", "https://www.tiktok.com/@acme.official"),
    ("https://old.reddit.com/u/AcmeMod", "reddit", "user", "https://www.reddit.com/user/acmemod/"),
    ("https://github.com/AcmeDev", "github", "user", "https://github.com/acmedev"),
    ("https://www.threads.net/@acme", "threads", "profile", "https://www.threads.net/@acme"),
    ("https://medium.com/@acme", "medium", "profile", "https://medium.com/@acme"),
])
def test_identifies_profiles(url: str, platform: str, kind: str, canonical: str) -> None:
    hit = identify(url)
    assert hit is not None, url
    assert hit[0].key == platform
    assert hit[1].kind == kind
    assert hit[1].canonical_url == canonical


@pytest.mark.parametrize("url", [
    "https://t.me/share/url?url=x",
    "https://t.me/joinchat",
    "https://www.instagram.com/p/Cxyz123/",
    "https://www.instagram.com/explore/",
    "https://www.facebook.com/groups/12345",
    "https://www.facebook.com/sharer.php?u=x",
    "https://twitter.com/intent/tweet",
    "https://x.com/home",
    "https://github.com/features",
    "https://github.com/acme/repo",
    "https://www.linkedin.com/feed/",
    "https://www.reddit.com/r/python",
    "https://evil.com/instagram.com/acme",
    "https://instagram.com.evil.com/acme",
    "ftp://t.me/acme",
    "javascript:alert(1)",
    "not a url",
    "https://wa.me/123",
])
def test_rejects_non_profile_urls(url: str) -> None:
    assert identify(url) is None


def test_extracts_urls_from_text() -> None:
    text = ("Contact us on t.me/acmehelp, https://www.instagram.com/acme_support or wa.me/919876543210. "
            "Ignore https://example.com/instagram.com/x and https://www.instagram.com/p/xyz/.")
    found = {ref.canonical_url for _, ref in extract_profile_urls(text)}
    assert found == {"https://t.me/acmehelp", "https://www.instagram.com/acme_support/", "https://wa.me/919876543210"}


def test_whatsapp_link_generation() -> None:
    ref = whatsapp_click_to_chat("+919876543210")
    assert ref.canonical_url == "https://wa.me/919876543210"
    assert ref.kind == "click_to_chat"
