@echo off
:: Windows launcher for the PawPoller CLI.
:: Drop this somewhere on your PATH (or run it via full path) for
:: easy access. The Python script lives next to it.
python "%~dp0pawpoller_cli.py" %*
