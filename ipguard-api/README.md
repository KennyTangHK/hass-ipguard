# ipguard-api

**Beta software.** This software is in beta and is not a stable release.

This is an independent community project. It is not affiliated with, sponsored by, or endorsed by IPGUARD, NACD, or Noralsy.

Use it lawfully, only with your own authorized account and buildings or doors you have permission to access and control. Do not use it for unauthorized access or abuse.

**Use at your own risk.** To the extent permitted by applicable law, the software is provided without warranty, and its authors and contributors accept no responsibility or liability for its use. The warranty and liability limitations in the [Apache-2.0 license](LICENSE) apply.

A typed asynchronous Python library for an authorized IPGUARD cloud account. It implements phone/email enrollment, ZeroMQ CURVE connections, pushed relay discovery, passive incoming-call callbacks and explicit remote release commands. It has no dependency on Home Assistant or the JavaScript POC.

[API usage](docs/USAGE.md) explains enrollment, saved sessions, event subscriptions and command errors. [Development](../DEVELOPMENT.md) covers tests and release builds.

Python and dependency requirements are defined in the [package metadata](pyproject.toml), with resolved dependencies in the [workspace lockfile](../uv.lock). The library is independently packaged as `ipguard-api` under Apache-2.0; this workspace does not publish it to PyPI. The Home Assistant release ZIP includes a generated copy of this same library.

Protocol behavior is defined in the [protocol implementation](src/ipguard_api/protocol.py), with Android metadata values in the [handset profile](src/ipguard_api/models.py). Direct enrollment uses ordinary TCP before the authenticated CURVE channel. Board acknowledgements do not measure physical door state. Bluetooth, SIP and video are outside its scope. Incoming offers are reported without acknowledgements, answers or media negotiation. Local fixtures verify protocol behavior; account-specific vendor compatibility and physical release remain field checks.
