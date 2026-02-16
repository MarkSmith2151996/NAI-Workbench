#!/usr/bin/env bash
# Install security hooks into a project
PROJECT_DIR="."
HOOK_SOURCE=""/bin/pre-commit"

if [ ! -d "/.git" ]; then
  echo "Error:  is not a git repository"
  exit 1
fi

mkdir -p "/.git/hooks"
cp "" "/.git/hooks/pre-commit"
chmod +x "/.git/hooks/pre-commit"
echo "Security hooks installed in "
