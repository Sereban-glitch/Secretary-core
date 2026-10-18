#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PATH="$script_dir/flatpak-bin:$PATH"
export PATH
exec "$@"
