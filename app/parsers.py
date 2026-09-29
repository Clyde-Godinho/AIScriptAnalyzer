"""Deterministic extraction of build components from docs and shell scripts.

Nothing in here talks to an LLM or the network: it is pure text processing so
that the ``(package_name, distro)`` matching, the rule-based diff and the
evaluation harness are reproducible. Regexes are anchored at line starts (after
stripping shell prompts and comments), which keeps prose mentions such as
"install the prerequisites" from being mistaken for commands.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from app.models import (
    BuildFlag,
    ComponentSignature,
    Dependency,
    Distro,
    EnvVar,
    PackageManager,
    S390xPatch,
    SourceType,
    normalize_distro,
)

# --------------------------------------------------------------------------- #
# Regexes
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([\w+.-]*)[ \t]*$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)

_PROMPT_RE = re.compile(r"^[\s]*(?:\$|#|>|>>>|sudo\s+#)\s+")
_LEADING_SUDO_RE = re.compile(r"^\s*(?:sudo\s+(?:-[A-Za-z]+\s+)*|env\s+)")

_ENV_EXPORT_RE = re.compile(
    r"^\s*(?P<export>export\s+)?(?P<name>[A-Z][A-Z0-9_]{1,})\s*=\s*(?P<value>.*?)\s*$"
)

_INSTALL_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?P<tool>apt-get|apt|yum|dnf|zypper|pip3|pip|microdnf)"
    r"\s+(?P<verb>install|in|-i|add|groupinstall)"
    r"(?P<args>.*)$",
    re.IGNORECASE,
)

_CMAKE_D_RE = re.compile(r"(?<![\w-])-D(?P<key>[A-Za-z_][\w.]*)(?:=(?P<val>[^\s'\"]*|\"[^\"]*\"|'[^']*'))?")
_CONFIGURE_OPT_RE = re.compile(r"(?<![\w-])(?P<flag>--[a-zA-Z][\w-]*(?:=(?:[^\s'\"]+|\"[^\"]*\"|'[^']*'))?)")
_COMPILER_FLAG_RE = re.compile(
    r"^\s*(?P<var>CFLAGS|CXXFLAGS|CPPFLAGS|LDFLAGS|RUSTFLAGS|GOFLAGS)\s*=\s*(?P<value>.*)$"
)
_ARCH_FLAG_RE = re.compile(r"-march=(?P<arch>z\d+|zEC\d+[a-z]*|z13|z14|z15|z16)[^\s]*", re.IGNORECASE)

_PATCH_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?P<tool>patch|git\s+apply|git\s+am)\s+(?P<args>.*)$", re.IGNORECASE
)
_SED_INPLACE_RE = re.compile(r"^\s*(?:sudo\s+)?sed\s+(?:-[a-zA-Z]*i[a-zA-Z]*|--in-place)\b(?P<args>.*)$")
_ARCH_GUARD_RE = re.compile(
    r"(uname\s+-[a-z]*m[a-z]*|s390x|z\s*/\s*architecture|zarchitecture|IBM\s*Z)", re.IGNORECASE
)
_PATCH_REF_RE = re.compile(r"[\w./@-]+\.(?:patch|diff)\b")

_DISTRO_WORD_RE = re.compile(
    r"\b(?P<distro>ubuntu|rhel|red\s*hat|centos|fedora|sles?|suse|opensuse|debian)\b",
    re.IGNORECASE,
)
_OS_RELEASE_RE = re.compile(r"(?:/etc/os-release|ID_LIKE|VERSION_ID|\bID\b\s*=)", re.IGNORECASE)

_VERSION_SUFFIX_RE = re.compile(r"^(?P<name>[A-Za-z0-9][\w.+:-]*?)(?P<op>>=|<=|==|~=|>|<|=)(?P<ver>[\w.+-]+)$")
_SHELL_STOP_RE = re.compile(r"\|\||&&|[|;&<>()]")
_PLAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")

def _dedupe(values: Iterable[Distro]) -> list[Distro]:
    """Order-preserving de-duplication of distro candidates."""
    seen: list[Distro] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


#: Tokens that appear in install commands but are not packages.
_NOISE_TOKENS = {
    "-y",
    "--yes",
    "--no-install-recommends",
    "--non-interactive",
    "-n",
    "-i",
    "--install-recommends",
    "install",
    "||",
    "|",
    "&&",
    ";",
    "sudo",
}

#: Map installer executables onto (package manager, implied distro).
_TOOL_META: dict[str, tuple[PackageManager, Distro]] = {
    "apt": (PackageManager.APT, Distro.UBUNTU),
    "apt-get": (PackageManager.APT, Distro.UBUNTU),
    "yum": (PackageManager.YUM, Distro.RHEL),
    "dnf": (PackageManager.DNF, Distro.RHEL),
    "microdnf": (PackageManager.DNF, Distro.RHEL),
    "zypper": (PackageManager.ZYPPER, Distro.SLES),
    "pip": (PackageManager.PIP, Distro.UNKNOWN),
    "pip3": (PackageManager.PIP, Distro.UNKNOWN),
}

#: Common doc-to-script naming aliases in linux-on-ibm-z.
PACKAGE_ALIASES: dict[str, str] = {
    "golang": "go",
    "go": "go",
    "node": "nodejs",
    "nodejs": "nodejs",
    "openjdk": "java",
    "jdk": "java",
    "java": "java",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "python": "python3",
    "python3": "python3",
    "gcc": "gcc",
    "openssl": "openssl",
}


# --------------------------------------------------------------------------- #
# Low-level text helpers
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Section:
    """A markdown section, with the distro it documents (inherited from parents)."""

    heading: str
    level: int
    distro: Distro
    start: int
    end: int
    text: str = ""
    parent_headings: list[str] = field(default_factory=list)

    @property
    def heading_path(self) -> str:
        return " > ".join([*self.parent_headings, self.heading]) if self.parent_headings else self.heading


def normalize_package_name(name: str) -> str:
    """Lower-case and drop separators for fuzzy doc/script pairing."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def canonical_package_name(name: str) -> str:
    """Apply the alias table so ``Golang`` and ``Go`` are the same package."""
    normalized = normalize_package_name(name)
    return PACKAGE_ALIASES.get(normalized, normalized)


