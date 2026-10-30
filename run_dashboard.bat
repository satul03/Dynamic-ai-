@echo off
echo ==============================================================================
echo Starting Bus Pricing Intelligence Streamlit Dashboard...
echo ==============================================================================
"%~dp0.venv\Scripts\streamlit.exe" run "%~dp0app.py"
pause
