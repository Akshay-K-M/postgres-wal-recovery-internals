#!/bin/sh
# Install the debug build of PostgreSQL (plus the two extensions the app
# needs) into ~/pgsql. Run this after (re)building the dev container: the
# rebuild wipes the home directory, but the compiled tree in /workspaces stays.
set -eu
src=$(cd "$(dirname "$0")/.." && pwd)
cd "$src"

if [ ! -f src/Makefile.global ]; then
    ./configure --enable-debug --enable-cassert --prefix="$HOME/pgsql" CFLAGS="-O0 -g3"
fi
make -j"$(nproc)" -s
make -s install
make -s -C contrib/pageinspect install
make -s -C contrib/pg_walinspect install

python3 -c "import django" 2>/dev/null || pip3 install --user -r "$src/recovery_app/requirements.txt"
echo "Ready. Start the app with:  cd recovery_app && python3 manage.py runserver --noreload"