def strip_comments(line: str) -> str:
    """Remove a trailing shell comment that is outside quotes."""
    out: list[str] = []
    quote: str | None = None
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (idx == 0 or line[idx - 1].isspace()):
            break
        out.append(ch)
    return "".join(out)


def logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, line)`` with prompts stripped and ``\\`` continuations joined."""
    buffer: list[str] = []
    start_line = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        continued = line.endswith("\\")
        if continued:
            line = line[:-1]
        if not buffer:
            start_line = number
            line = _PROMPT_RE.sub("", line, count=1)
        buffer.append(line)
        if continued:
            continue
        joined = " ".join(part.rstrip() for part in buffer).strip()
        buffer = []
        if joined:
            yield start_line, joined
    if buffer:  # unterminated continuation
        yield start_line, " ".join(buffer).strip()


def iter_code_blocks(markdown: str) -> Iterator[tuple[str, str, int]]:
    """Yield ``(language, code, start_line)`` for every fenced code block."""
    language: str | None = None
    collected: list[str] = []
    start = 0
    for number, line in enumerate(markdown.splitlines(), start=1):
        fence = _FENCE_RE.match(line)
        if fence:
            if language is None:
                language = fence.group(2) or ""
                collected = []
                start = number + 1
            else:
                yield language, "\n".join(collected), start
                language = None
                collected = []
            continue
        if language is not None:
            collected.append(line)
    if language is not None:
        yield language, "\n".join(collected), start


def iter_markdown_sections(markdown: str) -> list[Section]:
    """Split a wiki page into sections, resolving the distro each one documents."""
    matches = list(_HEADING_RE.finditer(markdown))
    sections: list[Section] = []
    stack: list[tuple[int, str, Distro]] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        while stack and stack[-1][0] >= level:
            stack.pop()
        inherited = stack[-1][2] if stack else Distro.UNKNOWN
        distro = normalize_distro(heading)
        if distro is Distro.UNKNOWN:
            distro = inherited
        parents = [item[1] for item in stack]
        sections.append(
            Section(
                heading=heading,
                level=level,
                distro=distro,
                start=match.start(),
                end=end,
                text=markdown[match.end() : end].strip(),
                parent_headings=parents,
            )
        )
        stack.append((level, heading, distro))
    return sections


def detect_distros(text: str, *, code: str | None = None) -> list[Distro]:
    """Best-effort set of distros a script supports.

    ``code`` (when supplied) is the shell content minus prose, so that comments
    and titles do not inflate the result for documentation pages.
    """
    haystack = code if code is not None else text
    found: list[Distro] = []
    for match in _DISTRO_WORD_RE.finditer(haystack):
        distro = normalize_distro(match.group("distro"))
        if distro is not Distro.UNKNOWN and distro not in found:
            found.append(distro)
    if not found and _OS_RELEASE_RE.search(haystack):
        return [Distro.UNKNOWN]
    return found or [Distro.UNKNOWN]


# --------------------------------------------------------------------------- #
# Component extraction
# --------------------------------------------------------------------------- #


def _clean_token(token: str) -> str:
    return token.strip().strip("'\"").rstrip(",").lstrip("{(").rstrip(")}")


def split_packages(args: str) -> list[tuple[str, str | None]]:
    """Split an install command's arguments into ``(name, version)`` pairs.

    Scripts in ``linux-on-ibm-z/scripts`` routinely pipe installer output
    (``sudo apt-get install -y foo |& tee -a "$LOG_FILE"``), so the argument
    string is truncated at the first shell operator and expansions are dropped
    instead of being mistaken for package names.
    """
    args = _SHELL_STOP_RE.split(args, maxsplit=1)[0]
    tokens: list[str] = []
    for raw in re.split(r"\s+", args):
        token = _clean_token(raw)
        if not token or token in _NOISE_TOKENS or token.startswith("-"):
            continue
        if "$" in token or "`" in token or token.endswith(":"):
            continue
        if token in {"then", "fi", "done", "do", "else", "esac"}:
            continue
        # Keep `pkg=1.2.3`, drop option assignments such as `Dpkg::Options::=...`.
        if "=" in token and not _PLAIN_NAME_RE.match(token.split("=", 1)[0]):
            continue
        tokens.append(token)

    packages: list[tuple[str, str | None]] = []
    for token in tokens:
        matched = _VERSION_SUFFIX_RE.match(token)
        if matched:
            packages.append((matched.group("name"), matched.group("ver")))
        else:
            packages.append((token, None))
    return packages


def _extract_dependencies(
    text: str, default_distro: Distro, *, only_code: bool
) -> list[Dependency]:
    dependencies: list[Dependency] = []
    seen: set[tuple[str, PackageManager]] = set()

    sources: Iterable[tuple[int, str]] = _code_lines(text) if only_code else logical_lines(text)

    for _, line in sources:
        stripped = _LEADING_SUDO_RE.sub("", strip_comments(line)).strip()
        match = _INSTALL_RE.match(stripped)
        if not match:
            continue
        tool = match.group("tool").lower()
        manager, implied = _TOOL_META.get(tool, (PackageManager.OTHER, Distro.UNKNOWN))
        distro = default_distro if default_distro is not Distro.UNKNOWN else implied
        for name, version in split_packages(match.group("args")):
            key = (name.lower(), manager)
            if key in seen:
                continue
            seen.add(key)
            dependencies.append(
                Dependency(
                    name=name,
                    version=version,
                    manager=manager,
                    distro=distro,
                    evidence=line.strip()[:300],
                )
            )
    return dependencies


def _extract_env_vars(text: str, *, only_code: bool) -> list[EnvVar]:
    env_vars: dict[str, EnvVar] = {}
    sources: Iterable[tuple[int, str]] = _code_lines(text) if only_code else logical_lines(text)

    for _, line in sources:
        stripped = strip_comments(line)
        if not stripped or stripped.startswith(("//", "*")):
            continue
        match = _ENV_EXPORT_RE.match(stripped)
        if not match:
            continue
        name = match.group("name")
        value = match.group("value").strip().strip('"').strip("'")
        if name in {"PATH", "PWD", "HOME", "USER", "SHELL", "TERM", "OSTYPE", "HOSTNAME"} and not value:
            continue
        env_vars[name] = EnvVar(
            name=name,
            value=value or None,
            exported=bool(match.group("export")),
            evidence=line.strip()[:300],
        )
    return list(env_vars.values())


def _extract_build_flags(text: str, *, only_code: bool) -> list[BuildFlag]:
    flags: dict[str, BuildFlag] = {}
    sources: Iterable[tuple[int, str]] = _code_lines(text) if only_code else logical_lines(text)

    for _, line in sources:
        stripped = re.sub(r"^\s*(?:export|env)\s+", "", strip_comments(line)).strip()
        if not stripped:
            continue

        lowered = stripped.lower()
        if "./configure" in lowered or re.search(r"\bconfigure\b", lowered):
            for match in _CONFIGURE_OPT_RE.finditer(stripped):
                flag = match.group("flag")
                flags.setdefault(flag, BuildFlag(flag=flag, target="configure", evidence=stripped[:300]))
        if lowered.startswith("cmake") or " cmake " in f" {lowered}":
            for match in _CMAKE_D_RE.finditer(stripped):
                key = match.group("key")
                value = (match.group("val") or "").strip("'\"")
                flag = f"-D{key}" + (f"={value}" if value else "")
                flags.setdefault(flag, BuildFlag(flag=flag, target="cmake", evidence=stripped[:300]))
        if lowered.startswith("make") or "make " in f" {lowered}":
            if "-j" in stripped:
                flags.setdefault("make -j", BuildFlag(flag="make -j", target="make", evidence=stripped[:300]))

        compiler = _COMPILER_FLAG_RE.match(stripped)
        if compiler:
            variable = compiler.group("var")
            for arch in _ARCH_FLAG_RE.finditer(stripped):
                flag = f"{variable}+={arch.group(0)}"
                flags.setdefault(
                    flag,
                    BuildFlag(flag=flag, target="compiler", evidence=stripped[:300]),
                )
            if not _ARCH_FLAG_RE.search(stripped):
                value = compiler.group("value").strip().strip('"')
                flag = f"{variable}={value}"[:120]
                flags.setdefault(flag, BuildFlag(flag=flag, target="compiler", evidence=stripped[:300]))
    return list(flags.values())


def _code_lines(text: str) -> list[tuple[int, str]]:
    """Lines inside fenced code blocks, with interactive prompts stripped.

    Documentation often shows commands as ``$ sudo apt-get install -y foo``, so
    the prompt has to go before the line-anchored regexes run.
    """
    lines: list[tuple[int, str]] = []
    for _, code, start in iter_code_blocks(text):
        for offset, line in enumerate(code.splitlines()):
            lines.append((start + offset, _PROMPT_RE.sub("", line, count=1)))
    return lines


def _patch_target(args: str) -> str | None:
    """Return the file/patch path a ``patch``/``git apply`` command touches."""
    candidates = _PATCH_REF_RE.findall(args) or re.findall(r"[\w./-]+\.[a-z]{1,5}\b", args)
    return candidates[0] if candidates else None


def _extract_s390x_patches(text: str, *, only_code: bool) -> list[S390xPatch]:
    patches: list[S390xPatch] = []
    seen: set[tuple[str, str]] = set()
    sources: Iterable[tuple[int, str]] = _code_lines(text) if only_code else logical_lines(text)
    window: list[str] = []

    for _, raw_line in sources:
        line = strip_comments(raw_line).strip()
        if not line:
            continue
        window.append(line)
        if len(window) > 6:
            window.pop(0)
        context = " ".join(window)
        is_s390x = bool(_ARCH_GUARD_RE.search(line)) or bool(_ARCH_GUARD_RE.search(context))

        patch_cmd = _PATCH_RE.match(line)
        sed_cmd = _SED_INPLACE_RE.match(line)

        if patch_cmd and is_s390x:
            target = _patch_target(patch_cmd.group("args"))
            method = "git_apply" if patch_cmd.group("tool").lower().startswith("git") else "patch"
            key = (method, target or line)
            if key not in seen:
                seen.add(key)
                patches.append(
                    S390xPatch(
                        description=f"s390x patch applied via `{patch_cmd.group('tool')}`",
                        method=method,
                        target=target,
                        architecture_guard=_guard_of(context),
                        evidence=line[:300],
                    )
                )
        elif sed_cmd and is_s390x:
            key = ("sed", line[:120])
            if key not in seen:
                seen.add(key)
                patches.append(
                    S390xPatch(
                        description="in-place sed edit targeting s390x behaviour",
                        method="sed",
                        target=_patch_target(sed_cmd.group("args")),
                        architecture_guard=_guard_of(context),
                        evidence=line[:300],
                    )
                )
        elif is_s390x and re.search(r"\buname\s+-[a-z]*m", line) and re.search(r"\b(if|elif|case)\b", context):
            key = ("conditional", line[:120])
            if key not in seen:
                seen.add(key)
                patches.append(
                    S390xPatch(
                        description="architecture-conditional branch handling s390x",
                        method="conditional",
                        architecture_guard=line[:200],
                        evidence=line[:300],
                    )
                )
    return patches


def _guard_of(context: str) -> str | None:
    match = re.search(r"uname\s+-[a-z]*m[^;|&)]*", context)
    if match:
        return match.group(0).strip()[:200]
    return "uname -m == s390x" if "s390x" in context.lower() else None


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def parse_markdown_doc(
    text: str,
    package_name: str,
    source_path: str = "",
    commit: str | None = None,
    distro: Distro | str | None = None,
) -> ComponentSignature:
    """Extract components documented by a wiki page (``Building-<Package>``)."""
    sections = iter_markdown_sections(text)
    # A wiki page names its distros in the headings; the code blocks add any
    # extra ones (e.g. an ``/etc/os-release`` check inside a snippet).
    mentioned = _dedupe([section.distro for section in sections if section.distro is not Distro.UNKNOWN])
    code_text = "\n".join(code for _, code, _ in iter_code_blocks(text))
    code_distros = [value for value in detect_distros(code_text) if value is not Distro.UNKNOWN]
    mentioned = _dedupe([*mentioned, *code_distros]) or [Distro.UNKNOWN]
    resolved = normalize_distro(distro) if distro is not None else mentioned[0]

    return ComponentSignature(
        package_name=package_name,
        distro=resolved,
        source_type=SourceType.DOC,
        source_path=source_path,
        commit=commit,
        env_vars=_extract_env_vars(text, only_code=True),
        dependencies=_extract_dependencies(text, Distro.UNKNOWN, only_code=True),
        build_flags=_extract_build_flags(text, only_code=True),
        s390x_patches=_extract_s390x_patches(text, only_code=True),
        distros_mentioned=mentioned,
    )


def parse_build_script(
    text: str,
    package_name: str,
    source_path: str = "",
    commit: str | None = None,
    distro: Distro | str | None = None,
    distros: list[Distro] | None = None,
) -> ComponentSignature:
    """Extract components from a ``build_<Package>.sh`` script."""
    resolved = normalize_distro(distro) if distro is not None else Distro.UNKNOWN
    mentioned = distros or detect_distros(text)
    if resolved is Distro.UNKNOWN and len(mentioned) == 1:
        resolved = mentioned[0]

    configure_options: list[str] = []
    for flag in _extract_build_flags(text, only_code=False):
        if flag.target == "configure":
            configure_options.append(flag.flag)

    return ComponentSignature(
        package_name=package_name,
        distro=resolved,
        source_type=SourceType.SCRIPT,
        source_path=source_path,
        commit=commit,
        env_vars=_extract_env_vars(text, only_code=False),
        dependencies=_extract_dependencies(text, resolved, only_code=False),
        build_flags=_extract_build_flags(text, only_code=False),
        s390x_patches=_extract_s390x_patches(text, only_code=False),
        configure_options=sorted(set(configure_options)),
        distros_mentioned=mentioned,
    )


def guess_package_from_path(path: str) -> str:
    """``scripts/nodejs/18/build_nodejs.sh`` -> ``nodejs``.

    The wiki name is preferred when available, but the script path is the
    fallback and also the source of the version directory.
    """
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if not parts:
        return ""
    filename = parts[-1]
    stem = re.sub(r"\.(sh|bash)$", "", filename)
    stem = re.sub(r"^(build|install|setup)[-_]", "", stem, flags=re.IGNORECASE)
    if stem and stem.lower() not in {"build", "install"}:
        return stem
    for part in reversed(parts[:-1]):
        if re.match(r"^v?\d", part):
            continue
        return part
    return parts[0]


def doc_package_from_path(path: str) -> str:
    """``Building-Nodejs.md`` -> ``nodejs``."""
    stem = re.sub(r"\.(md|markdown)$", "", path.replace("\\", "/").split("/")[-1], flags=re.I)
    stem = re.sub(r"^building[-_ ]+", "", stem, flags=re.I)
    return stem


def version_from_path(path: str, package_name: str = "") -> str | None:
    """Pick the version directory out of a script path, if present."""
    parts = path.replace("\\", "/").split("/")
    for part in reversed(parts[:-1]):
        if re.fullmatch(r"v?\d+(?:[._]\d+)*", part):
            return part.lstrip("v")
    if package_name:
        match = re.search(rf"{re.escape(package_name)}[-_]?(\d[\w.]*)", path, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def chunk_text(text: str, size: int = 1400, overlap: int = 200) -> list[str]:
    """Paragraph-aware chunker with character overlap between neighbours."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in paragraphs:
        if len(paragraph) > size:
            flush()
            sentences = re.split(r"(?<=[.;])\s+", paragraph)
            buffer = ""
            for sentence in sentences:
                if len(buffer) + len(sentence) + 1 > size and buffer:
                    chunks.append(buffer.strip())
                    buffer = buffer[-overlap:] if overlap else ""
                buffer = f"{buffer} {sentence}".strip()
            if buffer.strip():
                chunks.append(buffer.strip())
            continue
        if len(current) + len(paragraph) + 2 > size and current:
            flush()
            if overlap and chunks:
                tail = chunks[-1][-overlap:]
                current = f"{tail}\n\n"
        current = f"{current}\n\n{paragraph}".strip() if current else paragraph

    flush()

    # Merge a tiny trailing chunk back into its predecessor.
    if len(chunks) > 1 and len(chunks[-1]) < size // 5:
        chunks[-2] = f"{chunks[-2]}\n\n{chunks[-1]}"
        chunks.pop()
    return chunks


__all__ = [
    "PACKAGE_ALIASES",
    "Section",
    "canonical_package_name",
    "chunk_text",
    "detect_distros",
    "doc_package_from_path",
    "guess_package_from_path",
    "iter_code_blocks",
    "iter_markdown_sections",
    "logical_lines",
    "normalize_package_name",
    "parse_build_script",
    "parse_markdown_doc",
    "split_packages",
    "strip_comments",
    "version_from_path",
]
