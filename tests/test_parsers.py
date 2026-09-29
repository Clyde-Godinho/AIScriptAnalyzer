from __future__ import annotations

from app.models import Distro, PackageManager, SourceType, match_key, normalize_distro
from app.parsers import (
    chunk_text,
    detect_distros,
    doc_package_from_path,
    guess_package_from_path,
    iter_markdown_sections,
    parse_build_script,
    parse_markdown_doc,
    version_from_path,
)
from tests.samples import SCRIPT_TEXT, WIKI_TEXT


def test_markdown_sections_inherit_distro() -> None:
    sections = {section.heading: section for section in iter_markdown_sections(WIKI_TEXT)}
    assert sections["Ubuntu"].distro is Distro.UBUNTU
    assert sections["Prerequisites"].distro in {Distro.UBUNTU, Distro.RHEL}
    assert sections["Common prerequisites"].distro is Distro.UNKNOWN

    nested = [section for section in iter_markdown_sections(WIKI_TEXT) if section.heading == "Build and install"]
    assert nested[0].distro is Distro.UBUNTU
    assert nested[1].distro is Distro.RHEL
    assert nested[0].heading_path.endswith("Build and install")


def test_doc_extraction_finds_dependencies_env_vars_and_flags() -> None:
    signature = parse_markdown_doc(WIKI_TEXT, "example", source_path="docs/wiki/Building-Example.md")

    assert signature.source_type is SourceType.DOC
    names = {dep.name for dep in signature.dependencies}
    assert {"libssl-dev", "libpcre3-dev", "git", "curl"} <= names
    assert {dep.manager for dep in signature.dependencies} == {
        PackageManager.APT,
        PackageManager.YUM,
    }
    rhel_deps = {dep.name for dep in signature.dependencies if dep.manager is PackageManager.YUM}
    assert "zlib-devel" in rhel_deps and "pcre-devel" in rhel_deps

    assert signature.env_var_index()["EXAMPLE_ROOT"].value == "/usr/local/example"

    flags = set(signature.flag_index())
    assert "--prefix=/usr/local/example" in flags
    assert "--enable-shared" in flags
    assert Distro.UBUNTU in signature.distros_mentioned
    assert Distro.RHEL in signature.distros_mentioned


def test_script_extraction_matches_commands_and_s390x_workarounds() -> None:
    signature = parse_build_script(SCRIPT_TEXT, "example", source_path="scripts/example/2.4.0/build_example.sh")

    assert signature.source_type is SourceType.SCRIPT
    assert {dep.name for dep in signature.dependencies} >= {"build-essential", "libssl-dev", "zlib1g-dev"}
    assert signature.env_var_index()["EXAMPLE_ROOT"].value == "/opt/example"
    assert "CFLAGS+=-march=zEC12" in signature.flag_index()
    assert len(signature.s390x_patches) >= 2
    assert any(patch.target and "atomic" in patch.target for patch in signature.s390x_patches)
    assert any(patch.method == "sed" for patch in signature.s390x_patches)


def test_docs_only_count_code_blocks() -> None:
    prose = (
        "Install the prerequisites with apt-get install -y libfoo-dev.\n"
        "Then export SHELL_ROOT=/opt before building.\n"
    )
    signature = parse_markdown_doc(prose, "foo")
    # Commands must start a line inside a fenced block: prose is ignored.
    assert signature.dependencies == []
    assert "SHELL_ROOT" not in signature.env_var_index()

    fenced = "```bash\napt-get install -y libfoo-dev\nexport SHELL_ROOT=/opt\n```\n"
    fenced_signature = parse_markdown_doc(fenced, "foo")
    assert {dep.name for dep in fenced_signature.dependencies} == {"libfoo-dev"}
    assert fenced_signature.env_var_index()["SHELL_ROOT"].value == "/opt"


    # A prompt-prefixed line inside a code block is still a command.
    prompted = "```bash\n$ sudo yum install -y pcre-devel\n```\n"
    prompted_signature = parse_markdown_doc(prompted, "foo")
    assert {dep.name for dep in prompted_signature.dependencies} == {"pcre-devel"}
    assert prompted_signature.dependencies[0].manager is PackageManager.YUM


def test_install_commands_are_truncated_at_shell_operators() -> None:
    piped = 'sudo apt-get install -y libssl-dev zlib1g-dev |& tee -a "$LOG_FILE"\n'
    signature = parse_build_script(piped, "demo")
    assert {dep.name for dep in signature.dependencies} == {"libssl-dev", "zlib1g-dev"}

    with_options = 'sudo apt-get install -y -o Dpkg::Options::="--force-confdef" nginx=1.24.0\n'
    options_signature = parse_build_script(with_options, "demo")
    assert [(dep.name, dep.version) for dep in options_signature.dependencies] == [("nginx", "1.24.0")]


def test_match_key_is_deterministic_and_normalized() -> None:
    assert match_key("Node-js", "RHEL 9.2") == match_key("nodejs", "rhel")
    assert match_key("Nginx", "ubuntu") == "nginx::ubuntu"
    assert normalize_distro("SLES 15 SP5") is Distro.SLES
    assert normalize_distro("Debian 12") is Distro.DEBIAN
    assert normalize_distro("Haiku") is Distro.UNKNOWN


def test_path_helpers() -> None:
    assert guess_package_from_path("scripts/nodejs/18/build_nodejs.sh") == "nodejs"
    assert guess_package_from_path("scripts/nginx/1.24.0/build.sh") == "nginx"
    assert doc_package_from_path("docs/wiki/Building-Nodejs.md") == "Nodejs"
    assert version_from_path("scripts/nginx/1.24.0/build_nginx.sh", "nginx") == "1.24.0"


def test_distro_detection_from_script_branches() -> None:
    script = """\
if [[ $(grep -q ubuntu /etc/os-release) ]]; then
  echo ubuntu
elif [[ $(grep -q rhel /etc/os-release) ]]; then
  echo rhel
fi
"""
    detected = detect_distros(script)
    assert Distro.UBUNTU in detected and Distro.RHEL in detected


def test_chunking_respects_size_and_keeps_overlap() -> None:
    text = "\n\n".join(f"Paragraph {index} " + "lorem ipsum " * 20 for index in range(12))
    chunks = chunk_text(text, size=400, overlap=50)
    assert len(chunks) > 3
    assert all(len(chunk) <= 600 for chunk in chunks)
    assert "".join(chunks).count("lorem") >= text.count("lorem") * 0.9


def test_short_text_is_a_single_chunk() -> None:
    assert chunk_text("hello world", size=400, overlap=50) == ["hello world"]
    assert chunk_text("   ", size=400, overlap=50) == []
