Test all 17 MCP tools and report results. Run each one and show pass/fail. Do NOT skip any.

1. list_projects()
2. get_project_fossil("fba-command-center")
3. lookup_symbol("fba-command-center", "main")
4. get_symbol_context("fba-command-center", "main")
5. find_related_files("fba-command-center", "main")
6. get_recent_changes("fba-command-center")
7. get_detective_insights("fba-command-center")
8. trigger_custodian("fba-command-center")
9. sandbox_start("fba-command-center")
10. sandbox_status()
11. sandbox_logs()
12. sandbox_test("fba-command-center")
13. sandbox_stop()
14. sandbox_restart()
15. penpot_list_projects()
16. penpot_get_page("any-file-id-from-step-15")
17. penpot_export_svg("any-file-id-from-step-15")

After running all 17, print a summary table:

| # | Tool | Status | Notes |
|---|------|--------|-------|

Then say "MCP TEST COMPLETE — X/17 passed"
