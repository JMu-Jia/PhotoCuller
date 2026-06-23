$ErrorActionPreference = "Stop"
$python = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $python tests\capture_gui_original_end.py
& $python tests\capture_gui_zoom_pan.py
& $python tests\capture_readme_screenshot.py
