@echo off
echo.
echo  Don't forget to update version in pyproject.toml!
echo.
pause
python -m build
twine upload dist/*
if not exist old mkdir old
move dist\*.whl old\ >nul
move dist\*.tar.gz old\ >nul
echo  Done. Dist files moved to old\
