"""Sample documentation and build script, modelled on linux-on-ibm-z style repos.

The pair below is deliberately inconsistent so the rule engine has something
real to find:

* ``libpcre3-dev`` and ``git`` are documented but never installed by the script
* ``EXAMPLE_ROOT`` is documented as ``/usr/local/example`` but the script uses ``/opt/example``
* the script uses ``--prefix=/opt/example`` while the docs say ``--prefix=/usr/local/example``
* the script applies two s390x workarounds that the docs never mention
"""

from __future__ import annotations

WIKI_PAGE_NAME = "Building-Example.md"
SCRIPT_NAME = "build_example.sh"
WIKI_COMMIT = "a" * 40
SCRIPT_COMMIT = "b" * 40

WIKI_TEXT = """\
# Building Example

Instructions for building Example 2.4.0 on Linux on IBM Z.

## Common prerequisites

```bash
sudo apt-get install -y curl
```

## Ubuntu

### Prerequisites

```bash
sudo apt-get install -y build-essential libssl-dev zlib1g-dev libpcre3-dev git
export EXAMPLE_ROOT=/usr/local/example
```

### Build and install

```bash
./configure --prefix=/usr/local/example --with-openssl --enable-shared
make -j$(nproc)
sudo make install
```

## RHEL

### Prerequisites

```bash
sudo yum install -y gcc make openssl-devel zlib-devel pcre-devel
```

### Build and install

```bash
./configure --prefix=/usr/local/example --with-openssl
make -j$(nproc)
```
"""

SCRIPT_TEXT = """\
#!/bin/bash
# Build Example 2.4.0

export EXAMPLE_ROOT=/opt/example
export CFLAGS="-O2 -march=zEC12 -mtune=zEC12"

sudo apt-get install -y build-essential libssl-dev zlib1g-dev

if [ "$(uname -m)" = "s390x" ]; then
  sed -i 's/-m64/-m31/g' auto/cc/conf
  patch -p1 < s390x-atomic-ops.patch
fi

cd "${EXAMPLE_ROOT}/src" || exit 1
./configure --prefix=/opt/example --with-openssl
make -j$(nproc)
sudo make install
"""
