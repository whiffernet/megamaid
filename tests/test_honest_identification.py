"""The shipped scaffold identifies itself; it does not impersonate a browser.

megamaid shipped a spoofed Chrome User-Agent as `sitemap_discovery`'s default —
the function behind `sitemap_crawl`, the skill's first-preference pattern —
while SKILL.md and references/troubleshooting.md both forbade exactly that in
the scaffold. Prohibition in prose did not prevent the drift, so it is enforced
here.

The one legitimate `Mozilla/5.0` in the codebase is inside DEFAULT_USER_AGENT,
where it is a conventional compatibility token preceded by megamaid's own name
and repo URL. That is identification, not impersonation.
"""

import pathlib
import sys

import httpx

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid.constants import DEFAULT_USER_AGENT  # noqa: E402
from megamaid.discovery import sitemap_discovery  # noqa: E402

#: Every directory whose contents reach a user's machine. `skills/` matters most
#: and was the original omission: the Python inside `patterns/*.md` is copied
#: verbatim into generated projects, so it is a *larger* surface than `src/`,
#: and a spoofed UA added there shipped with the guard staying green.
_SCANNED_DIRS = ("src", "templates", "skills", "commands")

#: Impersonation signals. `Mozilla/5.0` alone was the original check, and it
#: caught only the symptom the deleted PerimeterX helper happened to carry —
#: that helper's own docstring credited the bypass to the `Sec-Fetch-*` set,
#: which the old guard did not look at. Each of these passed before:
#: `Chrome/131.0.0.0 Safari/537.36`, a bare `AppleWebKit/537.36`, `Opera/`,
#: and a full `Sec-Fetch-*` header set with no User-Agent at all.
_OFFENDER_PATTERNS = (
    "Mozilla/5.0",
    "AppleWebKit",
    "Safari/5",
    "Sec-Fetch-",
    "Opera/",
)

#: A line naming megamaid is identifying, not impersonating — that is the whole
#: point of `DEFAULT_USER_AGENT`'s conventional compatibility token.
_HONEST_MARKER = "megamaid/"

_FENCE = "```"


def _shipped_lines(repo_root: pathlib.Path):
    """Yield (path, lineno, line) for every line of shipped, executable content.

    Python is scanned whole. Markdown is scanned only inside fenced code blocks:
    `references/troubleshooting.md` legitimately *discusses* `Mozilla/5.0` in
    prose to explain why not to use it, and flagging that would push the guard
    toward being switched off.

    Deliberately not scanned: anything that is not `.py` or `.md`. In particular
    `src/megamaid_setup/known_hashes.json` embeds `ast.dump` output for every
    historical runtime file, including old User-Agent literals, and is generated
    rather than written.
    """
    for directory in _SCANNED_DIRS:
        root = repo_root / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".py", ".md"):
                continue
            markdown = path.suffix == ".md"
            fenced = False
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if markdown and line.lstrip().startswith(_FENCE):
                    fenced = not fenced
                    continue
                if markdown and not fenced:
                    continue
                yield path, lineno, line


def test_no_spoofed_browser_identity_in_shipped_content(repo_root):
    """Nothing shipped may claim to be a browser it is not.

    A line carrying an impersonation signal is allowed only when it also names
    megamaid — i.e. it is the honest UA, or a command using it.
    """
    offenders: list[str] = []

    for path, lineno, line in _shipped_lines(repo_root):
        hits = [pattern for pattern in _OFFENDER_PATTERNS if pattern in line]
        if not hits or _HONEST_MARKER in line:
            continue
        offenders.append(
            f"{path.relative_to(repo_root)}:{lineno}: [{', '.join(hits)}] {line.strip()}"
        )

    assert not offenders, (
        "shipped content impersonates a browser:\n  "
        + "\n  ".join(offenders)
        + "\n\nIdentify as megamaid instead — see non-negotiable #5 in SKILL.md."
    )


def test_the_scan_actually_reaches_shipped_content(repo_root):
    """Vacuity canary.

    Anchored on the scan reaching files, not on any literal appearing in them.
    The previous version asserted a `Mozilla/5.0` hit existed somewhere, which
    made the most honest possible follow-up — dropping the compatibility token
    from `DEFAULT_USER_AGENT` — fail with "the pattern is probably broken".
    """
    scanned = {path for path, _, _ in _shipped_lines(repo_root)}
    assert DEFAULT_USER_AGENT.strip(), "DEFAULT_USER_AGENT is empty"
    assert _HONEST_MARKER in DEFAULT_USER_AGENT, (
        f"DEFAULT_USER_AGENT is {DEFAULT_USER_AGENT!r}, which does not name megamaid"
    )
    for directory in _SCANNED_DIRS:
        assert any(directory in str(path.relative_to(repo_root)) for path in scanned), (
            f"the scan reached no file under {directory}/ — it would pass while "
            "checking nothing there"
        )


def test_sitemap_discovery_sends_the_honest_user_agent(monkeypatch):
    """The default must be the honest UA, asserted on the wire, not by reading source.

    A source-level check would pass if the constant were right but never sent.
    """
    seen: dict[str, str] = {}

    def fake_get(url, **kwargs):
        seen["user_agent"] = kwargs.get("headers", {}).get("User-Agent", "")
        return httpx.Response(200, text="<urlset></urlset>", request=httpx.Request("GET", url))

    monkeypatch.setattr("megamaid.discovery.httpx.get", fake_get)

    import asyncio

    asyncio.run(sitemap_discovery("https://example.com"))

    assert seen["user_agent"] == DEFAULT_USER_AGENT, (
        f"sitemap_discovery sent {seen['user_agent']!r}, expected the honest {DEFAULT_USER_AGENT!r}"
    )
    assert "megamaid/" in seen["user_agent"]


def test_policy_states_the_positive_requirement(repo_root):
    """The non-negotiable must say what the scaffold DOES, not only what it must not.

    Phrased purely as prohibition, a spoofed default violated no stated rule.
    """
    skill = (repo_root / "skills" / "megamaid" / "SKILL.md").read_text()
    assert "The scaffold identifies itself." in skill, (
        "non-negotiable #5 states only prohibitions; it needs a positive "
        "requirement that the scaffold identify itself"
    )
    assert "DEFAULT_USER_AGENT" in skill
