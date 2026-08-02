"""Browser impersonation must be declared, never incidental.

megamaid once shipped a spoofed Chrome User-Agent as `sitemap_discovery`'s
default — the function behind the skill's first-preference pattern — while the
docs forbade exactly that. The fix deleted the capability, which cost a mode
that measurably still works and did not stop anyone re-adding one inline later.

So the rule enforced here is not "never present as a browser". It is: present as
a browser only from a module that declares an `ACCESS_MODE` and is registered in
`_CAPABILITY_MODULES`, so every such mode reaches the capability table users read
and the manifest a run writes. Undeclared impersonation fails the build.

`DEFAULT_USER_AGENT` remains the default and carries a conventional `Mozilla/5.0`
compatibility token after megamaid's own name and URL. That is identification.
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

#: Modules permitted to send browser-shaped requests, each implementing a
#: declared access mode. This is the point of the guard: not that megamaid
#: cannot present as a browser, but that it cannot do so *undeclared*. A new
#: bypass has to be added here, and anything added here has to carry an
#: ACCESS_MODE the docs and the run manifest surface to the user.
_CAPABILITY_MODULES = {
    "src/megamaid/discovery.py": "browser-headers",
}

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
        relative = path.relative_to(repo_root).as_posix()
        if relative in _CAPABILITY_MODULES:
            continue
        hits = [pattern for pattern in _OFFENDER_PATTERNS if pattern in line]
        if not hits or _HONEST_MARKER in line:
            continue
        offenders.append(f"{relative}:{lineno}: [{', '.join(hits)}] {line.strip()}")

    assert not offenders, (
        "undeclared browser impersonation in shipped content:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither identify as megamaid, or move this behind a declared access "
        "mode and register the module in _CAPABILITY_MODULES so it reaches the "
        "capability table and the run manifest."
    )


def test_every_capability_module_declares_its_access_mode(repo_root):
    """A registered module must actually export the mode it claims.

    The registry is what lets browser-shaped headers through, so an entry that
    does not correspond to a real, named mode would be a silent hole rather than
    a declared capability.
    """
    import ast

    for relative, mode in _CAPABILITY_MODULES.items():
        path = repo_root / relative
        assert path.is_file(), f"{relative} is registered as a capability module but does not exist"

        declared = None
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ACCESS_MODE" for t in node.targets
            ):
                declared = getattr(node.value, "value", None)

        assert declared == mode, (
            f"{relative} is registered for access mode {mode!r} but declares "
            f"ACCESS_MODE = {declared!r}. The registry and the module must agree, "
            "or the manifest will record the wrong thing."
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


def test_policy_states_what_the_scaffold_does(repo_root):
    """The policy must describe behaviour, not only forbid things.

    Phrased purely as prohibition it failed twice, in both directions: a spoofed
    default violated no stated rule, and later the same framing made a genuine
    capability look like a violation to be deleted. What it has to pin down is
    the default, the escalation path, and the duty to declare which was used.
    """
    skill = (repo_root / "skills" / "megamaid" / "SKILL.md").read_text()
    for required in ("DEFAULT_USER_AGENT", "ACCESS_MODE", "browser-headers"):
        assert required in skill, (
            f"SKILL.md never mentions {required}; the access-mode policy needs to "
            "name the default, the escalation path, and the declaration requirement"
        )


def test_recon_does_not_need_playwright(repo_root):
    """`megamaid recon` must run in the launcher's browser-free venv.

    The launcher installs the `[cli]` extra, which has no playwright, and
    `recon` is documented as runnable from it. Importing DEFAULT_USER_AGENT
    through `base` — which imports playwright at module scope — made the
    documented first step die with ModuleNotFoundError. `constants.py` exists
    precisely so light consumers can avoid that; its own docstring says so.

    Asserted by source inspection rather than by import, because pytest runs in
    an environment that HAS playwright, so an import here would pass regardless.
    """
    import ast

    tree = ast.parse((repo_root / "src" / "megamaid" / "cli.py").read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "recon":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.module == "base":
                names = ", ".join(alias.name for alias in inner.names)
                offenders.append(f"cli.py:{inner.lineno}: recon imports {names} from .base")

    assert not offenders, (
        "\n  ".join(offenders)
        + "\n\n  base imports playwright at module scope; the launcher's venv has none. "
        "Import from .constants instead."
    )
