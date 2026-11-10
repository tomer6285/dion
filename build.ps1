# Build on Windows: powershell -File .\build.ps1
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path '.venv-build\Scripts\python.exe')) {
        py -3 -m venv .venv-build
        if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment' }
    }
    $python = '.\.venv-build\Scripts\python.exe'
    & $python -m pip install . pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & $python -m PyInstaller --onedir --clean --noconfirm --name dion --distpath dist --workpath build/pyi_build --hidden-import _cffi_backend --hidden-import cffi main.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
    & '.\dist\dion\dion.exe' --help
    if ($LASTEXITCODE -ne 0) { throw 'Packaged CLI startup failed' }
    Compress-Archive -Path 'dist\dion' -DestinationPath 'dist\dion-windows.zip' -Force
    Write-Host 'Built dist\dion-windows.zip. Extract the entire folder before running dion.exe.'
} finally {
    Pop-Location
}
