@echo off
echo.
echo  Don't forget to update version in pyproject.toml!
echo.
pause
python -m build
twine upload dist/*
