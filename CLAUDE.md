# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## CodeGraph First Policy

Tool budget:

- Maximum 2 codegraph_search calls
- Maximum 1 codegraph_files call
- Maximum 1 file read

After finding the file:
implement immediately.

## PowerShell UTF-8 Reading

When reading files that may contain Russian text in PowerShell, set the console output encoding explicitly and read as UTF-8 to avoid mojibake:

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -LiteralPath path\to\file.md -Encoding UTF8
```
