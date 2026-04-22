#!/bin/bash

# Problem: Asciinema files are taking up a lot of space and we don't need them for analysis.
# Solution: Given a directory, find and delete all .cast files and terminus_2.pane files.
# Usage: ./delete_asciinema_files.sh /path/to/directory

set -euo pipefail

if [[ $# -ne 1 ]]; then
	echo "Usage: $0 /path/to/directory" >&2
	exit 1
fi

target_dir=$1

if [[ ! -d "$target_dir" ]]; then
	echo "Error: directory does not exist: $target_dir" >&2
	exit 1
fi

deleted_count=0

while IFS= read -r -d '' file_path; do
	rm -f -- "$file_path"
	printf 'Deleted: %s\n' "$file_path"
	deleted_count=$((deleted_count + 1))
done < <(find "$target_dir" -type f \( -name '*.cast' -o -name 'terminus_2.pane' \) -print0)

printf 'Deleted %d file(s).\n' "$deleted_count"

