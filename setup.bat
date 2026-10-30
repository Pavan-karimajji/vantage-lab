@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYCMD="
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "" >nul 2>&1
  if not errorlevel 1 set "PYCMD=py -3"
)
REM Fallback to unversioned py/python only if -3 isn't resolvable -- prefer
REM -3 because PowerShell's own child-process invocation of a bare, unversioned
REM `py <script>.py` can fail (CreateProcess error) on machines where it
REM works fine from a real cmd.exe window; `py -3` sidesteps py.exe's own
REM auto-select path entirely and has been verified to work from both.
if not defined PYCMD (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -c "" >nul 2>&1
    if not errorlevel 1 set "PYCMD=py"
  )
)
if not defined PYCMD (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "" >nul 2>&1
    if not errorlevel 1 set "PYCMD=python"
  )
)
if not defined PYCMD (
  echo ERROR: Python was not found in PATH.
  exit /b 1
)

%PYCMD% "%~dp0scripts\setup.py" %*
exit /b %errorlevel%
