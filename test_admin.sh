#!/bin/bash
cd /home/dev/projects/nai-workbench
source custodian/.venv/bin/activate
python custodian/admin.py 2>/tmp/admin_error.log
echo "EXIT CODE: $?"
echo "STDERR:"
cat /tmp/admin_error.log
