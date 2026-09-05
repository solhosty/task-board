"""Programs that execute inside a persistent Coder runner, not on the desktop.

Each module here is sent over Coder SSH as stdin and run by ``python3 -``. They
must stay standard-library only and must never import from ``harness_rotation``
or ``app``; the host reads them with
``harness_rotation.remote_transport.payload``. Host code may import a pure
validator from one of them, as ``app`` does with ``validate_source``.
"""
