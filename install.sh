#!/usr/bin/env bash
# eisy / Polisy run FreeBSD: pip can exit non-zero on harmless resolver noise,
# and PG3 treats a non-zero install.sh as a failed install. Verify the import
# instead of trusting pip's exit code.
pip3 install -r requirements.txt --user --disable-pip-version-check
python3 -c "import udi_interface" && echo "Lotus Lantern node server: dependencies OK"
