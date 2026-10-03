#!/usr/bin/env bash
set -e

echo "🍇 Building Dion standalone executable..."

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e .
    .venv/bin/pip install pyinstaller
fi

# Ensure latest editable package is installed
.venv/bin/pip install -e . --no-deps

echo "Running PyInstaller in optimized directory mode..."
rm -rf build/pyi_dist build/pyi_build
.venv/bin/pyinstaller \
    --onedir \
    --clean \
    --name dion \
    --distpath build/pyi_dist \
    --workpath build/pyi_build \
    --exclude-module tkinter \
    --exclude-module unittest \
    --exclude-module test \
    --exclude-module pydoc \
    --noconfirm \
    main.py

echo "Creating self-caching standalone executable..."
mkdir -p dist build
TARBALL="build/dion_bundle.tar.gz"
tar -czf "$TARBALL" -C build/pyi_dist/dion .

if command -v shasum >/dev/null 2>&1; then
    BUNDLE_HASH=$(shasum -a 256 "$TARBALL" | awk '{print $1}')
else
    BUNDLE_HASH=$(sha256sum "$TARBALL" | awk '{print $1}')
fi

cat << 'EOF' | sed "s/__BUNDLE_HASH__/$BUNDLE_HASH/" > dist/dion
#!/bin/bash
BUNDLE_HASH="__BUNDLE_HASH__"

if [ -n "$XDG_CACHE_HOME" ]; then
    CACHE_BASE="$XDG_CACHE_HOME/dion"
elif [ -d "$HOME/Library/Caches" ]; then
    CACHE_BASE="$HOME/Library/Caches/dion"
else
    CACHE_BASE="$HOME/.cache/dion"
fi

CACHE_DIR="$CACHE_BASE/$BUNDLE_HASH"
APP_BIN="$CACHE_DIR/dion"

if [ ! -x "$APP_BIN" ]; then
    mkdir -p "$CACHE_DIR"
    PAYLOAD_LINE=$(awk '/^__PAYLOAD_BELOW__/ {print NR + 1; exit 0; }' "$0")
    tail -n "+$PAYLOAD_LINE" "$0" | tar -xzf - -C "$CACHE_DIR"
fi

exec "$APP_BIN" "$@"
__PAYLOAD_BELOW__
EOF

cat "$TARBALL" >> dist/dion
chmod +x dist/dion

echo ""
echo "✅ Build complete! High-performance standalone binary generated at: ./dist/dion"
echo "You can move it to your path with:"
echo "  sudo cp ./dist/dion /usr/local/bin/"
echo "or for user-only access:"
echo "  cp ./dist/dion ~/.local/bin/"

